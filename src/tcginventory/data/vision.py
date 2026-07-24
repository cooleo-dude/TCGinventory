"""Vision training corpus — a **separate** SQLite file (``vision.db``).

Foundation for the (future) card-recognition engine. The unit is a **capture**:
one purchase/auction's worth of photos plus the set of cards they collectively
document. A capture has:

* **images** — the one or more photos supplied for that entry (a photo is a
  *scene* that may show several cards), stored as content-addressed files on
  disk under ``<data_dir>/vision/images/``;
* **cards** — the identities/conditions the user assigned (the labels the engine
  learns to recognise against its catalog).

Grouping by capture keeps the several photos of a single lot associated, without
falsely asserting that every card appears in every photo. Kept out of the ledger
DB (bulky, derived ML data) so its exports/backups stay small — mirrors
``pricehistory.db`` / ``auctions.db``. Qt-free.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tcginventory import config

VISION_FILENAME = "vision.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capture (
    id         INTEGER PRIMARY KEY,
    source     TEXT,                      -- 'transaction' | 'auction' | 'manual'
    note       TEXT,
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS image (
    id         INTEGER PRIMARY KEY,
    capture_id INTEGER NOT NULL REFERENCES capture(id) ON DELETE CASCADE,
    sha256     TEXT    NOT NULL,          -- content hash (files dedup on disk)
    path       TEXT    NOT NULL,
    side       TEXT,                      -- 'front' | 'back' | NULL (unspecified)
    pair_group INTEGER,                   -- links a front+back of the same cards
    width      INTEGER,
    height     INTEGER,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_capture ON image(capture_id);
CREATE TABLE IF NOT EXISTS capture_card (
    id              INTEGER PRIMARY KEY,
    capture_id      INTEGER NOT NULL REFERENCES capture(id) ON DELETE CASCADE,
    card_id         TEXT,                 -- catalog id when the card was matched
    name            TEXT,
    set_name        TEXT,
    card_number     TEXT,
    language        TEXT,
    condition       TEXT,
    is_graded       INTEGER NOT NULL DEFAULT 0,
    grade           TEXT,
    grading_company TEXT,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capture_card_capture ON capture_card(capture_id);
CREATE INDEX IF NOT EXISTS idx_capture_card_card ON capture_card(card_id);
CREATE TABLE IF NOT EXISTS region (
    id              INTEGER PRIMARY KEY,
    image_id        INTEGER NOT NULL REFERENCES image(id) ON DELETE CASCADE,
    number          INTEGER NOT NULL,   -- the box's label on the canvas
    x               REAL    NOT NULL,   -- box geometry, normalised 0..1 of the
    y               REAL    NOT NULL,   -- image (resolution-independent)
    w               REAL    NOT NULL,
    h               REAL    NOT NULL,
    side            TEXT,               -- 'give' | 'receive' | NULL (listing)
    card_id         TEXT,
    name            TEXT,
    set_name        TEXT,
    card_number     TEXT,
    language        TEXT,
    condition       TEXT,
    is_graded       INTEGER NOT NULL DEFAULT 0,
    grade           TEXT,
    grading_company TEXT,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_region_image ON region(image_id);
CREATE INDEX IF NOT EXISTS idx_region_card ON region(card_id);
CREATE TABLE IF NOT EXISTS card_embedding (
    card_id       TEXT    NOT NULL,
    model_version TEXT    NOT NULL,      -- embedder identity; changing it re-embeds
    dim           INTEGER NOT NULL,
    vector        BLOB    NOT NULL,      -- float32 bytes, L2-normalised
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (card_id, model_version)
);
"""


def vision_path() -> Path:
    """The vision DB, kept beside the main database file."""
    return config.db_path().parent / VISION_FILENAME


