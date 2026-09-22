"""The published inventory projection (headless, Qt-free).

The load-bearing test is ``test_the_payload_reproduces_inventory_rows``: the
snapshot must stay a faithful view of what the app itself shows, so if the
projection ever drifts from ``InventoryService`` this goes red.
"""

from __future__ import annotations

import gzip
import json

import pytest

from conftest import buy, sell
from tcginventory.data.repositories import AppMetaRepository, LotRepository
from tcginventory.domain.enums import Intent
from tcginventory.services import snapshot
from tcginventory.services.inventory import InventoryService

DAY = "2026-03-01T12:00:00+00:00"


def test_ledger_id_is_minted_and_travels_in_the_payload(conn):
    ledger_id = AppMetaRepository(conn).ledger_id()
    assert len(ledger_id) == 32
    assert snapshot.build_snapshot(conn)["ledger_id"] == ledger_id


def test_a_database_without_a_ledger_id_is_refused(conn):
    """Publishing under a blank prefix would collide with every other blank one,
    so an unmigrated or foreign database must fail loudly instead."""
    conn.execute("DELETE FROM app_meta WHERE key = 'ledger_id'")
    with pytest.raises(LookupError):
        snapshot.build_snapshot(conn)


def test_the_payload_reproduces_inventory_rows(seeded_conn):
    """The snapshot is a view of what the app shows. If the two ever disagree,
    the phone is quietly lying about the ledger."""
    rows = InventoryService(seeded_conn).inventory_rows()
    payload = snapshot.build_snapshot(seeded_conn)

    assert payload["counts"]["rows"] == len(rows)
    assert len(payload["rows"]) == len(rows)
    assert payload["counts"]["in_stock"] == sum(1 for r in rows if r.in_stock)

    by_lot = {r["lot_id"]: r for r in payload["rows"]}
    for row in rows:
        published = by_lot[row.lot_id]
        assert published["name"] == row.item.name
        assert published["purchase_price_cents"] == row.purchase_price_cents
        assert published["current_price_cents"] == row.current_price_cents
        assert published["on_hand"] == row.on_hand
        assert published["sold_status"] == row.sold_status
        assert published["intent"] == row.intent


def test_money_stays_integer_cents_through_the_round_trip(conn, svc):
    """Cents are ``int`` everywhere in this app; JSON must not turn one into a
    float on the way to the phone."""
    buy(svc, "Charizard", 12345, 15000, DAY, set_name="Base Set", card_number="4/102")

    payload = snapshot.decode(snapshot.encode(snapshot.build_snapshot(conn)))
    row = payload["rows"][0]
    for field in ("purchase_price_cents", "market_at_purchase_cents"):
        assert isinstance(row[field], int), f"{field} came back as {type(row[field])}"
    assert row["purchase_price_cents"] == 12345
    assert row["market_at_purchase_cents"] == 15000
    # Unpriced: no snapshot polled yet and not marked for review.
    assert row["current_price_cents"] is None


def test_a_priced_item_publishes_its_current_value_as_an_int(conn, svc):
    result = buy(svc, "Umbreon VMAX", 20000, 22000, DAY, set_name="Evolving Skies")
    item_id = result.lots[0].item_id
    # A reviewed item uses its manual value as the current price.
    conn.execute(
        "UPDATE item SET review = 1, market_value_cents = ? WHERE id = ?",
        (31500, item_id),
    )
    conn.commit()

    row = snapshot.decode(snapshot.encode(snapshot.build_snapshot(conn)))["rows"][0]
    assert isinstance(row["current_price_cents"], int)
    assert row["current_price_cents"] == 31500
    assert row["review"] is True


def test_enums_serialise_as_their_values_not_their_reprs(conn, svc):
    buy(svc, "Pikachu", 100, 100, DAY, set_name="Base Set", card_number="58/102")
    row = snapshot.build_snapshot(conn)["rows"][0]
    assert row["language"] == "EN"
    assert row["category"] == "single"
    # Nothing may serialise as an enum repr -- that would reach the phone as
    # "Language.EN" and never match anything.
    assert "Enum" not in json.dumps(row)
    assert "." not in str(row["language"])


