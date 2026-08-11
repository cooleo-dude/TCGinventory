"""Catalog-wide market-price history — a **separate** SQLite file.

Kept out of the main ledger DB (``inventory.db``) on purpose: this is bulky,
re-derivable market data, so isolating it means the ledger's auto-backup stays
small/fast and this history is backed up on its own (trimmed) policy.

Three tables:

* ``catalog_price_snapshot`` — append-only change points (dedup-on-change: a row
  is written only when a card's price differs from its last recorded value).
* ``catalog_price_latest`` — one row per card with its current price (fast
  lookup + the dedup comparison).
* ``set_state`` — when each set was last swept (drives the owned-first rotation
  and lets dedup not stall the round-robin).

Qt-free; date-only ``as_of`` (daily resolution) keeps rows compact.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tcginventory import config

HISTORY_FILENAME = "pricehistory.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_price_snapshot (
    id          INTEGER PRIMARY KEY,
    card_id     TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'tcgcsv',
    price_cents INTEGER NOT NULL,
    currency    TEXT    NOT NULL DEFAULT 'USD',
    as_of       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cps_card
    ON catalog_price_snapshot(card_id, as_of);

CREATE TABLE IF NOT EXISTS catalog_price_latest (
    card_id     TEXT    PRIMARY KEY,
    price_cents INTEGER NOT NULL,
    as_of       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS set_state (
    set_key        TEXT PRIMARY KEY,
    last_priced_at TEXT
);
"""


def history_path() -> Path:
    """The price-history DB, kept beside the main database file."""
    return config.db_path().parent / HISTORY_FILENAME


