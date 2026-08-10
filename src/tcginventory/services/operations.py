"""The maintenance operations catalogue behind the Operations window.

Every job the app can run on demand — imports, exports, backups, price and
catalog updates, the daily-update toggle — is described **as data** here, not as
menu-building code. The view renders itself from :data:`OPERATIONS`, so adding a
future operation is one entry and it cannot drift out of line with the others.

This module is Qt-free and issues no SQL of its own: statuses are assembled from
the repositories and from files on disk, so they can be tested headless.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tcginventory.data.vision import KIND_CONDITION, KIND_DETECTOR

# ---------------------------------------------------------------------------
# the catalogue
# ---------------------------------------------------------------------------

# How an operation is triggered:
#   dialog     — asks for a file/folder first, then runs inline
#   background — a worker thread; the tile shows progress and logs the result
#   toggle     — flips a persistent setting, no run to report
KIND_DIALOG = "dialog"
KIND_BACKGROUND = "background"
KIND_TOGGLE = "toggle"


@dataclass(frozen=True)
class Group:
    id: str
    label: str


# Display order of the sections, and the order of the in-page navigator beside
# them. Operations is about the **ledger**: getting data in and out, keeping
# prices and the card database current, and looking after the file itself.
GROUPS: tuple[Group, ...] = (
    Group("data_in", "Bring data in"),
    Group("data_out", "Get data out"),
    Group("prices", "Prices"),
    Group("reference", "Reference data"),
    Group("automation", "Automation"),
    Group("maintenance", "Maintenance & diagnostics"),
)

# The Models window's sections. They were once tiles here, which put "train the
# detector" beside "import a CSV" and gave a model nowhere to report its metrics,
# its losses or its history. Each is about **one** thing: the three models have
# separate data and separate lifecycles, and the environment that trains them is
# machine setup rather than a model at all. The entries stay in the registry
# below — the run log, statuses and staleness all key off them — but Operations
# no longer renders them.
MODEL_GROUPS: tuple[Group, ...] = (
    Group("model_regions", "Region detection"),
    Group("model_condition", "Condition"),
    Group("model_identity", "Card identity"),
    Group("training_env", "Training environment"),
)


@dataclass(frozen=True)
class Operation:
    """One maintenance job, as the window presents it."""

    id: str
    group: str
    title: str
    blurb: str  # what it does, in the user's terms
    verb: str  # button label; a trailing "…" means it asks for a file first
    icon: str  # bundled Lucide name (ui/assets/icons)
    kind: str = KIND_DIALOG
    primary: bool = False  # at most one per screen — the accent-filled button
    danger: bool = False  # irreversible; drawn with a red rail
    caption: str = "Last run"  # label above the status value
    stale_after_days: int | None = None  # older than this reads as "Stale"


OPERATIONS: tuple[Operation, ...] = (
    Operation(
        id="import.items",
        group="data_in",
        title="Import items from CSV",
        blurb=(
            "Add cards you own to the catalog without posting money — names, "
            "sets, numbers, conditions. A validation preview runs first, so bad "
            "rows never reach the ledger."
        ),
        verb="Choose file…",
        icon="download",
        caption="Last import",
    ),
    Operation(
        id="import.purchases",
        group="data_in",
        title="Import purchases from CSV",
        blurb=(
            "Post real purchases in bulk: every row becomes a transaction with "
            "its own cost basis, so profit and loss are right from day one."
        ),
        verb="Choose file…",
        icon="arrow-left-right",
        caption="Last import",
    ),
    Operation(
        id="shows.refresh",
        group="reference",
        title="Refresh card shows",
        blurb=(
            "Re-scrape upcoming shows for the states you follow, so the Calendar "
            "and the dashboard's Upcoming Shows are current. Runs nightly too."
        ),
        verb="Refresh",
        icon="calendar-days",
        kind=KIND_BACKGROUND,
        caption="Shows",
        stale_after_days=7,
    ),
    Operation(
        id="export.inventory",
        group="data_out",
        title="Export inventory to CSV",
        blurb=(
            "One row per lot with cost basis, market value and P/L — the file to "
            "open in a spreadsheet or hand to an accountant."
        ),
        verb="Export…",
        icon="upload",
        caption="Last export",
    ),
    Operation(
        id="export.full",
        group="data_out",
        title="Export full backup (CSV folder)",
        blurb=(
            "Every ledger table as CSV, plus readable copies. This is the dump a "
            "restore reads back — your escape hatch if the database is lost."
        ),
        verb="Export…",
        icon="package",
        caption="Last export",
        stale_after_days=14,
    ),
    Operation(
        id="backup.database",
        group="data_out",
        title="Back up the database",
        blurb=(
            "A consistent point-in-time copy of inventory.db, taken with SQLite's "
            "online backup so it's safe to run while the app is open. One is "
            "written automatically at every startup."
        ),
        verb="Back up…",
        icon="database",
        caption="Automatic backups",
    ),
    Operation(
        id="prices.refresh",
        group="prices",
        title="Refresh prices you own",
        blurb=(
            "Re-price the cards in your inventory and record a snapshot, so "
            "Unrealized P/L and the dashboard move with the market."
        ),
        verb="Refresh",
        icon="trending-up",
        kind=KIND_BACKGROUND,
        primary=True,
        stale_after_days=3,
    ),
    Operation(
        id="prices.market_sweep",
        group="prices",
        title="Update market prices (whole catalog)",
        blurb=(
            "Sweep every set into the price-history file — the series behind "
            "trends and the market outlook. Only changes are stored."
        ),
        verb="Update",
        icon="refresh-cw",
        kind=KIND_BACKGROUND,
        caption="Last sweep",
        stale_after_days=7,
    ),
    Operation(
        id="catalog.update_en",
        group="reference",
        title="Update card database (English)",
        blurb=(
            "Pull the reference catalog: names, sets, collector numbers and art "
            "behind autofill, search and every card preview."
        ),
        verb="Update",
        icon="search",
        kind=KIND_BACKGROUND,
        caption="Loaded",
    ),
    Operation(
        id="catalog.update_jp",
        group="reference",
        title="Update card database (Japanese)",
        blurb=(
            "The same pull for the Japanese market, with English and romaji names "
            "so the cards stay searchable without a Japanese keyboard."
        ),
        verb="Update",
        icon="search",
        kind=KIND_BACKGROUND,
        caption="Loaded",
    ),
    Operation(
        id="identity.index_catalog",
        group="model_identity",
        title="Index the card catalog",
        blurb=(
            "Lets recognition name a card it has never seen. Until this runs, "
            "suggestions can only offer cards you have already labelled — 43 out of "
            "53,289. Choose how much to take on: art already downloaded costs "
            "nothing; the whole catalog is a long download, kept on disk so a "
            "future model can re-learn from it without fetching anything again."
        ),
        verb="Index…",
        icon="database",
        caption="Catalog index",
    ),
    Operation(
        id="identity.reclaim",
        group="model_identity",
        title="Reclaim catalog art",
        blurb=(
            "Deletes archived card art that nothing depends on, keeping every card "
            "you have labelled, own, or had already downloaded. The index itself "
            "stays — it is 82 MB and it is the part that makes recognition work."
        ),
        verb="Reclaim",
        icon="trash-2",
        kind=KIND_BACKGROUND,
        caption="Archived art",
    ),
    Operation(
        id="identity.fit_projection",
        group="model_identity",
        title="Fit photo matching",
        blurb=(
            "The catalog holds clean renders; your camera does not, and the gap "
            "is wide enough to invert the answer — a photo lands closer to the "
            "wrong card's art than its own. Learns the correction from catalog "
            "images plus the cards you have labelled. No training environment."
        ),
        verb="Fit",
        icon="sparkles",
        kind=KIND_BACKGROUND,
        caption="Last fitted",
        stale_after_days=90,
    ),
    Operation(
        id="identity.forget_projection",
        group="model_identity",
        title="Remove photo matching",
        blurb=(
            "Drops the fitted correction and ranks on the raw model again. The "
            "catalog index is untouched, so this costs nothing to undo — fit it "
            "again whenever."
        ),
        verb="Remove",
        icon="trash-2",
        kind=KIND_BACKGROUND,
        caption="Photo match",
    ),
    Operation(
        id="vision.reindex",
        group="model_identity",
        title="Re-index the recognition corpus",
        blurb=(
            "Re-embed every labelled card crop so photo autofill matches against "
            "the current model. Needed after installing a new model, or if "
            "recognition starts missing cards you have already labelled."
        ),
        verb="Re-index",
        icon="scan-line",
        kind=KIND_BACKGROUND,
        caption="Corpus",
    ),
    Operation(
        id="vision.compact_index",
        group="model_identity",
        title="Compact the card index",
        blurb=(
            "Halves the space the card index takes on disk and in memory by "
            "storing each card's fingerprint at half precision. Measured to pick "
            "the same match every time, so recognition is unaffected. Takes "
            "seconds — nothing is re-embedded."
        ),
        verb="Compact",
        icon="database",
        kind=KIND_BACKGROUND,
        caption="Card index",
    ),
    # -- Models & signals ---------------------------------------------------
    Operation(
        id="models.training_env",
        group="training_env",
        title="Set up model training",
        blurb=(
            "Builds the environment that trains the recognition models: finds a "
            "Python on this machine, creates a private virtualenv in your data "
            "folder and installs the training stack (~280 MB). One time, then "
            "training is a single click."
        ),
        verb="Set up",
        icon="sliders-horizontal",
        kind=KIND_BACKGROUND,
        caption="Training env",
    ),
    Operation(
        id="models.training_env_gpu",
        group="training_env",
        title="Set up training on the GPU",
        blurb=(
            "Builds the same environment against a CUDA build of torch, so training "
            "runs on your graphics card instead of the processor — the difference "
            "between minutes and hours on a real corpus. Needs an NVIDIA card with "
            "its driver installed; the download is larger, around 2.5 GB."
        ),
        verb="Set up with GPU",
        icon="sparkles",
        kind=KIND_BACKGROUND,
        caption="Accelerator",
    ),
    Operation(
        id="models.training_env_remove",
        group="training_env",
        title="Remove the training environment",
        blurb=(
            "Deletes the training virtualenv and reclaims its disk space. Your "
            "trained models, the labelled corpus and every training run are kept "
            "— only the interpreter and its packages go. Set it up again whenever "
            "you want to train."
        ),
        verb="Remove",
        icon="trash-2",
        kind=KIND_BACKGROUND,
        caption="Disk use",
    ),
    Operation(
        id="models.detector.train",
        group="model_regions",
        title="Train the card detector (regions)",
        blurb=(
            "Teaches the detector where cards are in a photo, from every box you "
            "have drawn in Recognition. It replaces the model in use only if it "
            "scores better, and everything about the run is kept: what it trained "
            "on, how it scored, and the model it produced."
        ),
        verb="Train",
        icon="scan-line",
        kind=KIND_BACKGROUND,
        caption="Last trained",
        stale_after_days=14,
    ),
    Operation(
        id="models.condition.train",
        group="model_condition",
        title="Train the condition grader",
        blurb=(
            "Grades card wear (NM → DMG) from both faces of a card — one grade per "
            "card, learned from its front and its back together, since wear shows "
            "on the corners and edges of each. Photograph cards front and back in "
            "Recognition and grade them to build it up."
        ),
        verb="Train",
        icon="image",
        kind=KIND_BACKGROUND,
        caption="Last trained",
        stale_after_days=14,
    ),
    Operation(
        id="db.integrity_check",
        group="maintenance",
        title="Check database integrity",
        blurb=(
            "Ask SQLite to verify every page and foreign key. Worth running after "
            "a crash, a disk problem, or before trusting a backup."
        ),
        verb="Check",
        icon="database",
        kind=KIND_BACKGROUND,
    ),
    Operation(
        id="db.vacuum",
        group="maintenance",
        title="Compact the database",
        blurb=(
            "Rebuild inventory.db to reclaim space left by deleted rows and "
            "re-pack its indexes. Safe to run any time; the ledger is untouched."
        ),
        verb="Compact",
        icon="package",
        kind=KIND_BACKGROUND,
        caption="Database size",
    ),
    Operation(
        id="automation.daily_prices",
        group="automation",
        title="Automatic daily price updates",
        blurb=(
            "Refreshes prices once a day in the background — even when the app is "
            "closed. Uses the Windows Task Scheduler."
        ),
        verb="",
        icon="calendar-days",
        kind=KIND_TOGGLE,
        caption="Next run",
    ),
)

BY_ID: dict[str, Operation] = {op.id: op for op in OPERATIONS}


def in_group(group_id: str) -> list[Operation]:
    return [op for op in OPERATIONS if op.group == group_id]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

# Tile states, in the visual vocabulary of the window.
READY, RUNNING, STALE, NEVER, ON, OFF = (
    "ready",
    "running",
    "stale",
    "never",
    "on",
    "off",
)


@dataclass(frozen=True)
class Status:
    value: str  # the mono line under the caption
    state: str  # drives the pill
    detail: str | None = None  # tooltip / secondary text
    # False when running the operation right now would be meaningless — the
    # environment is already built, or there is nothing to remove. The tile
    # greys its button out and explains why from ``detail``.
    actionable: bool = True
    # Overrides the button label when what the operation *does* depends on the
    # current state ("Set up" vs "Switch to GPU environment").
    verb: str | None = None


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(stamp[:10])
        except ValueError:
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _age_days(stamp: str | None) -> float | None:
    parsed = _parse(stamp)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 86400


def when(stamp: str | None) -> str:
    """A short, human rendering of a timestamp: today/yesterday, else the date."""
    parsed = _parse(stamp)
    if parsed is None:
        return "never"
    local = parsed.astimezone()
    days = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400
    if days < 1 and local.date() == datetime.now().date():
        return f"today {local:%H:%M}"
    if days < 2:
        return f"yesterday {local:%H:%M}"
    return f"{local:%Y-%m-%d}"


def _staleness(op: Operation, stamp: str | None) -> str:
    age = _age_days(stamp)
    if age is None:
        return NEVER
    if op.stale_after_days is not None and age > op.stale_after_days:
        return STALE
    return READY


def backups_dir() -> Path:
    from tcginventory import config

    return config.data_dir() / "backups"


def backup_facts(directory: Path | None = None) -> tuple[int, str | None, int]:
    """``(count, newest_iso, total_bytes)`` for the rotating startup backups."""
    directory = Path(directory) if directory else backups_dir()
    try:
        files = sorted(directory.glob("inventory-*.db"))
    except OSError:
        return 0, None, 0
    if not files:
        return 0, None, 0
    total = 0
    newest = 0.0
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        total += stat.st_size
        newest = max(newest, stat.st_mtime)
    stamp = datetime.fromtimestamp(newest, timezone.utc).isoformat() if newest else None
    return len(files), stamp, total


def _today() -> str:
    return datetime.now().date().isoformat()


def corpus_facts(vision_conn=None) -> tuple[int, int, int]:
    """``(captures, labelled regions, indexed exemplars)`` from the vision corpus.

    Best-effort: the corpus lives in its own optional file, so a missing or
    unreadable one reads as an empty corpus rather than breaking the window.
    """
    from tcginventory.data.vision import VisionRepository, vision_path

    own = False
    conn = vision_conn
    try:
        if conn is None:
            if not vision_path().exists():
                return 0, 0, 0
            from tcginventory.data.vision import open_vision

            conn = open_vision()
            own = True
        repo = VisionRepository(conn)
        return (
            repo.capture_count(),
            repo.labeled_region_count(),
            repo.region_embedding_count(),
        )
    except Exception:
        return 0, 0, 0
    finally:
        if own and conn is not None:
            conn.close()


def _compact_index_status() -> Status:
    """What the stored index costs, and whether compacting would help.

    Reports the dtypes actually on disk rather than a last-run timestamp: a file
    can be half-converted (an interrupted run is deliberately safe), and "3,200
    still at full precision" is the useful thing to say about one.
    """
    from tcginventory.data.vision import open_vision, vision_path
    from tcginventory.services import vectors

    path = vision_path()
    if not path.exists():
        return Status("no card index yet", NEVER)
    try:
        size = human_bytes(path.stat().st_size)
        conn = open_vision()
        try:
            counts = {
                str(row[0] or vectors.LEGACY_DTYPE): int(row[1])
                for row in conn.execute(
                    "SELECT dtype, COUNT(*) FROM card_embedding GROUP BY dtype"
                )
            }
        finally:
            conn.close()
    except Exception:  # pragma: no cover - unreadable corpus reads as absent
        return Status("no card index yet", NEVER)

    total = sum(counts.values())
    if not total:
        return Status("no card index yet", NEVER)
    legacy = total - counts.get(vectors.STORAGE_DTYPE, 0)
    if not legacy:
        return Status(f"{total:,} cards · {size} · compact", READY)
    return Status(
        f"{total:,} cards · {size}",
        STALE,
        detail=(
            f"{legacy:,} vector(s) still at full precision — compacting would "
            "roughly halve what the index costs on disk and in memory"
        ),
    )


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} GB"


def status_for(conn, op: Operation, *, history_conn=None) -> Status:
    """What ``op``'s tile should say about the last time it did anything.

    Everything is derived from data the app already keeps: price snapshots, the
    catalog's own ``updated_at``, the price-history file, the backup files on
    disk, the scheduled task, and the run log for the rest.
    """
    from tcginventory.data.repositories import (
        CardCatalogRepository,
        OperationRunRepository,
        PriceSnapshotRepository,
    )

    if op.id == "prices.refresh":
        stamp = PriceSnapshotRepository(conn).latest_as_of()
        return Status(when(stamp), _staleness(op, stamp))

    if op.id in ("catalog.update_en", "catalog.update_jp"):
        language = "EN" if op.id.endswith("_en") else "JP"
        cards, stamp = CardCatalogRepository(conn).stats(language)
        if not cards:
            return Status("not loaded", NEVER)
        return Status(f"{cards:,} cards · {when(stamp)}", READY)

    if op.id == "prices.market_sweep":
        stamp, snapshots = _history_facts(history_conn)
        if stamp is None:
            return Status("never", NEVER)
        return Status(f"{when(stamp)} · {snapshots:,} points", _staleness(op, stamp))

    if op.id == "backup.database":
        count, stamp, total = backup_facts()
        if not count:
            return Status("none yet", NEVER)
        return Status(
            f"{count} kept · newest {when(stamp)} · {human_bytes(total)}", READY
        )

    if op.id == "shows.refresh":
        from tcginventory.data.repositories import CardShowRepository

        upcoming = CardShowRepository(conn).count_from(_today())
        row = OperationRunRepository(conn).last_run(op.id)
        if row is None:
            return Status("never scraped", NEVER)
        return Status(
            f"{upcoming:,} upcoming · {when(row['started_at'])}",
            STALE if not row["ok"] else _staleness(op, row["started_at"]),
        )

    if op.id in ("models.training_env", "models.training_env_gpu"):
        return _training_env_status(op)

    if op.id == "models.training_env_remove":
        from tcginventory.services import trainingenv

        if not trainingenv.is_bootstrapped():
            return Status(
                "nothing to remove",
                NEVER,
                detail="No training environment is installed.",
                actionable=False,
            )
        size = trainingenv.env_size()
        return Status(
            human_bytes(size),
            READY,
            detail=(
                f"Removes {trainingenv.env_dir()} only. Trained models, the "
                "labelled corpus and the training history all stay."
            ),
        )

    if op.id == "models.detector.train":
        return _model_status(op, KIND_DETECTOR)

    if op.id == "models.condition.train":
        return _model_status(op, KIND_CONDITION)

    if op.id in ("identity.index_catalog", "identity.reclaim"):
        return _catalog_index_status(conn, op)

    if op.id in ("identity.fit_projection", "identity.forget_projection"):
        return _projection_status(op)

    if op.id == "vision.reindex":
        captures, regions, exemplars = corpus_facts()
        if not regions:
            return Status("no labelled cards yet", NEVER)
        return Status(
            f"{captures:,} captures · {regions:,} labelled · {exemplars:,} indexed",
            READY if exemplars else STALE,
            detail=(
                None
                if exemplars
                else "Labelled cards exist but none are indexed — re-index to use them"
            ),
        )

    if op.id == "vision.compact_index":
        return _compact_index_status()

    if op.id == "db.vacuum":
        from tcginventory import config

        try:
            size = human_bytes(config.db_path().stat().st_size)
        except OSError:
            return Status("in memory", READY)
        row = OperationRunRepository(conn).last_run(op.id)
        suffix = f" · compacted {when(row['started_at'])}" if row else ""
        return Status(f"{size}{suffix}", READY)

    if op.id == "automation.daily_prices":
        from tcginventory import scheduling

        if not scheduling.is_supported():
            return Status("Windows only", OFF)
        if scheduling.is_enabled():
            return Status(f"daily at {scheduling.DEFAULT_TIME}", ON)
        return Status("not scheduled", OFF)

    row = OperationRunRepository(conn).last_run(op.id)
    if row is None:
        return Status("never", NEVER)
    summary = row["summary"]
    value = when(row["started_at"])
    if summary:
        value = f"{value} · {summary}"
    # A run that succeeded can still be too old to rely on — that is the whole
    # point of a window on the full-backup export.
    state = _staleness(op, row["started_at"]) if op.stale_after_days else READY
    return Status(value, STALE if not row["ok"] else state, detail=summary)


def _training_env_status(op: Operation) -> Status:
    """The environment pair: build one, or swap the one you have for the other.

    The two tiles describe the same environment from opposite sides, so each reads
    the *installed* flavour and offers the other. Whichever flavour is in place is
    the tile that has nothing to do.
    """
    from tcginventory.services import training as training_svc
    from tcginventory.services import trainingenv

    wants_gpu = op.id == "models.training_env_gpu"
    gpu = trainingenv.detect_gpu()
    env = training_svc.training_env_summary()
    # An environment built before GPU support carries no recorded flavour, and
    # CPU is the only thing it could have been.
    installed = (
        (training_svc.training_env_flavour() or trainingenv.FLAVOUR_CPU)
        if env.ready
        else None
    )

    if wants_gpu:
        if gpu is None:
            return Status(
                "no suitable GPU detected",
                OFF,
                actionable=False,
                detail=(
                    "Training on the GPU needs an NVIDIA card with its driver "
                    "installed. Nothing that nvidia-smi can see is present, so the "
                    "processor is the only option here."
                ),
            )
        if not gpu.usable:
            return Status(
                f"{gpu.label} — too little memory",
                OFF,
                actionable=False,
                detail=(
                    f"{gpu.name} reports {gpu.vram_mb:,} MB; training wants at least "
                    f"{trainingenv.MIN_VRAM_MB:,} MB to be worth the trouble."
                ),
            )
        if installed == trainingenv.FLAVOUR_GPU:
            return Status(
                f"in use · {gpu.label}",
                READY,
                actionable=False,
                detail=f"Training already runs on {gpu.name}.",
            )
        if installed == trainingenv.FLAVOUR_CPU:
            return Status(
                f"available · {gpu.label}",
                STALE,
                verb="Switch to GPU environment",
                detail=(
                    f"Rebuilds the environment against CUDA so training uses "
                    f"{gpu.name}. Downloads about 2.5 GB and replaces what is "
                    "installed now; nothing else is touched."
                ),
            )
        return Status(
            f"ready to build · {gpu.label}",
            NEVER,
            detail=f"Will build against CUDA for {gpu.name}. About 2.5 GB, once.",
        )

    # The CPU tile.
    if installed == trainingenv.FLAVOUR_CPU:
        return Status(
            env.detail,
            READY,
            actionable=False,
            detail=(
                f"Already set up at {env.python} — remove the environment first if "
                "you want to rebuild it."
            ),
        )
    if installed == trainingenv.FLAVOUR_GPU:
        return Status(
            env.detail,
            READY,
            verb="Switch to CPU environment",
            detail=(
                "Rebuilds the environment against the CPU build of torch. Smaller "
                "on disk, and the only option if the card is needed elsewhere."
            ),
        )
    found = trainingenv.discover_interpreters()
    if not found:
        # No interpreter anywhere: the one case the app cannot fix itself, since a
        # frozen build ships no python.exe to make a virtualenv with.
        return Status(
            "no suitable Python detected", OFF, detail=trainingenv.NO_PYTHON_HINT
        )
    detail = (
        f"Will use {found[0].label} — {found[0].path}. About 280 MB of downloads, once."
    )
    if trainingenv.is_bootstrapped():
        return Status("set up, but incomplete", STALE, detail=detail)
    return Status(f"not set up · {found[0].label} found", NEVER, detail=detail)


def _catalog_index_status(conn, op: Operation) -> Status:
    """How much of the catalog recognition can currently reach."""
    from tcginventory.services import catalog_index as ci

    vision = _open_vision()
    if vision is None:
        return Status("unavailable", NEVER, actionable=False)
    try:
        from tcginventory.data.vision import VisionRepository

        repo = VisionRepository(vision)
        # Only the reclaim tile reports on the art on disk, and counting it means
        # a stat per file — two seconds across a full catalog. Every other caller
        # wants the two counts, which are a pair of COUNT(*)s.
        wants_archive = op.id == "identity.reclaim"
        status = ci.index_status(conn, repo, with_archive=wants_archive)
        if op.id == "identity.reclaim":
            if not status.archived:
                return Status(
                    "nothing archived yet",
                    NEVER,
                    actionable=False,
                    detail="There is no catalog art to reclaim until the index runs.",
                )
            keep = len(ci.protected_card_ids(conn, repo))
            return Status(
                f"{status.archived:,} cards · {status.archive_bytes / 1e9:.2f} GB",
                READY,
                detail=(
                    f"{keep:,} cards are protected — labelled, owned, or already "
                    "downloaded before the sweep. The rest can go."
                ),
            )
        if not status.indexed:
            return Status(
                f"not indexed · {status.catalog:,} cards available",
                NEVER,
                detail=(
                    "Suggestions can only offer cards you have already labelled "
                    "until this runs."
                ),
            )
        share = 100 * status.indexed / max(1, status.catalog)
        state = READY if status.complete else STALE
        return Status(
            f"{status.indexed:,} of {status.catalog:,} cards ({share:.0f}%)",
            state,
            detail=f"Indexed for {status.model_version}.",
        )
    finally:
        vision.close()


def _projection_status(op: Operation) -> Status:
    """The photo-matching map, reported like the models it sits beside.

    It is a learned artifact, so it states the same things they do: when it was
    fitted, what it was fitted on, and what it scored. The score is **held-out**
    — fitting uses the labelled crops, which are also the benchmark's queries, so
    an in-sample number would only be marking its own homework.
    """
    from tcginventory.services import identity_projection as ip

    installed = ip.matrix_path().is_file()
    removing = op.id == "identity.forget_projection"
    if removing and not installed:
        return Status("not fitted", NEVER, actionable=False)

    try:
        from tcginventory.data.vision import KIND_IDENTITY, ModelRunRepository

        conn = _open_vision()
        if conn is None:
            return Status("unavailable", NEVER)
        try:
            rows = [
                row
                for row in ModelRunRepository(conn).recent(KIND_IDENTITY, limit=40)
                if row["source"] == "projection" and row["status"] == "ok"
            ]
        finally:
            conn.close()
    except Exception:  # pragma: no cover - vision DB optional
        rows = []

    if not installed:
        return Status("never fitted", NEVER)
    if not rows:
        return Status("fitted", READY)

    row = rows[0]
    value = when(row["started_at"])
    if row["map50"] is not None and row["precision"] is not None:
        value += (
            f" · top-1 {row['precision']:.0%} → {row['map50']:.0%}"
            if row["map50"] != row["precision"]
            else f" · top-1 {row['map50']:.0%}"
        )
    if removing:
        return Status(value, READY, detail=row["notes"])
    state = _staleness(op, row["started_at"]) if op.stale_after_days else READY
    return Status(value, state, detail=row["notes"])


def _model_status(op: Operation, kind: str) -> Status:
    """One recognition model as a tile: version, provenance, and how old it is.

    The value line always carries **when it was last trained** — the marker the
    Regions and Conditions sections are read for — and the state turns STALE
    either because the corpus has moved on or because the model is older than
    ``stale_after_days``. Best-effort: the vision DB is optional, so any failure
    reads as "never" rather than breaking the window.
    """
    try:
        from tcginventory.data.vision import ModelRunRepository, VisionRepository
        from tcginventory.services import training as training_svc

        conn = _open_vision()
        if conn is None:
            return Status("unavailable", NEVER)
        try:
            repo = VisionRepository(conn)
            runs = ModelRunRepository(conn)
            if kind == KIND_CONDITION:
                from tcginventory.services import condition_dataset as cds

                samples = cds.collect_samples(repo)
                gate = cds.check_gate(samples)
                current = runs.current_model(kind)
                cards = len(samples)
                if current is None:
                    if not cards:
                        return Status(
                            "never · no graded cards yet",
                            NEVER,
                            actionable=False,
                            detail=(
                                "Photograph cards front and back in Recognition and "
                                "grade them: the back's grade is the one that counts, "
                                "and the grader reads it together with the front."
                            ),
                        )
                    return Status(
                        f"never · {cards:,} graded cards",
                        NEVER,
                        actionable=gate.ok,
                        detail=(
                            gate.summary
                            if gate.ok
                            else "Still needed — " + gate.summary
                        ),
                    )
                distance = current["map50_95"]
                value = f"v{current['version']} · {when(current['finished_at'])}"
                if distance is not None:
                    value += f" · {distance:.2f} grades off"
                return Status(
                    value,
                    _staleness(op, current["finished_at"]),
                    actionable=gate.ok,
                    detail=(
                        f"Trained on {current['corpus_cards'] or 0:,} cards; the "
                        f"corpus now holds {cards:,}."
                        + ("" if gate.ok else f" Still needed — {gate.summary}.")
                    ),
                )

            status = training_svc.detector_status(repo, runs)
            corpus = f"{status.corpus.images:,} images / {status.corpus.boxes:,} boxes"
            if status.never_trained:
                if status.installed:
                    # A model file is on disk, and Recognition proposes boxes
                    # from it (it looks the file up directly). Calling that
                    # "never run" contradicts the window next door. The run log
                    # is what's missing — say that instead.
                    return Status(
                        "installed · not in the run log",
                        READY,
                        detail=(
                            f"{status.model_path} is in use for box proposals, but "
                            "no training run claims it, so its version and accuracy "
                            f"are unknown. Re-train to record them. Corpus: {corpus}."
                        ),
                    )
                detail = (
                    f"{corpus} ready to train on."
                    if status.corpus.trainable
                    else "Draw and save boxes in Recognition first."
                )
                return Status(f"never · {corpus} ready", NEVER, detail=detail)
            age = _staleness(op, status.trained_at)
            state = STALE if (status.stale or age == STALE) else READY
            value = f"v{status.version} · {when(status.trained_at)}"
            if status.map50_95 is not None:
                value += f" · mAP50-95 {status.map50_95:.3f}"
            trained_on = status.trained_on
            detail = (
                f"Trained on {trained_on.images:,} images / {trained_on.boxes:,} boxes"
                f" / {trained_on.cards:,} cards. Corpus now: {corpus}."
                if trained_on
                else None
            )
            if status.stale and detail:
                detail += (
                    f" New since: {status.new_images:,} images, "
                    f"{status.new_boxes:,} boxes."
                )
            if not status.installed and detail:
                detail += " The model file is missing — re-train to restore it."
            return Status(value, state, detail=detail)
        finally:
            conn.close()
    except Exception:  # pragma: no cover - the vision DB is optional
        return Status("unavailable", NEVER)


def _open_vision():
    """The vision DB, or None when it isn't there yet."""
    from tcginventory.data.vision import open_vision, vision_path

    try:
        if not vision_path().exists():
            return None
        return open_vision()
    except Exception:  # pragma: no cover - unreadable/locked file
        return None


