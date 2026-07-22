"""Vision training corpus — a **separate** SQLite file (``vision.db``).

Foundation for the (future) card-recognition engine: user-supplied card photos
are stored as **content-addressed files on disk** (``<data_dir>/vision/images/``)
with metadata + a **label** (the identity/condition the user assigned to the
card) recorded here. A photo plus its label is one training example.

Kept out of the ledger DB on purpose — it's bulky, derived/ML data — so the
ledger's exports and keep-10 backup stay small (mirrors ``pricehistory.db`` /
``auctions.db``). Qt-free.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tcginventory import config

VISION_FILENAME = "vision.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS image (
    id         INTEGER PRIMARY KEY,
    sha256     TEXT    NOT NULL UNIQUE,   -- content hash → dedups identical files
    path       TEXT    NOT NULL,          -- on-disk location
    source     TEXT,                      -- 'transaction' | 'auction' | 'manual'
    width      INTEGER,
    height     INTEGER,
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS image_label (
    id              INTEGER PRIMARY KEY,
    image_id        INTEGER NOT NULL REFERENCES image(id) ON DELETE CASCADE,
    card_id         TEXT,                 -- catalog id when the card was matched
    name            TEXT,
    set_name        TEXT,
    card_number     TEXT,
    language        TEXT,
    condition       TEXT,
    is_graded       INTEGER NOT NULL DEFAULT 0,
    grade           TEXT,
    grading_company TEXT,
    label_source    TEXT    NOT NULL DEFAULT 'user',  -- user | suggested | …
    confidence      REAL,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_label_image ON image_label(image_id);
CREATE INDEX IF NOT EXISTS idx_image_label_card ON image_label(card_id);
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
    conn.executescript(_SCHEMA)
    return conn


@dataclass
class ImageLabel:
    """The identity/condition assigned to a photographed card (ground truth)."""

    card_id: str | None = None
    name: str | None = None
    set_name: str | None = None
    card_number: str | None = None
    language: str | None = None
    condition: str | None = None
    is_graded: bool = False
    grade: str | None = None
    grading_company: str | None = None
    label_source: str = "user"
    confidence: float | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class VisionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_image(
        self,
        sha256: str,
        path: str,
        source: str | None = None,
        width: int | None = None,
        height: int | None = None,
        now: str | None = None,
    ) -> int:
        """Record an image (idempotent on ``sha256``); returns its id."""
        row = self.conn.execute(
            "SELECT id FROM image WHERE sha256 = ?", (sha256,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO image (sha256, path, source, width, height, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (sha256, path, source, width, height, now or _now()),
            )
        return int(cur.lastrowid)

    def add_label(
        self, image_id: int, label: ImageLabel, now: str | None = None
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO image_label (image_id, card_id, name, set_name, "
                "card_number, language, condition, is_graded, grade, "
                "grading_company, label_source, confidence, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    image_id,
                    label.card_id,
                    label.name,
                    label.set_name,
                    label.card_number,
                    label.language,
                    label.condition,
                    1 if label.is_graded else 0,
                    label.grade,
                    label.grading_company,
                    label.label_source,
                    label.confidence,
                    now or _now(),
                ),
            )
        return int(cur.lastrowid)

    def image_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM image").fetchone()[0]

    def label_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM image_label").fetchone()[0]

    def labels_for_card(self, card_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM image_label WHERE card_id = ?", (card_id,)
        ).fetchone()[0]
