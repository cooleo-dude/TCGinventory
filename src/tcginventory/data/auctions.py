"""eBay auction watchlist — a **separate**, ephemeral SQLite file (``auctions.db``).

Deliberately kept out of the main ledger DB (``inventory.db``): watched auctions
are short-lived working data (you delete them once the auction ends), not part
of the inventory ledger. Isolating them in their own file means they never ride
along in the ledger's CSV export / ``import_all`` / audit trail / keep-10
backup. The data still **persists across sessions** and is removed only when the
user deletes it — nothing prunes it automatically.

Qt-free; three tables: ``auction``, its child ``auction_card``, and the
``auction_event`` lifecycle log that backs the History window's Auction
History pane (and makes a delete reversible).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
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
CREATE TABLE IF NOT EXISTS auction_event (
    id             INTEGER PRIMARY KEY,
    kind           TEXT    NOT NULL,
    auction_id     INTEGER,
    title          TEXT    NOT NULL DEFAULT '',
    card_count     INTEGER NOT NULL DEFAULT 0,
    value_cents    INTEGER NOT NULL DEFAULT 0,
    transaction_id INTEGER,
    snapshot       TEXT,
    restored_at    TEXT,
    restored_id    INTEGER,
    occurred_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auction_event_time
    ON auction_event(occurred_at DESC, id DESC);
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
    # Full-parity card fields (added later so the auction can export to a purchase).
    _parity_cols = {
        "category": "TEXT NOT NULL DEFAULT 'single'",
        "condition": "TEXT",
        "holo_pattern": "TEXT",
        "edition": "TEXT",
        "is_graded": "INTEGER NOT NULL DEFAULT 0",
        "grading_company": "TEXT",
        "grade": "TEXT",
        "cert_number": "TEXT",
        "ebay_url": "TEXT",
        "notes": "TEXT",
        "intent": "TEXT NOT NULL DEFAULT 'resale'",
        "review": "INTEGER NOT NULL DEFAULT 0",
        "market_is_manual": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, decl in _parity_cols.items():
        if card_cols and name not in card_cols:
            conn.execute(f"ALTER TABLE auction_card ADD COLUMN {name} {decl}")
    auction_cols = {r[1] for r in conn.execute("PRAGMA table_info(auction)")}
    if auction_cols and "ends_at" not in auction_cols:
        conn.execute("ALTER TABLE auction ADD COLUMN ends_at TEXT")
    if auction_cols and "archived" not in auction_cols:
        conn.execute(
            "ALTER TABLE auction ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
        )


@dataclass
class AuctionCard:
    """One card bundled inside a watched auction (a scratch estimate, not a lot).

    Carries the **full** transaction card field set so a saved/archived auction
    can be exported straight into the Transaction window as a purchase.
    """

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
    # Full parity with a Transaction incoming card (stored as enum .value strings).
    category: str = "single"
    condition: str | None = None
    holo_pattern: str | None = None
    edition: str | None = None
    is_graded: bool = False
    grading_company: str | None = None
    grade: str | None = None
    cert_number: str | None = None
    ebay_url: str | None = None
    notes: str | None = None
    intent: str = "resale"
    review: bool = False
    market_is_manual: bool = False  # Market hand-edited off the auto price
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
    archived: bool = False  # saved for later (hidden from the active watchlist)
    cards: list[AuctionCard] = field(default_factory=list)
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def value_cents(self) -> int:
        """Total market value of the bundled cards (value x quantity)."""
        return sum(c.market_value_cents * c.quantity for c in self.cards)


# The auction lifecycle events the History window's Auction History pane logs.
# ``deleted`` is the only kind that carries a snapshot, because it is the only
# one that destroys data — archiving is already reversible in place.
EVENT_DELETED = "deleted"
EVENT_ARCHIVED = "archived"
EVENT_UNARCHIVED = "unarchived"
EVENT_WON = "won"
EVENT_KINDS = (EVENT_DELETED, EVENT_ARCHIVED, EVENT_UNARCHIVED, EVENT_WON)


@dataclass
class AuctionEvent:
    """One thing that happened to a watched auction.

    Self-describing on purpose: ``title`` / ``card_count`` / ``value_cents`` are
    copied in at the time, so the row still reads correctly after the auction it
    refers to is gone.

    ``transaction_id`` (``won`` only) points into **inventory.db** and is a loose
    display pointer, not a foreign key — the ledger may since have voided that
    transaction, or been restored from a backup where the id means something else.
    Never resolve it as if it must exist.
    """

    kind: str
    occurred_at: str
    auction_id: int | None = None
    title: str = ""
    card_count: int = 0
    value_cents: int = 0
    transaction_id: int | None = None
    snapshot: str | None = None  # deleted only: JSON of the auction + its cards
    restored_at: str | None = None  # set when a restore consumed this event
    restored_id: int | None = None  # the *new* auction id a restore minted
    id: int | None = None

    @property
    def restorable(self) -> bool:
        """A delete that still has its payload and has not been undone yet."""
        return (
            self.kind == EVENT_DELETED
            and self.snapshot is not None
            and self.restored_at is None
        )

    def snapshot_cards(self) -> list[AuctionCard]:
        """The cards this event is holding, for a *preview* of what a restore
        would bring back. Empty for every kind but ``deleted``.

        Lives here rather than in the view so the stored payload is decoded in
        one place — the UI never parses the snapshot itself.
        """
        if not self.snapshot:
            return []
        payload = json.loads(self.snapshot)
        return [
            AuctionCard(**{k: v for k, v in c.items() if k != "id"})
            for c in payload.get("cards", [])
        ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _event_insert(
    conn: sqlite3.Connection,
    kind: str,
    auction: Auction,
    *,
    transaction_id: int | None = None,
    snapshot: str | None = None,
    now: str | None = None,
) -> int:
    """Append one lifecycle row. **Does not open a transaction** — the caller
    holds one, so the event and the change it describes commit together."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown auction event kind: {kind!r}")
    cur = conn.execute(
        "INSERT INTO auction_event (kind, auction_id, title, card_count, "
        "value_cents, transaction_id, snapshot, occurred_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            kind,
            auction.id,
            auction.title,
            sum(c.quantity for c in auction.cards),
            auction.value_cents,
            transaction_id,
            snapshot,
            now or _now(),
        ),
    )
    return int(cur.lastrowid)


class AuctionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def list_all(self, archived: bool | None = False) -> list[Auction]:
        """Watched auctions, newest first. ``archived`` filters by the flag
        (``False`` = active watchlist, ``True`` = archived, ``None`` = both).

        Watchlist display order (soonest-ending first, ended sink to the bottom)
        is applied in the view, since "ended" depends on the current time.
        """
        if archived is None:
            rows = self.conn.execute(
                "SELECT * FROM auction ORDER BY created_at DESC, id DESC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM auction WHERE archived = ? "
                "ORDER BY created_at DESC, id DESC",
                (1 if archived else 0,),
            ).fetchall()
        return [self._load(r) for r in rows]

    def set_archived(self, auction_id: int, archived: bool) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE auction SET archived = ? WHERE id = ?",
                (1 if archived else 0, auction_id),
            )

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
                category=c["category"],
                condition=c["condition"],
                holo_pattern=c["holo_pattern"],
                edition=c["edition"],
                is_graded=bool(c["is_graded"]),
                grading_company=c["grading_company"],
                grade=c["grade"],
                cert_number=c["cert_number"],
                ebay_url=c["ebay_url"],
                notes=c["notes"],
                intent=c["intent"],
                review=bool(c["review"]),
                market_is_manual=bool(c["market_is_manual"]),
            )
            for c in self.conn.execute(
                "SELECT * FROM auction_card WHERE auction_id = ? ORDER BY position, id",
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
            archived=bool(row["archived"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            cards=cards,
        )

    def save(self, auction: Auction, now: str | None = None) -> Auction:
        """Insert or update the auction and replace its card rows (one txn).

        Mutates and returns ``auction`` with its ``id`` / timestamps populated.
        """
        with self.conn:
            return self._save_locked(auction, now)

    def _save_locked(self, auction: Auction, now: str | None = None) -> Auction:
        """``save`` without opening its own transaction — the caller holds one.

        Lets ``AuctionEventRepository.restore`` re-insert a deleted auction and
        stamp its event row as consumed inside a single transaction, so a crash
        can never leave the auction back on the watchlist *and* its event still
        offering another restore.
        """
        now = now or _now()
        if auction.id is None:
            cur = self.conn.execute(
                "INSERT INTO auction (title, url, shipping_cents, "
                "tax_rate_bps, ends_at, notes, archived, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    auction.title,
                    auction.url,
                    auction.shipping_cents,
                    auction.tax_rate_bps,
                    auction.ends_at,
                    auction.notes,
                    1 if auction.archived else 0,
                    now,
                    now,
                ),
            )
            auction.id = int(cur.lastrowid)
            auction.created_at = now
        else:
            self.conn.execute(
                "UPDATE auction SET title=?, url=?, shipping_cents=?, "
                "tax_rate_bps=?, ends_at=?, notes=?, archived=?, updated_at=? "
                "WHERE id=?",
                (
                    auction.title,
                    auction.url,
                    auction.shipping_cents,
                    auction.tax_rate_bps,
                    auction.ends_at,
                    auction.notes,
                    1 if auction.archived else 0,
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
                "market_value_cents, image_url, tcgplayer_url, category, "
                "condition, holo_pattern, edition, is_graded, grading_company, "
                "grade, cert_number, ebay_url, notes, intent, review, "
                "market_is_manual, position) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    card.category,
                    card.condition,
                    card.holo_pattern,
                    card.edition,
                    1 if card.is_graded else 0,
                    card.grading_company,
                    card.grade,
                    card.cert_number,
                    card.ebay_url,
                    card.notes,
                    card.intent,
                    1 if card.review else 0,
                    1 if card.market_is_manual else 0,
                    pos,
                ),
            )
        return auction

    def delete(self, auction_id: int) -> None:
        """Remove a watchlist entry, logging a restorable ``deleted`` event.

        The snapshot is taken **before** the DELETE and inside the same
        transaction: ``auction_card`` rows go with the parent via ON DELETE
        CASCADE, so once the statement runs the payload is unrecoverable.
        """
        auction = self.get(auction_id)
        if auction is None:
            return
        payload = json.dumps(asdict(auction))
        with self.conn:
            _event_insert(self.conn, EVENT_DELETED, auction, snapshot=payload)
            self.conn.execute("DELETE FROM auction WHERE id = ?", (auction_id,))


class AuctionEventRepository:
    """The auction lifecycle log — append-only apart from the restore stamp."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(
        self,
        kind: str,
        auction: Auction,
        *,
        transaction_id: int | None = None,
        now: str | None = None,
    ) -> int:
        """Log a non-destructive event (archive / unarchive / won).

        Deletes are logged by ``AuctionRepository.delete`` instead, which is the
        only place that can snapshot the auction before it is gone.
        """
        if kind == EVENT_DELETED:
            raise ValueError("a deleted event is written by AuctionRepository.delete")
        with self.conn:
            return _event_insert(
                self.conn, kind, auction, transaction_id=transaction_id, now=now
            )

    def list_all(self) -> list[AuctionEvent]:
        """Every event, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM auction_event ORDER BY occurred_at DESC, id DESC"
        ).fetchall()
        return [self._load(r) for r in rows]

    def get(self, event_id: int) -> AuctionEvent | None:
        row = self.conn.execute(
            "SELECT * FROM auction_event WHERE id = ?", (event_id,)
        ).fetchone()
        return self._load(row) if row else None

    @staticmethod
    def _load(row: sqlite3.Row) -> AuctionEvent:
        return AuctionEvent(
            id=row["id"],
            kind=row["kind"],
            auction_id=row["auction_id"],
            title=row["title"],
            card_count=row["card_count"],
            value_cents=row["value_cents"],
            transaction_id=row["transaction_id"],
            snapshot=row["snapshot"],
            restored_at=row["restored_at"],
            restored_id=row["restored_id"],
            occurred_at=row["occurred_at"],
        )

    def restore(self, event_id: int, now: str | None = None) -> Auction:
        """Put a deleted auction back on the watchlist from its snapshot.

        The event row is **kept** and stamped consumed rather than removed —
        Auction History never loses a row, and the stamp is what stops a second
        restore minting a duplicate.

        The restored auction gets a **new** id (the old one may since have been
        reissued), recorded on the event as ``restored_id``.

        Raises ``ValueError`` if the event is not a restorable delete.
        """
        event = self.get(event_id)
        if event is None:
            raise ValueError(f"no such auction event: {event_id}")
        if event.kind != EVENT_DELETED:
            raise ValueError(f"only a deleted event restores, not {event.kind!r}")
        if event.restored_at is not None:
            raise ValueError(f"auction event {event_id} was already restored")
        if event.snapshot is None:
            raise ValueError(f"auction event {event_id} carries no snapshot")

        payload = json.loads(event.snapshot)
        cards = [
            AuctionCard(**{k: v for k, v in c.items() if k != "id"})
            for c in payload.pop("cards", [])
        ]
        # Drop the identity/timestamp fields: this is a fresh row, and pinning the
        # old id would collide with whatever has taken it since.
        for key in ("id", "created_at", "updated_at"):
            payload.pop(key, None)
        auction = Auction(cards=cards, **payload)

        stamp = now or _now()
        with self.conn:
            AuctionRepository(self.conn)._save_locked(auction, stamp)
            self.conn.execute(
                "UPDATE auction_event SET restored_at = ?, restored_id = ? "
                "WHERE id = ?",
                (stamp, auction.id, event_id),
            )
        return auction
