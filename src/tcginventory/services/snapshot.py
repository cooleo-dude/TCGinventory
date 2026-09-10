"""The published inventory projection (ticket 73).

What a phone at a card show actually needs is "do I own this, what did I pay,
what is it worth, where is it" — and that is a *derived, read-only* view. It is
built here, on the desktop, and published; the phone never sees the ledger.

That distinction is the whole design, so it is worth being blunt about why. Every
primary key in this schema is an ``INTEGER PRIMARY KEY``, item identity is
match-or-create by lowest id, and FIFO draw-down orders by ``(acquired_at, id)``.
Two devices both writing would collide on ids, and even with UUIDs the FIFO
tiebreak is insertion order — so merging two divergent ledgers silently changes
which lots were consumed, which changes cost basis, which changes realized P/L.
There is no reconciling that afterwards. A projection cannot be merged back,
which is exactly the property we want.

The payload is small because :class:`~tcginventory.services.inventory.InventoryRow`
is already flat: an ``Item`` plus a dozen ints, no catalog join. Measured on the
real ledger, 1,479 rows serialise to 1,266 KB of JSON and **106 KB gzipped**,
against 38 MB for ``inventory.db`` — almost all of which is catalog and price
history a phone has no use for.

**Fields are listed explicitly.** Adding a column to ``Item`` must not silently
change the wire format, so :data:`_ITEM_FIELDS` is a deliberate list and
:data:`SCHEMA` is bumped when it changes.

**Encoding is deterministic**: canonical JSON (sorted keys, no incidental
whitespace) and gzip with ``mtime=0``, because gzip otherwise stamps the current
time into the header and two publishes of identical data would differ. Without
that, "skip the publish when nothing changed" cannot be implemented and retention
churns on every run.

Qt-free, and free of PIL and numpy — the art bundle lives in
:mod:`tcginventory.services.artbundle` precisely so this module stays that way.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from tcginventory import __version__
from tcginventory.data.db import now_iso
from tcginventory.data.repositories import AppMetaRepository
from tcginventory.services.inventory import InventoryService

# Bump when the shape below changes in a way a reader must notice. A client
# refuses a payload whose schema it does not know rather than guessing.
SCHEMA = 1

# Item fields that travel. Deliberately not ``asdict`` — created_at/updated_at
# are ledger bookkeeping a reader has no use for, and an explicit list means
# adding a column to Item is a decision rather than an accident.
_ITEM_FIELDS = (
    "name",
    "category",
    "game",
    "set_name",
    "set_code",
    "card_number",
    "variant",
    "language",
    "condition",
    "holo_pattern",
    "edition",
    "is_graded",
    "grading_company",
    "grade",
    "cert_number",
    "location",
    "tcgplayer_url",
    "market_value_asof",
    "notes",
)


def _plain(value):
    """Enum → its ``.value``; everything else unchanged.

    The domain uses enums for category, language, condition, holo pattern,
    edition and grading company. JSON needs their values, and a reader should see
    the same strings the database stores.
    """
    return value.value if hasattr(value, "value") else value


def canonical_json(payload: dict) -> bytes:
    """Bytes that depend on the content and nothing else."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def build_snapshot(conn, *, generated_at: str | None = None) -> dict:
    """The full projection: every lot, in stock or not.

    Sold and written-off lots are included — 510 of 1,479 on the real ledger —
    because "did I already sell this one?" is a question worth answering at a
    table, and the whole payload is 106 KB either way.
    """
    rows = InventoryService(conn).inventory_rows()
    return {
        "schema": SCHEMA,
        "ledger_id": AppMetaRepository(conn).ledger_id(),
        "generated_at": generated_at or now_iso(),
        "app_version": __version__,
        "counts": {
            "rows": len(rows),
            "in_stock": sum(1 for r in rows if r.in_stock),
            "units": sum(r.on_hand for r in rows if r.in_stock),
        },
        "rows": [_row(r) for r in rows],
    }


def _row(row) -> dict:
    """One lot, flattened. All money is integer cents, as it is everywhere."""
    out = {field: _plain(getattr(row.item, field)) for field in _ITEM_FIELDS}
    out.update(
        item_id=row.item.id,
        lot_id=row.lot_id,
        sku=row.sku,
        intent=row.intent,
        acquired_at=row.acquired_at,
        on_hand=row.on_hand,
        acquired_qty=row.acquired_qty,
        purchase_price_cents=row.purchase_price_cents,
        market_at_purchase_cents=row.market_at_purchase_cents,
        current_price_cents=row.current_price_cents,
        sold_price_cents=row.sold_price_cents,
        sold_status=row.sold_status,
        review=row.review,
        missing=row.missing,
    )
    return out


def content_hash(snapshot: dict) -> str:
    """Identity of the *content*, ignoring when it was generated.

    Two builds of an unchanged ledger hash the same, which is what lets a publish
    say "no change" instead of writing another object and ageing a good one out
    of retention.
    """
    body = {k: v for k, v in snapshot.items() if k != "generated_at"}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def encode(snapshot: dict) -> bytes:
    """Canonical JSON, gzipped reproducibly."""
    return gzip.compress(canonical_json(snapshot), 9, mtime=0)


def decode(blob: bytes) -> dict:
    """Inverse of :func:`encode` — used by the tests and by any verifier."""
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def write_snapshot(conn, path: str | Path) -> Path:
    """Build and write a snapshot to ``path``. The offline/AirDrop route.

    Unencrypted on purpose: this is a file the user chose a location for, on
    their own machine. Encryption exists for the copy that leaves it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(build_snapshot(conn)))
    return path
