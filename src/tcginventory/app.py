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

    from tcginventory.data.db import open_database
    from tcginventory.services.cardshows import CardShowService
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
            # Record today's effective collection value (drives the dashboard
            # chart) now that prices are fresh.
            from tcginventory.services.reporting import ReportingService

            ReportingService(conn).record_collection_value()
        except Exception:
            log.exception("headless price refresh failed")
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
                backup_price_history(
                    hist_conn,
                    config.data_dir() / "backups",
                    timestamp_for_filename(),
                )
            finally:
                hist_conn.close()
        except Exception:
            log.exception("headless market sweep failed")
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
            except Exception:
                log.exception("headless show refresh failed")
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
        try:
            from tcginventory.data.backup import auto_backup, timestamp_for_filename

            dest = auto_backup(
                conn, data_dir / "backups", stamp=timestamp_for_filename()
            )
            log.info("Startup backup: %s", dest)
        except Exception:
            log.exception("Startup backup failed (continuing)")

        # Record today's collection-value point so the dashboard chart has a
        # current anchor even before any price refresh runs (deduped per day).
        try:
            from tcginventory.services.reporting import ReportingService

            ReportingService(conn).record_collection_value()
        except Exception:
            log.exception("Collection-value recording failed (continuing)")

    # Imported lazily so headless tooling (tests, tools) can import this module
    # without requiring a Qt display or the PySide6 dependency at import time.
    from PySide6.QtWidgets import QApplication

    from tcginventory.ui.assets import app_icon
    from tcginventory.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setWindowIcon(app_icon())
    window = MainWindow(conn)
    window.show()
    try:
        return app.exec()
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