def _history_facts(history_conn=None) -> tuple[str | None, int]:
    """Newest per-set sweep timestamp + snapshot count from the price-history DB.

    Best-effort: the file is optional and lives outside the ledger, so any
    failure just reports "never" rather than breaking the window.
    """
    from tcginventory.data.pricehistory import PriceHistoryRepository, history_path

    own = False
    conn = history_conn
    try:
        if conn is None:
            if not history_path().exists():
                return None, 0
            from tcginventory.data.pricehistory import open_history

            conn = open_history()
            own = True
        repo = PriceHistoryRepository(conn)
        last = repo.set_last_priced()
        newest = max(last.values()) if last else None
        return newest, repo.snapshot_count()
    except Exception:
        return None, 0
    finally:
        if own and conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# the fact strip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    caption: str
    value: str
    tone: str | None = None  # None | "pos"


def system_facts(conn) -> list[Fact]:
    """The state-of-this-install line above the tiles."""
    from tcginventory import config
    from tcginventory.data.migrator import current_version
    from tcginventory.data.repositories import (
        CardCatalogRepository,
        ItemRepository,
        LotRepository,
        PriceSnapshotRepository,
    )

    db = config.db_path()
    try:
        size = human_bytes(db.stat().st_size)
        where = f"{db.name} · {size}"
    except OSError:
        where = "in memory"

    items = ItemRepository(conn).count()
    copies = LotRepository(conn).on_hand_total()
    catalog = CardCatalogRepository(conn)
    en_cards, _ = catalog.stats("EN")
    jp_cards, _ = catalog.stats("JP")
    languages = " + ".join(
        code for code, n in (("EN", en_cards), ("JP", jp_cards)) if n
    )
    priced = PriceSnapshotRepository(conn).latest_as_of()

    return [
        Fact("Database", where),
        Fact("Items", f"{items:,} cards · {copies:,} copies"),
        Fact("Prices updated", when(priced), "pos" if priced else None),
        Fact(
            "Card database",
            f"{en_cards + jp_cards:,} cards · {languages}" if languages else "empty",
        ),
        Fact("Schema", f"v{current_version(conn)}"),
    ]


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


