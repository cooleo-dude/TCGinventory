"""eBay auction watchlist — a **separate**, ephemeral SQLite file (``auctions.db``).

Deliberately kept out of the main ledger DB (``inventory.db``): watched auctions
are short-lived working data (you delete them once the auction ends), not part
of the inventory ledger. Isolating them in their own file means they never ride
along in the ledger's CSV export / ``import_all`` / audit trail / keep-10
backup. The data still **persists across sessions** and is removed only when the
user deletes it — nothing prunes it automatically.

Qt-free; two tables (``auction`` and its child ``auction_card``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tcginventory import config
from tcginventory.services.auctions import DEFAULT_TAX_RATE_BPS

AUCTIONS_FILENAME = "auctions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS auction (
    id             INTEGER PRIMARY KEY,
    title          TEXT    NOT NULL DEFAULT '',
    url            TEXT,
    shipping_cents INTEGER NOT NULL DEFAULT 0,
    tax_rate_bps   INTEGER NOT NULL DEFAULT 600,
    ends_at        TEXT,
    notes          TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS auction_card (
    id                 INTEGER PRIMARY KEY,
    auction_id         INTEGER NOT NULL REFERENCES auction(id) ON DELETE CASCADE,
    card_id            TEXT,
    name               TEXT    NOT NULL,
    set_name           TEXT,
    set_code           TEXT,
    number             TEXT,
    language           TEXT    NOT NULL DEFAULT 'EN',
    quantity           INTEGER NOT NULL DEFAULT 1,
    market_value_cents INTEGER NOT NULL DEFAULT 0,
    image_url          TEXT,
    tcgplayer_url      TEXT,
    position           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_auction_card ON auction_card(auction_id);
"""


def auctions_path() -> Path:
    """The auctions DB, kept beside the main database file."""
    return config.db_path().parent / AUCTIONS_FILENAME


def open_auctions(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (and initialise) the auctions database.

    Pass ``":memory:"`` for an isolated in-memory DB (tests / an in-memory main
    database).
    """
    target = ":memory:" if path == ":memory:" else str(path or auctions_path())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")  # cascade auction_card on delete
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created (forward-only).

    ``CREATE TABLE IF NOT EXISTS`` won't touch an existing table, so new columns
    are added here with an idempotent ``ALTER``.
    """
    # A missing table yields an empty column set (PRAGMA doesn't error) — the
    # `cols and` guard then skips its ALTER rather than failing on a bad table.
    card_cols = {r[1] for r in conn.execute("PRAGMA table_info(auction_card)")}
    if card_cols and "tcgplayer_url" not in card_cols:
        conn.execute("ALTER TABLE auction_card ADD COLUMN tcgplayer_url TEXT")
    auction_cols = {r[1] for r in conn.execute("PRAGMA table_info(auction)")}
    if auction_cols and "ends_at" not in auction_cols:
        conn.execute("ALTER TABLE auction ADD COLUMN ends_at TEXT")


@dataclass
class AuctionCard:
    """One card bundled inside a watched auction (a scratch estimate, not a lot)."""

    name: str
    card_id: str | None = None  # catalog id (drives image + price refresh)
    set_name: str | None = None
    set_code: str | None = None
    number: str | None = None
    language: str = "EN"
    quantity: int = 1
    market_value_cents: int = 0
    image_url: str | None = None
    tcgplayer_url: str | None = None
    id: int | None = None


@dataclass
class Auction:
    """A watched eBay auction: a bundle of cards plus shipping + tax terms."""

    title: str = ""
    url: str | None = None
    shipping_cents: int = 0
    tax_rate_bps: int = DEFAULT_TAX_RATE_BPS
    ends_at: str | None = None  # ISO local datetime the auction ends (for countdown)
    notes: str | None = None
    cards: list[AuctionCard] = field(default_factory=list)
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def value_cents(self) -> int:
        """Total market value of the bundled cards (value x quantity)."""
        return sum(c.market_value_cents * c.quantity for c in self.cards)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuctionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def list_all(self) -> list[Auction]:
        """Every watched auction, newest first.

        Watchlist display order (soonest-ending first, ended sink to the bottom)
        is applied in the view, since "ended" depends on the current time.
        """
        rows = self.conn.execute(
            "SELECT * FROM auction ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [self._load(r) for r in rows]

    def get(self, auction_id: int) -> Auction | None:
        row = self.conn.execute(
            "SELECT * FROM auction WHERE id = ?", (auction_id,)
        ).fetchone()
        return self._load(row) if row else None

    def _load(self, row: sqlite3.Row) -> Auction:
        cards = [
            AuctionCard(
                id=c["id"],
                card_id=c["card_id"],
                name=c["name"],
                set_name=c["set_name"],
                set_code=c["set_code"],
                number=c["number"],
                language=c["language"],
                quantity=c["quantity"],
                market_value_cents=c["market_value_cents"],
                image_url=c["image_url"],
                tcgplayer_url=c["tcgplayer_url"],
            )
            for c in self.conn.execute(
                "SELECT * FROM auction_card WHERE auction_id = ? "
                "ORDER BY position, id",
                (row["id"],),
            ).fetchall()
        ]
        return Auction(
            id=row["id"],
            title=row["title"],
            url=row["url"],
            shipping_cents=row["shipping_cents"],
            tax_rate_bps=row["tax_rate_bps"],
            ends_at=row["ends_at"],
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            cards=cards,
        )

    def save(self, auction: Auction, now: str | None = None) -> Auction:
        """Insert or update the auction and replace its card rows (one txn).

        Mutates and returns ``auction`` with its ``id`` / timestamps populated.
        """
        now = now or _now()
        with self.conn:
            if auction.id is None:
                cur = self.conn.execute(
                    "INSERT INTO auction (title, url, shipping_cents, "
                    "tax_rate_bps, ends_at, notes, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        auction.title,
                        auction.url,
                        auction.shipping_cents,
                        auction.tax_rate_bps,
                        auction.ends_at,
                        auction.notes,
                        now,
                        now,
                    ),
                )
                auction.id = int(cur.lastrowid)
                auction.created_at = now
            else:
                self.conn.execute(
                    "UPDATE auction SET title=?, url=?, shipping_cents=?, "
                    "tax_rate_bps=?, ends_at=?, notes=?, updated_at=? WHERE id=?",
                    (
                        auction.title,
                        auction.url,
                        auction.shipping_cents,
                        auction.tax_rate_bps,
                        auction.ends_at,
                        auction.notes,
                        now,
                        auction.id,
                    ),
                )
            auction.updated_at = now
            # Replace the whole card set — the editor always saves the full list.
            self.conn.execute(
                "DELETE FROM auction_card WHERE auction_id = ?", (auction.id,)
            )
            for pos, card in enumerate(auction.cards):
                self.conn.execute(
                    "INSERT INTO auction_card (auction_id, card_id, name, "
                    "set_name, set_code, number, language, quantity, "
                    "market_value_cents, image_url, tcgplayer_url, position) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        auction.id,
                        card.card_id,
                        card.name,
                        card.set_name,
                        card.set_code,
                        card.number,
                        card.language,
                        card.quantity,
                        card.market_value_cents,
                        card.image_url,
                        card.tcgplayer_url,
                        pos,
                    ),
                )
        return auction

    def delete(self, auction_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM auction WHERE id = ?", (auction_id,))