def open_history(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (and initialise) the price-history database."""
    target = ":memory:" if path == ":memory:" else str(path or history_path())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    return conn


class PriceHistoryRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(
        self, card_id: str, price_cents: int, as_of: str, source: str = "tcgcsv"
    ) -> bool:
        """Upsert the card's current price; append a history row only if it
        changed. Returns True when a new snapshot was written."""
        prev = self.conn.execute(
            "SELECT price_cents FROM catalog_price_latest WHERE card_id = ?",
            (card_id,),
        ).fetchone()
        changed = prev is None or prev["price_cents"] != price_cents
        self.conn.execute(
            "INSERT INTO catalog_price_latest (card_id, price_cents, as_of) "
            "VALUES (?,?,?) ON CONFLICT(card_id) DO UPDATE SET "
            "price_cents = excluded.price_cents, as_of = excluded.as_of",
            (card_id, price_cents, as_of),
        )
        if changed:
            self.conn.execute(
                "INSERT INTO catalog_price_snapshot "
                "(card_id, source, price_cents, as_of) VALUES (?,?,?,?)",
                (card_id, source, price_cents, as_of),
            )
        return changed

    def current_price(self, card_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT price_cents FROM catalog_price_latest WHERE card_id = ?",
            (card_id,),
        ).fetchone()
        return row["price_cents"] if row else None

    def history_for_card(
        self, card_id: str, since: str | None = None
    ) -> list[tuple[str, int]]:
        """``(as_of, price_cents)`` change points for a card, oldest first."""
        if since:
            rows = self.conn.execute(
                "SELECT as_of, price_cents FROM catalog_price_snapshot "
                "WHERE card_id = ? AND as_of >= ? ORDER BY as_of",
                (card_id, since),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT as_of, price_cents FROM catalog_price_snapshot "
                "WHERE card_id = ? ORDER BY as_of",
                (card_id,),
            ).fetchall()
        return [(r["as_of"], r["price_cents"]) for r in rows]

    def movers_since(
        self,
        cutoff: str | None,
        *,
        floor_cents: int = 0,
        min_points: int = 2,
        max_step_ratio: float = 5.0,
        limit: int | None = None,
    ) -> list[tuple[str, int, int, float]]:
        """Catalog-wide movement: ``(card_id, price_now, change_cents, change_pct)``.

        The first aggregate over this file — everything else here reads one card at a
        time, which is why ranking the catalog needed a new query rather than a new
        call site.

        Movement is endpoint-to-endpoint: the current price against the card's price
        at the window start. ``cutoff`` is a date string (``None`` = all recorded
        history, which uses each card's earliest snapshot as the baseline).

        Two filters, both load-bearing rather than tidiness:

        * ``floor_cents`` drops cheap cards. Ranking is by **percent**, so without a
          floor the list is dominated by commons where a 15c to 30c tick is +100%.
        * ``min_points`` requires that many recorded change points, so a card seeded
          once by the sweep and never seen to move is not reported as flat-then-jumped.
          Remember the table is **dedup-on-change**: a card whose price never moved has
          exactly one row, which is precisely what this excludes.
        * ``max_step_ratio`` drops a card whose series contains a single jump of that
          factor or more between adjacent observations. **This is the one that makes
          the panel usable**, and it is a data-quality guard rather than a market
          judgement. Measured on the real file, the top of a raw percent ranking was
          entirely cards like ``ex7-36`` (271.64, 294.97, then **6.34** and flat after)
          and ``basep-17`` (1.00, 0.99, then **111.30** and flat after) — the sweep's
          card-to-pricing-product mapping changing mid-series, in batches of ~52 cards
          on 2026-07-28 and 2026-08-04. Such a series is not one product's price
          history, so no window over it is meaningful. A genuine move is distributed
          across observations; a relabel is one step that then holds, which is also
          why a least-squares slope would not have caught it.

        Sorted by percent descending, so gainers are the head and losers the tail; the
        caller splits and slices. ``limit`` caps each end, applied after sorting.
        """
        # The baseline is the latest snapshot on or before the cutoff — and its
        # earliest snapshot otherwise, which is the part that matters. Catalog history
        # only reaches back to 2026-07-16, so *every* range pill asks for a cutoff
        # older than the data: without the fallback the honest answer is "no baseline"
        # and the panel renders empty at every range, which is what measuring caught.
        # ReportingService._baseline_prices does the same thing in Python
        # (`baseline.setdefault(...)` over `_earliest_prices()`); here one conditional
        # aggregate does it in the group, since MAX over a CASE is NULL when no row
        # qualifies and COALESCE then takes the earliest.
        if cutoff is None:
            pick, params = "MIN(as_of)", ()
        else:
            pick, params = (
                "COALESCE(MAX(CASE WHEN as_of <= ? THEN as_of END), MIN(as_of))",
                (cutoff,),
            )
        baseline_sql = (
            "SELECT s.card_id AS card_id, s.price_cents AS price "
            "FROM catalog_price_snapshot s JOIN ("
            f"SELECT card_id, {pick} m FROM catalog_price_snapshot GROUP BY card_id"
            ") t ON t.card_id = s.card_id AND t.m = s.as_of"
        )

        # Cards with a relabel step anywhere in their series, excluded wholesale: the
        # series describes two different products, so no window over it is a price
        # history. LAG needs SQLite 3.25+, which every interpreter this ships on has.
        relabelled = (
            "SELECT DISTINCT card_id FROM (SELECT card_id, price_cents AS cur, "
            "LAG(price_cents) OVER (PARTITION BY card_id ORDER BY as_of) AS prev "
            "FROM catalog_price_snapshot) WHERE prev IS NOT NULL AND prev > 0 "
            "AND (cur * 1.0 / prev >= ? OR prev * 1.0 / cur >= ?)"
        )
        sql = (
            "SELECT l.card_id AS card_id, l.price_cents AS now, b.price AS base "
            "FROM catalog_price_latest l "
            f"JOIN ({baseline_sql}) b ON b.card_id = l.card_id "
            "JOIN (SELECT card_id, COUNT(*) n FROM catalog_price_snapshot "
            "GROUP BY card_id) c ON c.card_id = l.card_id "
            f"WHERE l.card_id NOT IN ({relabelled}) "
            # The floor applies to **both** endpoints. Flooring only the current price
            # let `basep-41` rank first at +493,619% — 26c to $1283.67 — because the
            # cheap end was the bad reading. A real move on a dollar card has a dollar
            # baseline too.
            "AND l.price_cents >= ? AND b.price >= ? AND b.price > 0 AND c.n >= ? "
            "AND l.price_cents != b.price"
        )
        rows = self.conn.execute(
            sql,
            (
                *params,
                max_step_ratio,
                max_step_ratio,
                floor_cents,
                floor_cents,
                min_points,
            ),
        ).fetchall()

        out = [
            (
                r["card_id"],
                r["now"],
                r["now"] - r["base"],
                100.0 * (r["now"] - r["base"]) / r["base"],
            )
            for r in rows
        ]
        out.sort(key=lambda m: m[3], reverse=True)
        if limit is None:
            return out
        # Both ends of one sorted list: the biggest risers and the biggest fallers.
        # Slicing the middle out would drop losers when the catalog is mostly rising.
        if len(out) <= 2 * limit:
            return out
        return out[:limit] + out[-limit:]

    def touch_set(self, set_key: str, as_of: str) -> None:
        self.conn.execute(
            "INSERT INTO set_state (set_key, last_priced_at) VALUES (?,?) "
            "ON CONFLICT(set_key) DO UPDATE SET "
            "last_priced_at = excluded.last_priced_at",
            (set_key, as_of),
        )

    def set_last_priced(self) -> dict[str, str]:
        return {
            r["set_key"]: r["last_priced_at"]
            for r in self.conn.execute(
                "SELECT set_key, last_priced_at FROM set_state"
            ).fetchall()
        }

    def prune(self, before: str) -> int:
        """Delete snapshots older than ``before`` (date str). Latest is kept."""
        cur = self.conn.execute(
            "DELETE FROM catalog_price_snapshot WHERE as_of < ?", (before,)
        )
        return cur.rowcount

    def snapshot_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM catalog_price_snapshot"
        ).fetchone()[0]
