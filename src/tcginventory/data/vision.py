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

# Model kinds in the run log. The detector finds card boxes; the condition
# grader scores card backs. The identifier has no kind because it needs no
# training run at all — a labeled crop *is* its training step.
KIND_DETECTOR = "detector"
KIND_CONDITION = "condition"
# Identification has no training run of its own — a labelled crop is its
# training step — but its accuracy is benchmarked, and those runs belong in the
# same log so the three models are read the same way.
KIND_IDENTITY = "identity"

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
    dim           INTEGER NOT NULL,      -- ELEMENTS, not bytes (see dtype)
    vector        BLOB    NOT NULL,      -- L2-normalised, packed as `dtype`
    dtype         TEXT    NOT NULL DEFAULT 'float32',  -- how to read `vector`
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (card_id, model_version)
);
CREATE TABLE IF NOT EXISTS region_embedding (
    region_id     INTEGER NOT NULL REFERENCES region(id) ON DELETE CASCADE,
    model_version TEXT    NOT NULL,      -- embedder identity; changing it re-embeds
    card_id       TEXT    NOT NULL,      -- the region's label (denormalised)
    dim           INTEGER NOT NULL,      -- ELEMENTS, not bytes (see dtype)
    vector        BLOB    NOT NULL,      -- L2-normalised crop embedding, as `dtype`
    dtype         TEXT    NOT NULL DEFAULT 'float32',  -- how to read `vector`
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (region_id, model_version)
);
CREATE INDEX IF NOT EXISTS idx_region_embedding_card
    ON region_embedding(model_version, card_id);
