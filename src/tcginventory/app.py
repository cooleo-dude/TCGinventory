"""Application entry point: wire up config, logging, and launch the GUI."""

from __future__ import annotations

import logging
import os
import sys

from tcginventory import config


def refresh_prices_headless() -> int:
    """Refresh market prices *and* card shows without launching the GUI.

    Entry point for scheduled/background updates (``--refresh-prices``): opens
    the database, runs migrations, records a price snapshot per mapped item, and
    re-scrapes card shows for the states the user has toggled on (read from the
    JSON prefs, so no Qt is needed). Runs fine from a Windows Scheduled Task with
    no display. Returns a non-zero exit code if anything errored.
    """
    config.configure_logging()
    log = logging.getLogger(__name__)

    # Each phase below also appends to the maintenance run log (``record``), so
    # the Operations window can report what happened while the app was closed.
    from tcginventory.data.db import open_database
    from tcginventory.services.cardshows import CardShowService
    from tcginventory.services.operations import record
    from tcginventory.services.pricing import PricingService

    config.ensure_data_dir()
    db = config.db_path()
    if not db.exists():
        log.warning("No database at %s; nothing to refresh.", db)
        return 0

    had_error = False
    conn = open_database(db)
    try:
        try:
            summary = PricingService(conn).refresh()
            log.info(
                "headless price refresh: priced=%d unmapped=%d errors=%s",
                summary.priced,
                len(summary.unmapped),
                summary.errors,
            )
            had_error = had_error or bool(summary.errors)
            note = f"{summary.priced} priced"
            if summary.unmapped:
                note += f", {len(summary.unmapped)} skipped (no TCGplayer link)"
            record(
                conn,
                "prices.refresh",
                ok=not summary.errors,
                summary=note,
                source="scheduled",
            )
            # Record today's effective collection value (drives the dashboard
            # chart) now that prices are fresh.
            from tcginventory.services.reporting import ReportingService

            ReportingService(conn).record_collection_value()
        except Exception as exc:
            log.exception("headless price refresh failed")
            record(
                conn,
                "prices.refresh",
                ok=False,
                summary=str(exc)[:200],
                source="scheduled",
            )
            had_error = True

        # Catalog-wide market sweep → separate price-history DB (+ trimmed backup).
        try:
            from tcginventory.data.backup import (
                backup_price_history,
                timestamp_for_filename,
            )
            from tcginventory.data.pricehistory import open_history
            from tcginventory.services.marketprices import sweep

            hist_conn = open_history()
            try:
                s = sweep(conn, hist_conn)
                log.info(
                    "headless market sweep: sets=%d priced=%d snapshots=%d errors=%s",
                    s.sets,
                    s.priced,
                    s.snapshots,
                    s.errors,
                )
                had_error = had_error or bool(s.errors)
                record(
                    conn,
                    "prices.market_sweep",
                    ok=not s.errors,
                    summary=(
                        f"{s.sets} sets, {s.priced:,} priced, "
                        f"{s.snapshots:,} changes recorded"
                    ),
                    source="scheduled",
                )
                backup_price_history(
                    hist_conn,
                    config.data_dir() / "backups",
                    timestamp_for_filename(),
                )
            finally:
                hist_conn.close()
        except Exception as exc:
            log.exception("headless market sweep failed")
            record(
                conn,
                "prices.market_sweep",
                ok=False,
                summary=str(exc)[:200],
                source="scheduled",
            )
            had_error = True

        states = list(config.get_pref("show_states", []))
        if states:
            try:
                shows = CardShowService(conn).refresh(states)
                log.info(
                    "headless show refresh: fetched=%d states=%s errors=%s",
                    shows.fetched,
                    states,
                    shows.errors,
                )
                had_error = had_error or bool(shows.errors)
                record(
                    conn,
                    "shows.refresh",
                    ok=not shows.errors,
                    summary=f"{shows.fetched} shows across {', '.join(states)}",
                    source="scheduled",
                )
            except Exception as exc:
                log.exception("headless show refresh failed")
                record(
                    conn,
                    "shows.refresh",
                    ok=False,
                    summary=str(exc)[:200],
                    source="scheduled",
                )
                had_error = True
    finally:
        conn.close()
    return 1 if had_error else 0


def main() -> int:
    """Launch the TCGinventory desktop app. Returns the Qt exit code."""
    # Headless subcommand for scheduled background updates — handled before Qt
    # is imported so it needs no display.
    if "--refresh-prices" in sys.argv[1:]:
        return refresh_prices_headless()

    config.configure_logging()
    log = logging.getLogger(__name__)

    data_dir = config.ensure_data_dir()
    log.info("Data directory: %s", data_dir)
    log.info("Database path: %s", config.db_path())

    # Qt comes up first now, so the startup screen can cover the work below —
    # building the workspace is about two seconds on a real collection, and it
    # used to be two seconds of nothing. Imported lazily all the same, so headless
    # tooling (tests, the scheduled refresh) never needs a display.
    from PySide6.QtWidgets import QApplication

    from tcginventory.ui.assets import app_icon
    from tcginventory.ui.main_window import NAV_ITEMS, MainWindow
    from tcginventory.ui.splash import StartupSplash
    from tcginventory.ui.theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    apply_theme(app)  # Ember Duo: Fusion + IBM Plex + palette/QSS
    app.setWindowIcon(app_icon())

    # One step per view, plus the startup backup ("opening the ledger" is the
    # state the screen comes up in, not a step of its own).
    splash = StartupSplash(total_steps=len(NAV_ITEMS) + 1)
    splash.begin("Opening the ledger…")

    # Open the DB and run any pending migrations on startup (F-DA-7). The
    # connection stays open for the app's lifetime; the views read from it.
    from tcginventory.data.db import open_database
    from tcginventory.data.migrator import current_version

    conn = open_database(config.db_path())
    log.info("Schema version: %d", current_version(conn))

    # Assign SKUs to any pre-existing lots that predate the SKU system.
    from tcginventory.services.sku import backfill_skus

    filled = backfill_skus(conn)
    if filled:
        log.info("Backfilled %d lot SKU(s).", filled)

    # Optional demo seeding for a fresh DB (dev/verification only).
    if os.environ.get("TCGINVENTORY_SEED") == "1":
        empty = conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 0
        if empty:
            from tcginventory.data.seed import seed_demo

            seed_demo(conn)
            log.info("Seeded demo data.")

    # Automatic timestamped backup on startup (F-DA-6); never blocks launch.
    if conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] > 0:
        splash.step("Writing a startup backup…")
        try:
            from tcginventory.data.backup import auto_backup, timestamp_for_filename

            dest = auto_backup(
                conn, data_dir / "backups", stamp=timestamp_for_filename()
            )
            log.info("Startup backup: %s", dest)
            from tcginventory.services.operations import record

            record(
                conn,
                "backup.database",
                summary=f"startup backup — {dest.name}",
                source="startup",
            )
        except Exception:
            log.exception("Startup backup failed (continuing)")

        # Record today's collection-value point so the dashboard chart has a
        # current anchor even before any price refresh runs (deduped per day).
        try:
            from tcginventory.services.reporting import ReportingService

            ReportingService(conn).record_collection_value()
        except Exception:
            log.exception("Collection-value recording failed (continuing)")

    # Each view names itself on the splash as it's built, which is also what
    # keeps the screen painting: they're all built on this thread.
    try:
        window = MainWindow(conn, progress=lambda name: splash.step(f"Loading {name}…"))
    except Exception:
        splash.close()  # never leave a splash hanging over a failure
        raise
    splash.finish(window)
    try:
        return app.exec()
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