def record(
    conn,
    op_id: str,
    *,
    ok: bool = True,
    summary: str | None = None,
    source: str = "ui",
    started_at: str | None = None,
) -> None:
    """Append a run to the log and commit it — best-effort.

    Called from the window, from the daily background task and from the startup
    backup, so the Activity list is the whole story rather than only what
    happened while a window was open. A logging failure must never take down the
    operation it is describing.
    """
    from tcginventory.data.repositories import OperationRunRepository

    try:
        repo = OperationRunRepository(conn)
        repo.record(op_id, ok=ok, summary=summary, source=source, started_at=started_at)
        repo.prune()
        conn.commit()
    except Exception:  # pragma: no cover - defensive
        pass


_DIGEST_SEEN = "operations_digest_seen"  # prefs key: last acknowledged run id


@dataclass(frozen=True)
class Digest:
    """What ran in the background since the user last looked."""

    lines: list[str]
    failures: int
    newest: int  # the run id to acknowledge — ids, not timestamps, because
    # ``now_iso`` is second-precision and a nightly task can log several runs
    # inside one second, which a timestamp cutoff would swallow.

    @property
    def headline(self) -> str:
        count = len(self.lines)
        word = "run" if count == 1 else "runs"
        text = f"While you were away — {count} background {word}"
        if self.failures:
            text += f", {self.failures} failed"
        return text