def open_vision(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (and initialise) the vision database (``":memory:"`` for tests)."""
    target = ":memory:" if path == ":memory:" else str(path or vision_path())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_legacy(conn)
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a capture-schema DB was first created."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(image)")}
    if "side" not in cols:
        conn.execute("ALTER TABLE image ADD COLUMN side TEXT")
    if "pair_group" not in cols:
        conn.execute("ALTER TABLE image ADD COLUMN pair_group INTEGER")


def _reset_legacy(conn: sqlite3.Connection) -> None:
    """Drop the pre-capture experimental schema (per-image labels) if present.

    The vision corpus is new/experimental and the ingestion flow is still being
    shaped; a stale ``image`` table without ``capture_id`` is thrown away rather
    than migrated.
    """
    has_image = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='image'"
    ).fetchone()
    if not has_image:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(image)")}
    if "capture_id" not in cols:
        conn.executescript(
            "DROP TABLE IF EXISTS image_label; DROP TABLE IF EXISTS image;"
        )


@dataclass
class CardLabel:
    """A card in a capture (its assigned identity/condition — the ground truth)."""

    card_id: str | None = None
    name: str | None = None
    set_name: str | None = None
    card_number: str | None = None
    language: str | None = None
    condition: str | None = None
    is_graded: bool = False
    grade: str | None = None
    grading_company: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class VisionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_capture(
        self, source: str | None = None, note: str | None = None, now: str | None = None
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO capture (source, note, created_at) VALUES (?,?,?)",
                (source, note, now or _now()),
            )
        return int(cur.lastrowid)

    def add_image(
        self,
        capture_id: int,
        sha256: str,
        path: str,
        side: str | None = None,
        pair_group: int | None = None,
        width: int | None = None,
        height: int | None = None,
        now: str | None = None,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO image (capture_id, sha256, path, side, pair_group, "
                "width, height, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    capture_id,
                    sha256,
                    path,
                    side,
                    pair_group,
                    width,
                    height,
                    now or _now(),
                ),
            )
        return int(cur.lastrowid)

    def add_capture_card(
        self, capture_id: int, card: CardLabel, now: str | None = None
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO capture_card (capture_id, card_id, name, set_name, "
                "card_number, language, condition, is_graded, grade, "
                "grading_company, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    capture_id,
                    card.card_id,
                    card.name,
                    card.set_name,
                    card.card_number,
                    card.language,
                    card.condition,
                    1 if card.is_graded else 0,
                    card.grade,
                    card.grading_company,
                    now or _now(),
                ),
            )
        return int(cur.lastrowid)

    def add_region(
        self,
        image_id: int,
        number: int,
        box: tuple[float, float, float, float],
        side: str | None,
        card: CardLabel,
        now: str | None = None,
    ) -> int:
        """Record a drawn box: ``box`` = (x, y, w, h) normalised to 0..1."""
        x, y, w, h = box
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO region (image_id, number, x, y, w, h, side, card_id, "
                "name, set_name, card_number, language, condition, is_graded, "
                "grade, grading_company, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    image_id,
                    number,
                    x,
                    y,
                    w,
                    h,
                    side,
                    card.card_id,
                    card.name,
                    card.set_name,
                    card.card_number,
                    card.language,
                    card.condition,
                    1 if card.is_graded else 0,
                    card.grade,
                    card.grading_company,
                    now or _now(),
                ),
            )
        return int(cur.lastrowid)

    def region_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM region").fetchone()[0]

    def labeled_card_ids(self) -> list[str]:
        """Distinct catalog ids that appear in saved regions — the growing pool
        of cards the identity matcher can retrieve against (active learning:
        the more you label, the richer the candidate set)."""
        rows = self.conn.execute(
            "SELECT DISTINCT card_id FROM region WHERE card_id IS NOT NULL "
            "ORDER BY card_id"
        ).fetchall()
        return [r["card_id"] for r in rows]

    # -- reference-embedding cache (bytes in/out; numpy stays in the service) --

    def get_embedding(self, card_id: str, model_version: str) -> bytes | None:
        row = self.conn.execute(
            "SELECT vector FROM card_embedding WHERE card_id = ? AND "
            "model_version = ?",
            (card_id, model_version),
        ).fetchone()
        return bytes(row["vector"]) if row is not None else None

    def put_embedding(
        self,
        card_id: str,
        model_version: str,
        dim: int,
        vector: bytes,
        now: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO card_embedding (card_id, model_version, dim, vector, "
                "created_at) VALUES (?,?,?,?,?) ON CONFLICT(card_id, model_version) "
                "DO UPDATE SET dim = excluded.dim, vector = excluded.vector",
                (card_id, model_version, dim, vector, now or _now()),
            )

    def embedding_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM card_embedding").fetchone()[0]

    def regions_for_image(self, image_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM region WHERE image_id = ? ORDER BY number", (image_id,)
        ).fetchall()

    def capture_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM capture").fetchone()[0]

    def image_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM image").fetchone()[0]

    def capture_card_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM capture_card").fetchone()[0]

    def captures_with_card(self, card_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(DISTINCT capture_id) FROM capture_card WHERE card_id = ?",
            (card_id,),
        ).fetchone()[0]
