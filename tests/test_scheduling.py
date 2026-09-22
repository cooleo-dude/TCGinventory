"""Headless price-refresh entry point + background-scheduling helper."""

from __future__ import annotations

import sys

from tcginventory import scheduling
from tcginventory.data.db import open_database
from tcginventory.data.repositories import ItemRepository
from tcginventory.domain.enums import Category
from tcginventory.domain.models import Item
from tcginventory.services.pricing import RefreshSummary


def test_refresh_command_includes_flag_from_source():
    cmd = scheduling.refresh_command()
    assert "--refresh-prices" in cmd
    assert "tcginventory" in cmd  # `-m tcginventory` when running from source


def test_is_supported_matches_platform():
    import os

    assert scheduling.is_supported() == (os.name == "nt")


def test_main_dispatches_to_headless_refresh(monkeypatch):
    import tcginventory.app as app

    monkeypatch.setattr(sys, "argv", ["tcginventory", "--refresh-prices"])
    monkeypatch.setattr(app, "refresh_prices_headless", lambda: 7)
    assert app.main() == 7  # returns without importing Qt / launching a window


def test_headless_refresh_missing_db_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("TCGINVENTORY_DB", str(tmp_path / "nope.db"))
    from tcginventory.app import refresh_prices_headless

    assert refresh_prices_headless() == 0


def test_headless_refresh_runs_pricing(monkeypatch, tmp_path):
    db = tmp_path / "inv.db"
    conn = open_database(db)
    ItemRepository(conn).add(Item(name="X", category=Category.SINGLE))
    conn.commit()
    conn.close()

    monkeypatch.setenv("TCGINVENTORY_DB", str(db))

    calls = {}

    class _FakeService:
        def __init__(self, conn):
            calls["built"] = True

        def refresh(self):
            calls["refreshed"] = True
            return RefreshSummary(priced=1)

    monkeypatch.setattr("tcginventory.services.pricing.PricingService", _FakeService)
    from tcginventory.app import refresh_prices_headless

    assert refresh_prices_headless() == 0
    assert calls == {"built": True, "refreshed": True}


# --------------------------------------------------------------------------
# The nightly publish rides the price refresh (tickets 73/74)
# --------------------------------------------------------------------------


def _headless_db(tmp_path, monkeypatch):
    db = tmp_path / "inv.db"
    conn = open_database(db)
    ItemRepository(conn).add(Item(name="X", category=Category.SINGLE))
    conn.commit()
    conn.close()
    monkeypatch.setenv("TCGINVENTORY_DB", str(db))
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))

    class _NoPricing:
        def __init__(self, conn):
            pass

        def refresh(self):
            return RefreshSummary(priced=0)

    monkeypatch.setattr("tcginventory.services.pricing.PricingService", _NoPricing)
    return db


def _runs(db, op):
    import sqlite3

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c.execute(
        "SELECT ok, source, summary FROM operation_run WHERE op = ?", (op,)
    ).fetchall()


def test_headless_skips_the_snapshot_when_no_store_is_connected(monkeypatch, tmp_path):
    """Not configuring it is a choice, not a nightly failure to report."""
    from tcginventory.services import snapsecrets

    db = _headless_db(tmp_path, monkeypatch)
    snapsecrets.set_backend(snapsecrets.MemoryBackend())
    try:
        from tcginventory.app import refresh_prices_headless

        assert refresh_prices_headless() == 0
        assert _runs(db, "snapshot.publish") == []
    finally:
        snapsecrets.set_backend(None)


def test_headless_publishes_and_records_a_scheduled_run(monkeypatch, tmp_path):
    from tcginventory.services import snappublish, snapsecrets

    db = _headless_db(tmp_path, monkeypatch)
    snapsecrets.set_backend(snapsecrets.MemoryBackend())
    try:
        snapsecrets.save_credentials(
            account_id="a", bucket="b", access_key_id="k", secret_access_key="s"
        )
        monkeypatch.setattr(
            snappublish,
            "publish",
            lambda conn: snappublish.PublishResult(
                published=True,
                rows=1,
                in_stock=1,
                payload_bytes=10,
                art_bytes=0,
                pruned=0,
                summary="1 row | 0 KB",
            ),
        )
        from tcginventory.app import refresh_prices_headless

        assert refresh_prices_headless() == 0
        rows = _runs(db, "snapshot.publish")
        assert len(rows) == 1
        assert rows[0]["ok"] == 1 and rows[0]["source"] == "scheduled"
        assert rows[0]["summary"] == "1 row | 0 KB"
    finally:
        snapsecrets.set_backend(None)


def test_headless_records_a_failed_publish_and_exits_non_zero(monkeypatch, tmp_path):
    from tcginventory.services import snappublish, snapsecrets

    db = _headless_db(tmp_path, monkeypatch)
    snapsecrets.set_backend(snapsecrets.MemoryBackend())
    try:
        snapsecrets.save_credentials(
            account_id="a", bucket="b", access_key_id="k", secret_access_key="s"
        )

        def boom(conn):
            raise RuntimeError("HTTP 403: AccessDenied")

        monkeypatch.setattr(snappublish, "publish", boom)
        from tcginventory.app import refresh_prices_headless

        assert refresh_prices_headless() == 1
        rows = _runs(db, "snapshot.publish")
        assert len(rows) == 1
        assert rows[0]["ok"] == 0 and "AccessDenied" in rows[0]["summary"]
    finally:
        snapsecrets.set_backend(None)