def digest(conn, limit: int = 6) -> Digest | None:
    """Background/startup work the user hasn't acknowledged yet, or None.

    The daily task runs with the app closed, so its results would otherwise be
    invisible until someone scrolled Activity. Reads the ``scheduled``/``startup``
    runs newer than the last acknowledgement (see :func:`acknowledge_digest`).
    """
    from tcginventory import config
    from tcginventory.data.repositories import OperationRunRepository

    try:
        seen = int(config.get_pref(_DIGEST_SEEN, 0) or 0)
    except (TypeError, ValueError):
        seen = 0
    rows = [
        row
        for row in OperationRunRepository(conn).recent(60)
        if row["source"] in ("scheduled", "startup") and row["id"] > seen
    ]
    if not rows:
        return None
    lines = []
    for row in rows[:limit]:
        op = BY_ID.get(row["op"])
        title = op.title if op else row["op"]
        lines.append(f"{title} — {row['summary'] or 'finished'}")
    return Digest(
        lines=lines,
        failures=sum(1 for row in rows if not row["ok"]),
        newest=max(row["id"] for row in rows),
    )


def acknowledge_digest(run_id: int) -> None:
    """Mark everything up to ``run_id`` as seen, so the banner stays dismissed."""
    from tcginventory import config

    config.set_pref(_DIGEST_SEEN, int(run_id))


def activity(conn, limit: int = 8) -> list[tuple[str, str, bool]]:
    """Recent runs as ``(when, message, ok)`` for the Activity list."""
    from tcginventory.data.repositories import OperationRunRepository

    lines: list[tuple[str, str, bool]] = []
    for row in OperationRunRepository(conn).recent(limit):
        op = BY_ID.get(row["op"])
        title = op.title if op else row["op"]
        summary = row["summary"] or ("finished" if row["ok"] else "failed")
        suffix = "" if row["source"] == "ui" else f"  ({row['source']})"
        lines.append(
            (when(row["started_at"]), f"{title} — {summary}{suffix}", bool(row["ok"]))
        )
    return lines


# Runners live in the view (they need Qt for dialogs and worker threads); the
# registry only says *what* exists. This is the seam a future operation plugs
# into: add an Operation above, add its runner to ui/views/operations.py.
Runner = Callable[[], None]
