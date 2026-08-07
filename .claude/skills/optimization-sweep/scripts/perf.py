"""Measurement harness for the optimization sweep.

Answers the only question that licenses an edit: *what actually costs anything?*
Three axes, because they disagree — a view can build in 1.5ms and cost 100MB, and
a table can be tiny on disk and still dominate a refresh.

    perf.py latency [--views] [--services]   wall clock: view build/refresh, services
    perf.py memory                           resident memory, staged and settled
    perf.py storage                          bytes on disk: databases, caches, indexes
    perf.py profile <View>                   cProfile one view's build and refresh
    perf.py plans                            EXPLAIN QUERY PLAN over the big tables
    perf.py capabilities                     what is available without a new dependency
    perf.py all                              latency + memory + storage

Never opens the live database. ``latency``/``memory``/``profile``/``plans`` copy
it to a temp file first and point ``TCGINVENTORY_DB`` there; ``storage`` opens
every database read-only (``mode=ro``) and only ever reads. Nothing here writes
to the user's app data.

Stdlib only, except that the view harness imports PySide6 — already a hard
dependency of the app.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Class -> module. Note two traps: PriceLookupView lives in views/market.py while
# MarketView lives in views/market_signal.py, and the nav label is "eBay
# Auctions" but the module is auctions. Kept in NAV_ITEMS order.
VIEWS: list[tuple[str, str, str]] = [
    ("Dashboard", "dashboard", "DashboardView"),
    ("Transaction", "transaction", "TransactionView"),
    ("Inventory", "inventory", "InventoryView"),
    ("History", "history", "HistoryView"),
    ("Calendar", "calendar", "CalendarView"),
    ("Price Lookup", "market", "PriceLookupView"),
    ("eBay Auctions", "auctions", "AuctionsView"),
    ("Recognition", "recognition", "RecognitionView"),
    ("Market", "market_signal", "MarketView"),
    ("Models", "models", "ModelsView"),
    ("Operations", "operations", "OperationsView"),
]

# Service entry points a window actually calls on refresh. (module, class|None,
# attribute). A None class means a module-level function.
SERVICE_CALLS: list[tuple[str, str | None, str]] = [
    ("services.inventory", "InventoryService", "inventory_rows"),
    ("services.inventory", "InventoryService", "all_positions"),
    ("services.inventory", "InventoryService", "sellable_items"),
    ("services.reporting", "ReportingService", "portfolio_summary"),
    ("services.reporting", "ReportingService", "collection_value_series"),
    ("services.reporting", "ReportingService", "cost_basis_series"),
    ("services.reporting", "ReportingService", "money_movement_series"),
    ("services.reporting", "ReportingService", "realized_profit_series"),
    ("services.reporting", "ReportingService", "price_movers"),
    ("services.reporting", "ReportingService", "top_gainers"),
    ("services.reporting", "ReportingService", "top_sellers"),
    ("services.market_signal", "MarketSignalService", "overview"),
    ("services.market_signal", "MarketSignalService", "signals"),
]

# Queries worth a plan: every one touches a table with tens of thousands of rows.
# A SCAN here is a finding; a SCAN on `item` or `lot` is 900 rows and is not.
PLAN_QUERIES: list[tuple[str, str, tuple]] = [
    (
        "image_for: set gather",
        "SELECT card_id, name, set_name, number, image_small, printed_total "
        "FROM card_catalog WHERE set_name = ? COLLATE NOCASE "
        "AND image_small IS NOT NULL",
        ("Base",),
    ),
    (
        "image_for: name gather",
        "SELECT card_id, name, set_name, number, image_small, printed_total "
        "FROM card_catalog WHERE name = ? COLLATE NOCASE AND image_small IS NOT NULL",
        ("Pikachu",),
    ),
    (
        "image_for: number gather",
        "SELECT card_id, name, set_name, number, image_small, printed_total "
        "FROM card_catalog WHERE (number = ? OR number = ?) "
        "AND image_small IS NOT NULL LIMIT 400",
        ("4", "4"),
    ),
    (
        "image_for: LIKE fallback",
        "SELECT card_id FROM card_catalog WHERE name LIKE ? "
        "AND image_small IS NOT NULL LIMIT 50",
        ("%Pikachu%",),
    ),
    (
        "catalog search by set+number",
        "SELECT card_id FROM card_catalog WHERE set_name = ? AND number = ?",
        ("Base", "4"),
    ),
    (
        "latest price per item",
        "SELECT item_id, MAX(as_of) FROM price_snapshot GROUP BY item_id",
        (),
    ),
]

BIG_TABLE = 20_000  # rows above which query shape and index choice actually matter

_LIVE_DB: Path | None = None  # the real inventory.db, resolved before we override it


# ---- plumbing ---------------------------------------------------------------


def _data_dir() -> Path:
    from tcginventory import config

    return Path(config.data_dir())


def _stage_db(tmp: Path) -> Path:
    """Copy the live inventory.db somewhere disposable and point the app at it.

    The sweep profiles real data or it profiles nothing, but it must never hold a
    handle on the file the app writes to. Falls back to a migrated empty database
    when there is no live one, which is honest but worth saying out loud.
    """
    global _LIVE_DB
    from tcginventory import config

    # Resolved once: this function sets TCGINVENTORY_DB, so a second call would
    # otherwise "stage" the previous call's temp copy.
    if _LIVE_DB is None:
        _LIVE_DB = Path(config.db_path())
    live = _LIVE_DB
    staged = tmp / "inventory.db"
    if live.is_file():
        shutil.copy2(live, staged)
        for suffix in ("-wal", "-shm"):
            side = live.with_name(live.name + suffix)
            if side.is_file():
                shutil.copy2(side, staged.with_name(staged.name + suffix))
        print(f"staged a copy of {live} ({live.stat().st_size / 1e6:.1f} MB)\n")
    else:
        from tcginventory.data.db import open_database

        open_database(staged).close()
        print("NO LIVE DATABASE — measuring an empty one. Numbers mean nothing.\n")
    os.environ["TCGINVENTORY_DB"] = str(staged)
    return staged


def _conn(path: Path) -> sqlite3.Connection:
    """A connection to the staged copy.

    ``data.db.connect()`` defaults to ``:memory:``. Calling it bare gives you an
    empty database that answers every query instantly — the classic way to
    measure nothing and believe it.
    """
    from tcginventory.data.db import connect

    return connect(path)


def _qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _build_view(name: str, module: str, cls: str, conn):
    import importlib

    mod = importlib.import_module(f"tcginventory.ui.views.{module}")
    factory = getattr(mod, cls)
    if name == "eBay Auctions":
        from tcginventory.data.auctions import open_auctions

        return factory(conn, auctions_conn=open_auctions(":memory:"))
    if name == "Recognition":
        return factory(conn, prewarm=True)
    return factory(conn)


def _best_ms(fn, runs: int) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best * 1000


def _rss_mb() -> float:
    """Resident set size. Windows is the primary dev box; POSIX falls back to
    ``resource``, whose ru_maxrss is a high-water mark, not a live reading."""
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as wt

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wt.DWORD),
                ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        fn = ctypes.windll.kernel32.K32GetProcessMemoryInfo
        fn.argtypes = [wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]
        fn.restype = wt.BOOL
        counters = _PMC()
        counters.cb = ctypes.sizeof(counters)
        fn(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
        return counters.WorkingSetSize / 1e6
    import resource

    scale = 1e6 if sys.platform == "darwin" else 1e3
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale


def _settle(app, seconds: float = 2.0) -> None:
    """Let deferred work land before reading RSS.

    Prewarming and worker threads allocate after the constructor returns, so a
    naive delta bills the *next* view for memory the previous one asked for.
    """
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.05)


# ---- latency ----------------------------------------------------------------


def cmd_latency(args) -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        staged = _stage_db(Path(tmp))
        do_views = args.views or not args.services
        do_services = args.services or not args.views
        if do_services:
            _latency_services(staged)
        if do_views:
            _latency_views(staged)


def _latency_services(staged: Path) -> None:
    import importlib

    conn = _conn(staged)
    print("SERVICES  (best of 5)")
    results = []
    for module, cls, attr in SERVICE_CALLS:
        try:
            mod = importlib.import_module(f"tcginventory.{module}")
            owner = getattr(mod, cls)(conn) if cls else mod
            fn = getattr(owner, attr)
            fn()  # warm
            results.append((_best_ms(fn, 5), f"{cls or module}.{attr}()"))
        except Exception as exc:  # a signature change is a finding, not a crash
            print(
                f"      skip  {cls or module}.{attr}()  ({type(exc).__name__}: {exc})"
            )
    for ms, label in sorted(results, reverse=True):
        print(f"  {ms:9.1f} ms  {label}")
    print()


def _latency_views(staged: Path) -> None:
    _qt_app()  # a QApplication must exist before any widget is constructed
    conn = _conn(staged)
    print("VIEWS  (single run — construction is not idempotent)")
    built, total = {}, 0.0
    for name, module, cls in VIEWS:
        start = time.perf_counter()
        try:
            built[name] = _build_view(name, module, cls, conn)
        except Exception as exc:
            print(f"      ERROR  build  {name}: {type(exc).__name__}: {exc}")
            continue
        elapsed = (time.perf_counter() - start) * 1000
        total += elapsed
        print(f"  {elapsed:9.1f} ms  build    {name}")
    print(f"  {total:9.1f} ms  TOTAL BUILD  (paid on every launch)")
    print(
        "  Construction runs once, and Recognition's prewarm thread competes with\n"
        "  whatever is built after it — individual rows move by 100ms+ between runs.\n"
        "  Trust the total and the outliers; re-run before believing a small gap.\n"
    )

    for name, view in built.items():
        fn = getattr(view, "refresh", None)
        if fn is None:
            continue
        try:
            elapsed = _best_ms(fn, 1)
            print(f"  {elapsed:9.1f} ms  refresh  {name}")
        except Exception as exc:
            print(f"      ERROR  refresh  {name}: {type(exc).__name__}: {exc}")
    print()


# ---- memory -----------------------------------------------------------------


def cmd_memory(args) -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        staged = _stage_db(Path(tmp))
        print(f"  {_rss_mb():8.1f} MB  interpreter")
        app = _qt_app()
        print(f"  {_rss_mb():8.1f} MB  + QApplication")
        conn = _conn(staged)

        import importlib

        for _, module, _cls in VIEWS:
            importlib.import_module(f"tcginventory.ui.views.{module}")
        print(f"  {_rss_mb():8.1f} MB  + view modules imported\n")

        keep, previous = [], _rss_mb()
        for name, module, cls in VIEWS:
            try:
                keep.append(_build_view(name, module, cls, conn))
            except Exception as exc:
                print(f"       ERROR  {name}: {type(exc).__name__}: {exc}")
                continue
            _settle(app)  # so async allocation is billed to whoever asked for it
            now = _rss_mb()
            print(
                f"  {now - previous:+8.1f} MB  build {name:<14s} (total {now:.0f} MB)"
            )
            previous = now
        print(f"\n  {previous:8.1f} MB  RESIDENT with all views built")
        print(
            "\n  A delta is only trustworthy once the process has settled. If one\n"
            "  looks wrong, isolate it: build that view alone in a fresh process,\n"
            "  and re-measure after two seconds of processEvents()."
        )


# ---- storage ----------------------------------------------------------------


def _payload_bytes(conn: sqlite3.Connection, table: str) -> tuple[int, int]:
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    if not cols:
        return 0, 0
    expr = "+".join(f'COALESCE(LENGTH("{c}"),0)' for c in cols)
    rows, total = conn.execute(
        f'SELECT COUNT(*), SUM({expr}) FROM "{table}"'
    ).fetchone()
    return rows or 0, total or 0


def _redundant_indexes(conn: sqlite3.Connection) -> list[str]:
    """Indexes whose column list is a prefix of another on the same table.

    A prefix index earns nothing a longer one does not already serve, and every
    one of them is paid for again on each insert.
    """
    by_table: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for name, table in conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        cols = tuple(r[2] for r in conn.execute(f'PRAGMA index_info("{name}")'))
        by_table.setdefault(table, []).append((name, cols))
    findings = []
    for table, entries in by_table.items():
        covered: set[str] = set()
        for name, cols in entries:
            if name in covered:
                continue  # already reported as the shorter half of a pair
            for other, other_cols in entries:
                if other == name or len(other_cols) < len(cols):
                    continue
                if other_cols[: len(cols)] == cols:
                    covered.add(other if len(other_cols) == len(cols) else name)
                    verb = "duplicates" if other_cols == cols else "is a prefix of"
                    findings.append(
                        f"{table}.{name} ({', '.join(cols)}) {verb} "
                        f"{other} ({', '.join(other_cols)})"
                    )
                    break
    return findings


def cmd_storage(args) -> None:
    data = _data_dir()
    print(f"APP DATA  {data}\n")

    from tcginventory import config

    databases = [
        ("inventory.db", Path(config.db_path())),
        ("vision.db", data / "vision.db"),
        ("auctions.db", data / "auctions.db"),
        ("pricehistory.db", data / "pricehistory.db"),
    ]
    for label, path in databases:
        if not path.is_file():
            print(f"== {label}: absent\n")
            continue
        size = path.stat().st_size
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        reclaim = free * page_size / 1e6
        print(
            f"== {label}: {size / 1e6:.1f} MB   "
            f"free pages {free:,} ({reclaim:.1f} MB reclaimable by VACUUM)"
        )
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        measured = []
        for table in tables:
            rows, payload = _payload_bytes(conn, table)
            measured.append((payload, rows, table))
        for payload, rows, table in sorted(measured, reverse=True)[:6]:
            flag = "  <-- big" if rows >= BIG_TABLE else ""
            per = f"{payload / rows:,.0f} B/row" if rows else "-"
            print(
                f"     {payload / 1e6:8.2f} MB payload  {rows:>9,} rows  "
                f"{per:>12s}  {table}{flag}"
            )
        for finding in _redundant_indexes(conn):
            print(f"     REDUNDANT INDEX  {finding}")
        conn.close()
        print()

    print("DIRECTORIES")
    entries = []
    for root, _dirs, files in os.walk(data):
        total = 0
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
        if total:
            entries.append((total, len(files), root))
    grand = sum(e[0] for e in entries)
    for total, count, root in sorted(entries, reverse=True)[:10]:
        rel = os.path.relpath(root, data)
        print(f"  {total / 1e6:9.1f} MB  {count:>7,} files  {rel}")
    print(f"  {grand / 1e6:9.1f} MB  TOTAL")


# ---- profile / plans --------------------------------------------------------


def cmd_profile(args) -> None:
    import cProfile
    import pstats

    match = [v for v in VIEWS if v[0].lower().startswith(args.view.lower())]
    if not match:
        print(f"unknown view {args.view!r}. Known: {', '.join(v[0] for v in VIEWS)}")
        raise SystemExit(2)
    name, module, cls = match[0]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        staged = _stage_db(Path(tmp))
        _qt_app()
        conn = _conn(staged)

        profiler = cProfile.Profile()
        profiler.enable()
        view = _build_view(name, module, cls, conn)
        profiler.disable()
        print(f"=== {name}: build ===")
        pstats.Stats(profiler).sort_stats("cumulative").print_stats(args.rows)

        fn = getattr(view, "refresh", None)
        if fn is None:
            return
        profiler = cProfile.Profile()
        profiler.enable()
        fn()
        profiler.disable()
        print(f"=== {name}: refresh ===")
        pstats.Stats(profiler).sort_stats("cumulative").print_stats(args.rows)


def cmd_plans(args) -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        staged = _stage_db(Path(tmp))
        conn = sqlite3.connect(staged)
        sizes = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for (table,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        print("TABLE SIZES  (a plan only matters on a big table)")
        for table, count in sorted(sizes.items(), key=lambda kv: -kv[1]):
            flag = "  <-- big" if count >= BIG_TABLE else ""
            print(f"  {count:>9,}  {table}{flag}")
        print()
        for label, sql, params in PLAN_QUERIES:
            print(f"Q: {label}")
            try:
                for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params):
                    detail = row[-1]
                    mark = "  <-- SCAN" if detail.startswith("SCAN") else ""
                    print(f"     {detail}{mark}")
            except sqlite3.Error as exc:
                print(f"     error: {exc}")
        conn.close()
        print(
            "\nA SCAN over a table under "
            f"{BIG_TABLE:,} rows is not a finding. Say so and move on."
        )


def cmd_capabilities(args) -> None:
    """What this app can already do without a new dependency.

    The research half needs grounding as much as the measurement half. Most
    "use library X" ideas die on packaging; most of the rest turn out to be
    something numpy, Pillow, Qt or SQLite already ships. Probe, don't assume —
    and note the absences, because a missing compile option is the difference
    between a good idea and an afternoon lost.
    """
    import sqlite3 as sq

    print(f"python      {sys.version.split()[0]}")
    conn = sq.connect(":memory:")
    print(f"sqlite      {sq.sqlite_version}")

    options = [r[0] for r in conn.execute("PRAGMA compile_options")]
    for label, needle in (
        ("FTS5 full-text", "ENABLE_FTS5"),
        ("R*Tree", "ENABLE_RTREE"),
        ("dbstat vtab", "ENABLE_DBSTAT_VTAB"),
        ("STAT4 planner stats", "ENABLE_STAT4"),
        ("math functions", "ENABLE_MATH_FUNCTIONS"),
    ):
        present = any(needle in option for option in options)
        print(f"   {'yes' if present else 'NO ':3s}  {label}")

    probes = [
        ("JSON functions", "SELECT json_extract('{\"a\":1}', '$.a')"),
        ("generated columns", "CREATE TABLE _g(a INT, b INT AS (a*2))"),
        ("WITHOUT ROWID", "CREATE TABLE _w(k TEXT PRIMARY KEY, v INT) WITHOUT ROWID"),
        ("expression indexes", "CREATE TABLE _e(x TEXT)"),
    ]
    for label, sql in probes:
        try:
            conn.execute(sql)
            print(f"   yes  {label}")
        except sq.Error as exc:
            print(f"   NO   {label} ({exc})")
    print(f"   {'yes' if hasattr(conn, 'blobopen') else 'NO '}  incremental BLOB I/O")
    conn.close()

    print("\nalready a hard dependency — free to use")
    for module in ("numpy", "PIL", "PySide6", "httpx", "onnxruntime"):
        try:
            mod = __import__(module)
            print(f"   {module:14s} {getattr(mod, '__version__', '?')}")
        except ImportError:
            print(f"   {module:14s} MISSING")

    try:
        from PIL import features

        pillow = [f for f in ("webp", "jpg", "avif", "zlib") if features.check(f)]
        print(f"   Pillow writes  {', '.join(pillow)}")
    except Exception as exc:
        print(f"   Pillow probe failed: {exc}")

    try:
        _qt_app()
        from PySide6.QtGui import QImageReader, QImageWriter

        readable = {bytes(f).decode() for f in QImageReader.supportedImageFormats()}
        writable = {bytes(f).decode() for f in QImageWriter.supportedImageFormats()}
        interesting = ("png", "jpg", "webp", "avif", "jxl")
        both = [f for f in interesting if f in readable and f in writable]
        neither = [f for f in interesting if f not in readable]
        print(f"   Qt read+write  {', '.join(both)}")
        print(f"   Qt CANNOT read {', '.join(neither)}")
        print(
            "\n   An image format is only usable if BOTH Pillow and Qt handle it.\n"
            "   Pillow writes AVIF and Qt cannot read it — smaller files the UI\n"
            "   would not be able to display."
        )
    except Exception as exc:
        print(f"   Qt probe failed: {exc}")


def cmd_all(args) -> None:
    """Run each axis in a fresh subprocess.

    Not tidiness — correctness. Sharing a process makes the memory numbers a lie
    twice over: RSS starts at whatever the latency pass left behind (273 MB in
    testing, so every per-view delta is noise against a huge base), and
    ``_stage_db`` would re-stage from the previous pass's temp copy because it
    reads the ``TCGINVENTORY_DB`` the previous pass set.
    """
    import subprocess

    for axis in ("latency", "memory", "storage"):
        print("=" * 72)
        print(axis.upper())
        print("=" * 72)
        sys.stdout.flush()
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), axis], check=False
        )
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    latency = sub.add_parser("latency", help="wall clock for views and services")
    latency.add_argument("--views", action="store_true")
    latency.add_argument("--services", action="store_true")
    latency.set_defaults(func=cmd_latency)

    sub.add_parser("memory", help="resident memory, staged").set_defaults(
        func=cmd_memory
    )
    sub.add_parser("storage", help="bytes on disk").set_defaults(func=cmd_storage)
    sub.add_parser("plans", help="query plans over the big tables").set_defaults(
        func=cmd_plans
    )
    sub.add_parser(
        "capabilities", help="what is available without a new dependency"
    ).set_defaults(func=cmd_capabilities)
    sub.add_parser("all", help="latency + memory + storage").set_defaults(func=cmd_all)

    profile = sub.add_parser("profile", help="cProfile one view")
    profile.add_argument("view")
    profile.add_argument("--rows", type=int, default=25)
    profile.set_defaults(func=cmd_profile)

    args = parser.parse_args()
    args.func(args)

    # Qt teardown without an event loop takes the process down with an access
    # violation (the same total behind tests/conftest.py's deferred-deletion
    # fixture). The measurement is already printed, so leave before that happens
    # rather than pretend a nonzero exit means the run failed.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
