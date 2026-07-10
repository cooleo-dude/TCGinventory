"""Offscreen smoke tests for the M3 read views over a seeded database."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tcginventory.data.db import connect  # noqa: E402
from tcginventory.data.migrator import migrate  # noqa: E402
from tcginventory.data.seed import seed_demo  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def seeded_conn():
    conn = connect(":memory:")
    migrate(conn)
    seed_demo(conn)
    yield conn
    conn.close()


def test_inventory_view_populates(app, seeded_conn):
    from tcginventory.ui.views.inventory import InventoryView

    view = InventoryView(seeded_conn)
    # Every seeded item that has a lot shows as a row.
    assert view._model.rowCount() >= 6
    view.deleteLater()


def test_history_view_populates_and_details(app, seeded_conn):
    from tcginventory.ui.views.history import HistoryView

    view = HistoryView(seeded_conn)
    assert view._model.rowCount() >= 6
    # Selecting the first row renders detail HTML without error.
    view._table.selectRow(0)
    app.processEvents()
    assert view._detail.toPlainText() != ""
    view.deleteLater()


def test_dashboard_builds_charts(app, seeded_conn):
    from tcginventory.ui.views.dashboard import DashboardView

    view = DashboardView(seeded_conn)
    chart = view._chart_view.chart()
    assert chart is not None
    assert len(chart.series()) == 2  # money-movement + realized-profit
    view.deleteLater()


def test_item_detail_dialog_opens(app, seeded_conn):
    from tcginventory.data.repositories import ItemRepository
    from tcginventory.ui.views.item_detail import ItemDetailDialog

    item = ItemRepository(seeded_conn).list_all()[0]
    dialog = ItemDetailDialog(seeded_conn, item.id)
    assert dialog.windowTitle().endswith("detail")
    dialog.deleteLater()


def test_main_window_hosts_views(app, seeded_conn):
    from tcginventory.ui.main_window import NAV_ITEMS, MainWindow

    window = MainWindow(seeded_conn)
    assert window.windowTitle().startswith("TCGinventory")
    # Visit each nav destination; refresh must not raise.
    for row in range(len(NAV_ITEMS)):
        window._on_nav_changed(row)
        app.processEvents()
    window.close()
    window.deleteLater()
