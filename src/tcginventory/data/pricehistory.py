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