CREATE TABLE IF NOT EXISTS model_run (
    id                 INTEGER PRIMARY KEY,
    kind               TEXT    NOT NULL DEFAULT 'detector',  -- 'detector' | 'condition'
    version            INTEGER,            -- model version *within its kind*
    status             TEXT    NOT NULL,   -- 'running' | 'ok' | 'failed'
    source             TEXT    NOT NULL,   -- 'ui' (Operations) | 'cli' | 'scheduled'
    started_at         TEXT    NOT NULL,
    finished_at        TEXT,
    -- the corpus snapshot it trained on (so a model can be called stale)
    corpus_images      INTEGER NOT NULL DEFAULT 0,
    corpus_boxes       INTEGER NOT NULL DEFAULT 0,
    corpus_cards       INTEGER NOT NULL DEFAULT 0,
    corpus_fingerprint TEXT,               -- hash of the (image, box) set
    train_images       INTEGER,
    val_images         INTEGER,
    val_frac           REAL,
    split_seed         INTEGER,
    -- how it was trained
    base_weights       TEXT,
    epochs             INTEGER,
    imgsz              INTEGER,
    -- how it scored on its own val split
    map50              REAL,
    map50_95           REAL,
    precision          REAL,
    recall             REAL,
    best_epoch         INTEGER,
    -- One comparable, higher-is-better number per kind (see services/fitness.py).
    -- The four columns above mean different things for each model — mAP for the
    -- detector, accuracy/mean-distance/macro-F1 for the grader, top-1/top-5 for
    -- identity — so nothing shared could ever read them safely. This one can.
    fitness            REAL,
    split_policy       TEXT,               -- how val was chosen; see _SPLIT_POLICY
    metrics_json       TEXT,               -- kind-specific extras (confusion matrix…)
    -- what it produced
    artifact_path      TEXT,
    artifact_sha256    TEXT,
    artifact_bytes     INTEGER,
    promoted           INTEGER NOT NULL DEFAULT 0,
    notes              TEXT,               -- summary, or the error on failure
    created_at         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_run_kind
    ON model_run(kind, started_at DESC);
CREATE TABLE IF NOT EXISTS model_epoch (
    run_id    INTEGER NOT NULL REFERENCES model_run(id) ON DELETE CASCADE,
    epoch     INTEGER NOT NULL,
    map50     REAL,
    map50_95  REAL,
    precision REAL,
    recall    REAL,
    box_loss  REAL,          -- validation loss, as ultralytics reports it
    cls_loss  REAL,
    train_box_loss REAL,     -- training loss: moves every epoch (see _ensure_columns)
    train_cls_loss REAL,
    PRIMARY KEY (run_id, epoch)
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
    _migrate_run_log(conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a capture-schema DB was first created."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(image)")}
    if "side" not in cols:
        conn.execute("ALTER TABLE image ADD COLUMN side TEXT")
    if "pair_group" not in cols:
        conn.execute("ALTER TABLE image ADD COLUMN pair_group INTEGER")

    # The *training* loss, recorded per epoch (2026-08-06). The validation
    # columns above are what ultralytics reports, and it does not validate on
    # every epoch — it repeats the previous row's figures on the epochs it skips,
    # so a curve drawn from them is a stair of duplicate pairs rather than a
    # curve. `train/box_loss` moves every epoch, so it is the honest loss.
    epoch_cols = {r[1] for r in conn.execute("PRAGMA table_info(model_epoch)")}
    if "train_box_loss" not in epoch_cols:
        conn.execute("ALTER TABLE model_epoch ADD COLUMN train_box_loss REAL")
    if "train_cls_loss" not in epoch_cols:
        conn.execute("ALTER TABLE model_epoch ADD COLUMN train_cls_loss REAL")

    # One comparable score per kind, and the split policy behind it (2026-08-07).
    # Runs recorded before this shipped keep a NULL policy, which is exactly what
    # marks them as scored on an unstable validation split and therefore not
    # comparable with anything after it — see services/fitness.py.
    run_cols = {r[1] for r in conn.execute("PRAGMA table_info(model_run)")}
    if "fitness" not in run_cols:
        conn.execute("ALTER TABLE model_run ADD COLUMN fitness REAL")
    if "split_policy" not in run_cols:
        conn.execute("ALTER TABLE model_run ADD COLUMN split_policy TEXT")

    # How to read `vector` (2026-08-09). Embeddings were float32-only until
    # float16 halved them at no measured cost, and the dtype was implicit in
    # every reader's `np.frombuffer(..., np.float32)`. Defaulting to 'float32'
    # is what lets an existing file keep reading while it is converted a table
    # at a time — a half-converted table is mixed, and mixed is still correct.
    # Nothing infers the dtype from blob length; a model with a different `dim`
    # would make that guess wrong. See services/vectors.py.
    for table in ("card_embedding", "region_embedding"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "dtype" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN dtype TEXT NOT NULL DEFAULT 'float32'"
            )


def _migrate_run_log(conn: sqlite3.Connection) -> None:
    """Fold the detector-only run log into ``model_run`` (2026-08-04).

    ``detector_run``/``detector_epoch`` were superseded by kind-tagged tables once
    a second model (the condition grader) came into view. Rows are carried over
    rather than dropped — there were none anywhere when this shipped, but a log
    that quietly loses history would be a poor thing to own.
    """
    have = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('detector_run', 'detector_epoch')"
        )
    }
    if not have:
        return
    if "detector_run" in have:
        old = [r[1] for r in conn.execute("PRAGMA table_info(detector_run)")]
        new = [r[1] for r in conn.execute("PRAGMA table_info(model_run)")]
        shared = [c for c in old if c in new]
        cols = ", ".join(shared)
        conn.execute(
            f"INSERT INTO model_run (kind, {cols}) "  # noqa: S608 - names from PRAGMA
            f"SELECT 'detector', {cols} FROM detector_run"
        )
    if "detector_epoch" in have:
        conn.execute("INSERT INTO model_epoch SELECT * FROM detector_epoch")
    conn.executescript(
        "DROP TABLE IF EXISTS detector_epoch; DROP TABLE IF EXISTS detector_run;"
    )
    conn.commit()


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

    def get_embedding(
        self, card_id: str, model_version: str
    ) -> tuple[bytes, str] | None:
        """The stored ``(vector-bytes, dtype)``, or ``None``.

        The dtype comes back with the bytes because they are meaningless apart:
        reading a float16 blob as float32 yields a half-length vector that the
        scoring paths *skip* rather than reject. Decode with
        ``services.vectors.unpack``.
        """
        row = self.conn.execute(
            "SELECT vector, dtype FROM card_embedding "
            "WHERE card_id = ? AND model_version = ?",
            (card_id, model_version),
        ).fetchone()
        return (bytes(row["vector"]), row["dtype"]) if row is not None else None

    def put_embedding(
        self,
        card_id: str,
        model_version: str,
        dim: int,
        vector: bytes,
        dtype: str,
        now: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO card_embedding (card_id, model_version, dim, vector, "
                "dtype, created_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(card_id, model_version) DO UPDATE SET "
                "dim = excluded.dim, vector = excluded.vector, dtype = excluded.dtype",
                (card_id, model_version, dim, vector, dtype, now or _now()),
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
        dtype: str,
        now: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO region_embedding (region_id, model_version, card_id, "
                "dim, vector, dtype, created_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(region_id, model_version) DO UPDATE SET "
                "card_id = excluded.card_id, dim = excluded.dim, "
                "vector = excluded.vector, dtype = excluded.dtype",
                (region_id, model_version, card_id, dim, vector, dtype, now or _now()),
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
        return self.conn.execute("SELECT COUNT(*) FROM region_embedding").fetchone()[0]

    def region_vectors(self, model_version: str) -> list[tuple[str, bytes, str, int]]:
        """All exemplar ``(card_id, vector-bytes, dtype, dim)`` for a version.

        Decode with ``services.vectors.unpack`` — the dtype and dim travel with
        the bytes so a caller cannot read them as the wrong type.
        """
        rows = self.conn.execute(
            "SELECT card_id, vector, dtype, dim FROM region_embedding "
            "WHERE model_version = ?",
            (model_version,),
        ).fetchall()
        return [
            (r["card_id"], bytes(r["vector"]), r["dtype"], int(r["dim"])) for r in rows
        ]

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
            f"SELECT COUNT(*) FROM region r WHERE {self._LABELED}"  # noqa: S608 - constant predicate
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


class ModelRunRepository:
    """The model training log (``model_run`` + ``model_epoch``).

    One row per training attempt, holding what it trained on, how, how it scored,
    and what it produced — so the Operations window can state the current model's
    version and provenance, say whether the corpus has moved on since, and chart
    accuracy across runs. Mirrors ``OperationRunRepository`` in the ledger DB, but
    lives here because it describes the vision corpus.

    Rows are tagged by ``kind`` (:data:`KIND_DETECTOR` / :data:`KIND_CONDITION`);
    versions run **per kind**, so "detector v2" and "condition v2" are unrelated.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def start(
        self,
        *,
        kind: str = KIND_DETECTOR,
        source: str = "manual",
        corpus_images: int = 0,
        corpus_boxes: int = 0,
        corpus_cards: int = 0,
        corpus_fingerprint: str | None = None,
        base_weights: str | None = None,
        epochs: int | None = None,
        imgsz: int | None = None,
        val_frac: float | None = None,
        split_seed: int | None = None,
        split_policy: str | None = None,
        now: str | None = None,
    ) -> int:
        """Open a run (status ``running``) and return its id.

        Recorded *before* training so a crashed or killed attempt still leaves a
        trace rather than vanishing.
        """
        stamp = now or _now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO model_run (kind, status, source, started_at, "
                "corpus_images, corpus_boxes, corpus_cards, corpus_fingerprint, "
                "base_weights, epochs, imgsz, val_frac, split_seed, split_policy, "
                "created_at) VALUES (?,'running',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    kind,
                    source,
                    stamp,
                    corpus_images,
                    corpus_boxes,
                    corpus_cards,
                    corpus_fingerprint,
                    base_weights,
                    epochs,
                    imgsz,
                    val_frac,
                    split_seed,
                    split_policy,
                    stamp,
                ),
            )
        return int(cur.lastrowid)

    def finish(
        self,
        run_id: int,
        *,
        status: str = "ok",
        train_images: int | None = None,
        val_images: int | None = None,
        map50: float | None = None,
        map50_95: float | None = None,
        precision: float | None = None,
        recall: float | None = None,
        best_epoch: int | None = None,
        fitness: float | None = None,
        artifact_path: str | None = None,
        artifact_sha256: str | None = None,
        artifact_bytes: int | None = None,
        metrics_json: str | None = None,
        notes: str | None = None,
        now: str | None = None,
    ) -> None:
        """Close a run with its outcome. Promotion is a separate step."""
        with self.conn:
            self.conn.execute(
                "UPDATE model_run SET status = ?, finished_at = ?, "
                "train_images = ?, val_images = ?, map50 = ?, map50_95 = ?, "
                "precision = ?, recall = ?, best_epoch = ?, fitness = ?, "
                "artifact_path = ?, artifact_sha256 = ?, artifact_bytes = ?, "
                "metrics_json = ?, notes = ? WHERE id = ?",
                (
                    status,
                    now or _now(),
                    train_images,
                    val_images,
                    map50,
                    map50_95,
                    precision,
                    recall,
                    best_epoch,
                    fitness,
                    artifact_path,
                    artifact_sha256,
                    artifact_bytes,
                    metrics_json,
                    notes,
                    run_id,
                ),
            )

    def add_epochs(self, run_id: int, rows) -> int:
        """Record the per-epoch curve: dicts with ``epoch`` plus any of map50,
        map50_95, precision, recall, box_loss, cls_loss. Returns the row count."""
        payload = [
            (
                run_id,
                int(r["epoch"]),
                r.get("map50"),
                r.get("map50_95"),
                r.get("precision"),
                r.get("recall"),
                r.get("box_loss"),
                r.get("cls_loss"),
                r.get("train_box_loss"),
                r.get("train_cls_loss"),
            )
            for r in rows
        ]
        if not payload:
            return 0
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO model_epoch (run_id, epoch, map50, "
                "map50_95, precision, recall, box_loss, cls_loss, "
                "train_box_loss, train_cls_loss) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
        return len(payload)

    def promote(self, run_id: int) -> int:
        """Mark a run's artifact as the model in use; returns its new version.

        Versions increment **within a kind**, so "detector v3" always means the
        third detector actually put to work, regardless of condition runs.
        """
        kind_row = self.conn.execute(
            "SELECT kind FROM model_run WHERE id = ?", (run_id,)
        ).fetchone()
        kind = kind_row["kind"] if kind_row else KIND_DETECTOR
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM model_run WHERE kind = ?",
            (kind,),
        ).fetchone()
        version = int(row["v"]) + 1
        with self.conn:
            self.conn.execute(
                "UPDATE model_run SET promoted = 1, version = ? WHERE id = ?",
                (version, run_id),
            )
        return version

    # -- reads --------------------------------------------------------------

    def get(self, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM model_run WHERE id = ?", (run_id,)
        ).fetchone()

    def recent(self, kind: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        """Newest runs first — the training history. ``kind`` None means all."""
        if kind is None:
            return self.conn.execute(
                "SELECT * FROM model_run ORDER BY started_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM model_run WHERE kind = ? "
            "ORDER BY started_at DESC, id DESC LIMIT ?",
            (kind, limit),
        ).fetchall()

    def last_run(self, kind: str | None = None) -> sqlite3.Row | None:
        """The most recent attempt of a kind, whatever its outcome."""
        rows = self.recent(kind, limit=1)
        return rows[0] if rows else None

    def current_model(
        self, kind: str = KIND_DETECTOR, *, source: str | None = None
    ) -> sqlite3.Row | None:
        """The promoted run behind the model in use (top version of a kind).

        ``source`` narrows to one flavour of run within a kind. Identity needs
        it: benchmarks and projection fits share ``KIND_IDENTITY``, and only a
        benchmark is a measurement another benchmark can be judged against.
        """
        if source is None:
            return self.conn.execute(
                "SELECT * FROM model_run WHERE promoted = 1 AND kind = ? "
                "ORDER BY version DESC, id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM model_run WHERE promoted = 1 AND kind = ? AND source = ? "
            "ORDER BY version DESC, id DESC LIMIT 1",
            (kind, source),
        ).fetchone()

    def last_finished(
        self, kind: str = KIND_DETECTOR, *, source: str | None = None
    ) -> sqlite3.Row | None:
        """The newest run that finished successfully, promoted or not.

        Staleness is measured against this rather than against the promoted run.
        A retrain that saw the whole corpus and then lost on score still *saw the
        whole corpus*, and a screen that keeps saying "stale" after it sends you
        round the same loop forever (2026-08-07).
        """
        if source is None:
            return self.conn.execute(
                "SELECT * FROM model_run WHERE kind = ? AND status = 'ok' "
                "ORDER BY started_at DESC, id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM model_run WHERE kind = ? AND status = 'ok' AND source = ? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (kind, source),
        ).fetchone()

    def epochs(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM model_epoch WHERE run_id = ? ORDER BY epoch",
            (run_id,),
        ).fetchall()

    def accuracy_series(
        self, kind: str = KIND_DETECTOR, limit: int = 20
    ) -> list[tuple[int, float]]:
        """(version, fitness) for promoted models of a kind, oldest first — the
        accuracy trend the Operations panel charts.

        Charts ``fitness``, not ``map50_95``. That column is the grader's *mean
        grade distance*, where lower is better, so plotting it as an accuracy
        trend drew the condition model's improvement upside down (2026-08-07).
        ``fitness`` is higher-is-better for every kind by construction.
        """
        rows = self.conn.execute(
            "SELECT version, fitness FROM model_run "
            "WHERE promoted = 1 AND kind = ? AND fitness IS NOT NULL "
            "ORDER BY version DESC LIMIT ?",
            (kind, limit),
        ).fetchall()
        return [(int(r["version"]), float(r["fitness"])) for r in reversed(rows)]
