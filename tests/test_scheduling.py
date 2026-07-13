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

    monkeypatch.setattr(
        "tcginventory.services.pricing.PricingService", _FakeService
    )
    from tcginventory.app import refresh_prices_headless

    assert refresh_prices_headless() == 0
    assert calls == {"built": True, "refreshed": True}
