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


# Display order of the sections. ``models`` is **reserved** for the coming model
# metrics/triggers work: a group with no operations renders nothing, so it costs
# nothing until its first entry lands (see docs/decisions.md, 2026-08-04).
GROUPS: tuple[Group, ...] = (
    Group("data_in", "Bring data in"),
    Group("data_out", "Get data out"),
    Group("prices", "Prices & card database"),
    Group("automation", "Automation"),
    Group("maintenance", "Maintenance & diagnostics"),
    Group("models", "Models & signals"),
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
        group="data_in",
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
        group="prices",
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
        group="prices",
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
        id="vision.reindex",
        group="maintenance",
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