def test_a_ripping_lot_publishes_at_zero(conn, svc):
    """Ripping lots are valued at $0 — the phone must show the same loss the
    desktop does, not a market price the card will never realise."""
    buy(
        svc,
        "Booster Box",
        50000,
        60000,
        DAY,
        intent=Intent.RIPPING,
        set_name="Evolving Skies",
    )
    row = snapshot.build_snapshot(conn)["rows"][0]
    assert row["intent"] == "ripping"
    assert row["current_price_cents"] == 0
    assert row["purchase_price_cents"] == 50000


def test_sold_lots_still_travel(conn, svc):
    """510 of 1,479 rows on the real ledger are sold or gone, and 'did I already
    sell this?' is worth answering at a table."""
    result = buy(svc, "Blastoise", 2000, 2400, DAY, quantity=2, set_name="Base Set")
    sell(svc, result.lots[0].item_id, 2, 3600, "2026-03-05T12:00:00+00:00")

    rows = snapshot.build_snapshot(conn)["rows"]
    assert len(rows) == 1
    assert rows[0]["sold_status"] == "sold"
    assert rows[0]["on_hand"] == 0
    assert rows[0]["sold_price_cents"] == 1800  # 3600 proceeds / 2 units


# --------------------------------------------------------------------------
# Determinism -- what makes "skip the publish when nothing changed" possible.
# --------------------------------------------------------------------------


def test_content_hash_ignores_the_timestamp(seeded_conn):
    a = snapshot.build_snapshot(seeded_conn, generated_at="2026-01-01T00:00:00+00:00")
    b = snapshot.build_snapshot(seeded_conn, generated_at="2026-09-10T12:00:00+00:00")
    assert a["generated_at"] != b["generated_at"]
    assert snapshot.content_hash(a) == snapshot.content_hash(b)


def test_content_hash_moves_when_the_ledger_moves(conn, svc):
    buy(svc, "Mewtwo", 500, 700, DAY, quantity=3, set_name="Base Set")
    before = snapshot.content_hash(snapshot.build_snapshot(conn))

    lot = LotRepository(conn).list_all()[0]
    conn.execute(
        "UPDATE lot SET intent = ? WHERE id = ?", (Intent.PERSONAL.value, lot.id)
    )
    conn.commit()

    assert snapshot.content_hash(snapshot.build_snapshot(conn)) != before


def test_encoding_is_byte_identical_for_identical_content(seeded_conn):
    """gzip stamps the current time into its header by default, which would make
    every publish differ and churn retention on runs that changed nothing."""
    stamp = "2026-09-10T12:00:00+00:00"
    first = snapshot.encode(snapshot.build_snapshot(seeded_conn, generated_at=stamp))
    second = snapshot.encode(snapshot.build_snapshot(seeded_conn, generated_at=stamp))
    assert first == second


def test_the_encoded_payload_is_gzip_and_round_trips(seeded_conn):
    payload = snapshot.build_snapshot(seeded_conn)
    blob = snapshot.encode(payload)
    assert blob[:2] == b"\x1f\x8b"  # gzip magic
    assert snapshot.decode(blob) == payload
    assert json.loads(gzip.decompress(blob))["schema"] == snapshot.SCHEMA


def test_canonical_json_is_key_order_independent():
    assert snapshot.canonical_json({"b": 1, "a": 2}) == snapshot.canonical_json(
        {"a": 2, "b": 1}
    )


def test_write_snapshot_creates_parents_and_writes_a_readable_file(
    seeded_conn, tmp_path
):
    out = tmp_path / "nested" / "dir" / "inventory.json.gz"
    written = snapshot.write_snapshot(seeded_conn, out)
    assert written == out and out.exists()
    assert snapshot.decode(out.read_bytes())["schema"] == snapshot.SCHEMA


def test_the_schema_version_is_pinned():
    """A reader keys off this. Changing the payload shape without bumping it is
    the bug this test exists to catch."""
    assert snapshot.SCHEMA == 1
    assert "created_at" not in snapshot._ITEM_FIELDS
    assert "updated_at" not in snapshot._ITEM_FIELDS
