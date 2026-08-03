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


def test_inventory_trend_column_renders_arrows(app, seeded_conn):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor

    from tcginventory.services.trends import Trend
    from tcginventory.ui.models.inventory_model import TREND_COL, InventoryTableModel

    model = InventoryTableModel()
    from tcginventory.services.inventory import InventoryService

    model.set_rows(InventoryService(seeded_conn).inventory_rows())
    item_id = model.row_at(0).item.id
    # A strong up-trend → green up-arrows scaling with magnitude.
    model.set_trends({
        item_id: Trend("up", 12.0, 40.0, 3, 30, 30),
    })
    from tcginventory.ui import theme

    idx = model.index(0, TREND_COL)
    assert model.data(idx, Qt.DisplayRole) == "▲▲▲"
    assert model.data(idx, Qt.ForegroundRole).color() == QColor(theme.active().pos)
    assert model.data(idx, Qt.UserRole) == 40.0  # sorts by signed %
    # An item with no computed trend shows the neutral placeholder.
    other = model.index(1, TREND_COL)
    assert model.data(other, Qt.DisplayRole) == "·"


def test_inventory_mark_missing_from_view(app, seeded_conn):
    from tcginventory.ui.views.inventory import InventoryView

    view = InventoryView(seeded_conn)
    row = view._model.row_at(0)
    assert not row.missing

    view._toggle_missing(row)  # the right-click action handler
    updated = next(
        view._model.row_at(i)
        for i in range(view._model.rowCount())
        if view._model.row_at(i).lot_id == row.lot_id
    )
    assert updated.sold_status == "Missing"
    assert updated.missing is True

    view._toggle_missing(updated)  # reversible
    restored = next(
        view._model.row_at(i)
        for i in range(view._model.rowCount())
        if view._model.row_at(i).lot_id == row.lot_id
    )
    assert restored.missing is False
    view.deleteLater()


def test_inventory_ebay_link_auto_populated(app, seeded_conn):
    from tcginventory.services.inventory import InventoryService
    from tcginventory.ui.models.inventory_model import (
        EBAY_COL,
        LINK_URL_ROLE,
        InventoryTableModel,
    )

    model = InventoryTableModel()
    model.set_rows(InventoryService(seeded_conn).inventory_rows())
    # Every row's eBay link resolves (derived sold-search when none is stored).
    url = model.data(model.index(0, EBAY_COL), LINK_URL_ROLE)
    assert url and "ebay.com/sch" in url and "LH_Sold=1" in url


def test_history_view_populates_and_details(app, seeded_conn):
    from tcginventory.ui.views.history import HistoryView

    view = HistoryView(seeded_conn)
    view.resize(900, 600)
    view.show()  # force a render so the pill delegate paints
    app.processEvents()
    assert view._model.rowCount() >= 6
    # Selecting a row populates the bottom item table (SKU, name, price, …).
    view._table.selectRow(0)
    app.processEvents()
    assert view._detail_table.rowCount() > 0
    assert view._detail_table.columnCount() == 8
    assert "Select a transaction" not in view._detail_header.text()  # filled in
    view.close()
    view.deleteLater()


def test_history_search_matches_card_or_counterparty(app):
    from conftest import incoming
    from tcginventory.services.transactions import (
        TransactionRequest,
        TransactionService,
    )
    from tcginventory.ui.views.history import HistoryView

    conn = connect(":memory:")
    migrate(conn)
    svc = TransactionService(conn)
    svc.post(
        TransactionRequest(
            occurred_at="2026-01-01",
            cash_out_cents=1000,
            incoming=[incoming("Charizard ex", 1000)],
            counterparty="eBay",
        )
    )
    svc.post(
        TransactionRequest(
            occurred_at="2026-01-02",
            cash_out_cents=500,
            incoming=[incoming("Blastoise", 500)],
            counterparty="Card Cavern",
        )
    )

    view = HistoryView(conn)
    assert view._proxy.rowCount() == 2
    view._search.setText("chari")  # matches a card name
    assert view._proxy.rowCount() == 1
    view._search.setText("cavern")  # matches a counterparty
    assert view._proxy.rowCount() == 1
    view._search.setText("zzzz")  # matches nothing → the "no match" empty state
    assert view._proxy.rowCount() == 0
    assert view._stack.currentWidget() is view._no_match
    view._search.clear()
    assert view._proxy.rowCount() == 2
    assert view._stack.currentWidget() is view._table
    view.deleteLater()
    conn.close()


