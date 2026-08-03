"""Vision training corpus — a **separate** SQLite file (``vision.db``).

Foundation for the card-recognition engine. The unit is a **capture**: one
annotation session's worth of photos. A capture has:

* **images** — the one or more photos supplied for that session (a photo is a
  *scene* that may show several cards), stored as content-addressed files on
  disk under ``<data_dir>/vision/images/``;
* **regions** — the drawn boxes on those images, each with box geometry +
  the card identity/condition the user assigned (the localized ground-truth
  labels the detector + identifier learn from). Their crop embeddings cache in
  ``region_embedding``.

Grouping by capture keeps the several photos of a session associated. Kept out of
the ledger DB (bulky, derived ML data) so its exports/backups stay small —
mirrors ``pricehistory.db`` / ``auctions.db``. Qt-free.
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
CREATE TABLE IF NOT EXISTS region_embedding (
    region_id     INTEGER NOT NULL REFERENCES region(id) ON DELETE CASCADE,
    model_version TEXT    NOT NULL,      -- embedder identity; changing it re-embeds
    card_id       TEXT    NOT NULL,      -- the region's label (denormalised)
    dim           INTEGER NOT NULL,
    vector        BLOB    NOT NULL,      -- float32 bytes, L2-normalised crop embedding
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (region_id, model_version)
);
CREATE INDEX IF NOT EXISTS idx_region_embedding_card
    ON region_embedding(model_version, card_id);
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
    """Drop superseded experimental schema.

    The vision corpus is new/experimental and the ingestion flow is still being
    shaped; stale tables are thrown away rather than migrated:
    * a pre-capture ``image`` table without ``capture_id`` (per-image labels), and
    * ``capture_card`` — the M-V0 image-level label table, superseded by
      ``region`` (localized per-box labels).
    """
    conn.execute("DROP TABLE IF EXISTS capture_card")
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

    # -- per-crop exemplar embeddings (the metric-learning corpus) -----------

    def put_region_embedding(
        self,
        region_id: int,
        model_version: str,
        card_id: str,
        dim: int,
        vector: bytes,
        now: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO region_embedding (region_id, model_version, card_id, "
                "dim, vector, created_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(region_id, model_version) DO UPDATE SET "
                "card_id = excluded.card_id, dim = excluded.dim, "
                "vector = excluded.vector",
                (region_id, model_version, card_id, dim, vector, now or _now()),
            )

    def has_region_embedding(self, region_id: int, model_version: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM region_embedding WHERE region_id = ? AND "
                "model_version = ?",
                (region_id, model_version),
            ).fetchone()
            is not None
        )

    def region_embedding_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM region_embedding"
        ).fetchone()[0]

    def region_vectors(self, model_version: str) -> list[tuple[str, bytes]]:
        """All exemplar (card_id, vector-bytes) pairs for a model version."""
        rows = self.conn.execute(
            "SELECT card_id, vector FROM region_embedding WHERE model_version = ?",
            (model_version,),
        ).fetchall()
        return [(r["card_id"], bytes(r["vector"])) for r in rows]

    def labeled_regions(self) -> list[sqlite3.Row]:
        """Regions carrying a catalog id, joined with their image path/size —
        the (crop, identity) pairs the identifier trains and indexes on."""
        return self.conn.execute(
            "SELECT r.id AS region_id, r.card_id, r.x, r.y, r.w, r.h, "
            "i.path AS image_path, i.width, i.height "
            "FROM region r JOIN image i ON i.id = r.image_id "
            "WHERE r.card_id IS NOT NULL ORDER BY r.id"
        ).fetchall()

    def condition_regions(self) -> list[sqlite3.Row]:
        """Back-side regions carrying a condition label, joined with their image
        path — the (crop, condition) pairs the condition model trains on."""
        return self.conn.execute(
            "SELECT r.id AS region_id, r.condition, r.x, r.y, r.w, r.h, "
            "i.path AS image_path "
            "FROM region r JOIN image i ON i.id = r.image_id "
            "WHERE r.condition IS NOT NULL AND i.side = 'back' ORDER BY r.id"
        ).fetchall()

    def regions_for_image(self, image_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM region WHERE image_id = ? ORDER BY number", (image_id,)
        ).fetchall()

    def images_with_regions(self) -> list[sqlite3.Row]:
        """Images that carry at least one drawn region (detector training set)."""
        return self.conn.execute(
            "SELECT i.id, i.path, i.width, i.height FROM image i "
            "WHERE EXISTS (SELECT 1 FROM region r WHERE r.image_id = i.id) "
            "ORDER BY i.id"
        ).fetchall()

    def capture_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM capture").fetchone()[0]

    def image_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM image").fetchone()[0]

    # -- corpus summary (Recognition History view) --------------------------

    # A region counts as *labeled* once the user has given it an identity —
    # either a resolved catalog id or a typed name (the recognition tool often
    # stores only the name string, with card_id left NULL).
    _LABELED = "(r.card_id IS NOT NULL OR (r.name IS NOT NULL AND r.name <> ''))"

    def labeled_region_count(self) -> int:
        """Regions carrying an identity — the labeled crops in the corpus."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM region r "
            f"WHERE {self._LABELED}"  # noqa: S608 - constant predicate
        ).fetchone()[0]

    def labeled_card_count(self) -> int:
        """Distinct cards represented in the corpus (breadth) — keyed by catalog
        id when present, otherwise by name."""
        return self.conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(r.card_id, r.name)) FROM region r "
            f"WHERE {self._LABELED}"  # noqa: S608
        ).fetchone()[0]

    def capture_rows(self) -> list[sqlite3.Row]:
        """One row per capture (annotation session), oldest first: its source,
        note, date, and counts of images, regions (boxes) and labeled regions."""
        return self.conn.execute(
            "SELECT c.id, c.source, c.note, c.created_at, "
            "COUNT(DISTINCT i.id) AS images, "
            "COUNT(r.id) AS regions, "
            f"COALESCE(SUM(CASE WHEN {self._LABELED} THEN 1 ELSE 0 END), 0) AS labeled "
            "FROM capture c "
            "LEFT JOIN image i ON i.capture_id = c.id "
            "LEFT JOIN region r ON r.image_id = i.id "
            "GROUP BY c.id ORDER BY c.created_at, c.id"  # noqa: S608
        ).fetchall()

    def capture_cards(self) -> list[sqlite3.Row]:
        """Labeled regions keyed by capture — the per-session card list, plus the
        box geometry + source image needed to crop each card's thumbnail."""
        return self.conn.execute(
            "SELECT i.capture_id AS capture_id, r.id AS region_id, r.card_id, "
            "r.name, r.set_name, r.card_number, r.x, r.y, r.w, r.h, "
            "i.path AS image_path FROM region r JOIN image i ON i.id = r.image_id "
            f"WHERE {self._LABELED} ORDER BY i.capture_id, r.id"  # noqa: S608
        ).fetchall()

    def capture_images(self, capture_id: int) -> list[sqlite3.Row]:
        """The images supplied in a capture (the filmstrip for the preview)."""
        return self.conn.execute(
            "SELECT id, path, side, width, height FROM image "
            "WHERE capture_id = ? ORDER BY id",
            (capture_id,),
        ).fetchall()

    def capture_regions(self, capture_id: int) -> list[sqlite3.Row]:
        """Every region in a capture (labeled or not), with its image path/side —
        the per-capture detail drill-down."""
        return self.conn.execute(
            "SELECT r.*, i.path AS image_path, i.side AS image_side "
            "FROM region r JOIN image i ON i.id = r.image_id "
            "WHERE i.capture_id = ? ORDER BY i.id, r.number",
            (capture_id,),
        ).fetchall()

    def image_growth_series(self) -> list[tuple[str, int]]:
        """(day, cumulative image count) across all supplied images — the
        corpus-growth curve behind the Recognition History sparkline."""
        rows = self.conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n "
            "FROM image GROUP BY day ORDER BY day"
        ).fetchall()
        series: list[tuple[str, int]] = []
        total = 0
        for row in rows:
            total += row["n"]
            series.append((row["day"], total))
        return series