def test_dashboard_builds_charts(app, seeded_conn):
    from tcginventory.ui.views.dashboard import DashboardView

    view = DashboardView(seeded_conn)
    chart = view._chart_view.chart()
    assert chart is not None
    from PySide6.QtCharts import QLineSeries

    lines = [s for s in chart.series() if isinstance(s, QLineSeries)]
    # money-movement + realized-profit + collection-value (the collection line
    # now shows for any holdings, valued at current/reviewed prices).
    assert len(lines) == 3
    view.deleteLater()


def test_item_detail_dialog_opens(app, seeded_conn):
    from tcginventory.data.repositories import ItemRepository
    from tcginventory.ui.views.item_detail import ItemDetailDialog

    item = ItemRepository(seeded_conn).list_all()[0]
    dialog = ItemDetailDialog(seeded_conn, item.id)
    assert dialog.windowTitle().endswith("detail")
    dialog.deleteLater()


def test_app_icon_loads(app):
    from tcginventory.ui.assets import app_icon

    assert not app_icon().isNull()


def test_main_window_hosts_views(app, seeded_conn):
    from tcginventory.ui.main_window import NAV_ITEMS, MainWindow

    window = MainWindow(seeded_conn)
    assert window.windowTitle().startswith("TCGinventory")
    assert not window.windowIcon().isNull()
    # Visit each nav destination; refresh must not raise.
    for row in range(len(NAV_ITEMS)):
        window._on_nav_changed(row)
        app.processEvents()
    window.close()
    window.deleteLater()


def test_history_recognition_tab_summarizes_the_corpus(app, seeded_conn):
    """The Recognition History toggle shows one row per capture with corpus
    counts, KPI totals, and the growth sparkline."""
    from tcginventory.data.vision import CardLabel, VisionRepository, open_vision
    from tcginventory.ui.views.history import HistoryView

    vrepo = VisionRepository(open_vision(":memory:"))
    cap = vrepo.add_capture(source="transaction", now="2026-01-01T09:00:00+00:00")
    img = vrepo.add_image(
        cap, "s", "/a.png", width=100, height=60, now="2026-01-01T09:00:00+00:00"
    )
    vrepo.add_region(img, 1, (0, 0, 0.5, 1), None, CardLabel("cel-4", name="Zard"))
    vrepo.add_region(img, 2, (0.5, 0, 0.5, 1), None, CardLabel(name="mystery"))

    view = HistoryView(seeded_conn, vision_repo=vrepo)
    assert view._pages.currentIndex() == 0  # defaults to Transaction History

    view._set_mode(1)  # → Recognition History (refreshes from the corpus)
    assert view._pages.currentIndex() == 1
    assert view._rec._model.rowCount() == 1
    row = view._rec._model.row_at(0)
    assert (row.images, row.regions, row.labeled) == (1, 2, 1)
    assert row.state == "partial"  # one of two boxes labeled
    assert view._rec._stat_images.text() == "1"
    assert view._rec._stat_labeled.text() == "1"
    view.deleteLater()


def test_nav_refresh_is_gated_until_data_changes(app, seeded_conn):
    """Switching to an unchanged screen must not re-run its refresh; a data
    mutation invalidates the views so the next visit refreshes once."""
    from tcginventory.ui.main_window import MainWindow

    window = MainWindow(seeded_conn)
    inv_index = 2  # Inventory
    view = window._stack.widget(inv_index)
    calls = {"n": 0}
    original = view.refresh

    def counting() -> None:
        calls["n"] += 1
        original()

    view.refresh = counting

    window._on_nav_changed(inv_index)  # first visit → refreshes
    assert calls["n"] == 1
    window._on_nav_changed(0)  # away…
    window._on_nav_changed(inv_index)  # …and back, nothing changed → cached
    assert calls["n"] == 1

    window._on_nav_changed(0)  # sit on another view
    window._on_ledger_changed()  # a mutation invalidates all views
    window._on_nav_changed(inv_index)  # revisit → refreshes exactly once more
    assert calls["n"] == 2

    window.close()
    window.deleteLater()
