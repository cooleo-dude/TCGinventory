"""Dashboard highlights are links: movers/sellers → Inventory, shows → Calendar."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import buy, sell  # noqa: E402
from tcginventory.domain.enums import Intent  # noqa: E402
from tcginventory.domain.models import CardShow  # noqa: E402
from tcginventory.services.reporting import MoverRow, SellerRow  # noqa: E402
from tcginventory.services.transactions import TransactionService  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _mover(item_id: int) -> MoverRow:
    return MoverRow(
        item_id=item_id,
        name=f"Card {item_id}",
        set_name="Base",
        card_number="4",
        qty=1,
        price_cents=5000,
        change_cents=800,
        change_pct=19.0,
    )


def _click(row_widget) -> None:
    """Left-click the row the way a user would (release inside its rect)."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    center = QPointF(row_widget.rect().center())
    row_widget.mouseReleaseEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            center,
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
    )


def test_mover_and_seller_rows_emit_their_item_id(app, conn):
    from tcginventory.ui.views.dashboard import MoversColumn, _ImageLoader

    col = MoversColumn("Top Gainers", "#ffffff", _ImageLoader(conn))
    col.set_movers([_mover(7), _mover(9)], gain=True)
    seen: list[int] = []
    col.activated.connect(seen.append)

    _click(col._rows.itemAt(1).widget())
    assert seen == [9]  # the row that was clicked, not the first one

    col.set_sellers(
        [
            SellerRow(
                item_id=4,
                name="Snorlax",
                set_name="Base",
                card_number="11",
                units=3,
                sales=2,
                revenue_cents=9000,
                realized_cents=1200,
            )
        ],
        "last 90 days",
    )
    _click(col._rows.itemAt(0).widget())
    assert seen == [9, 4]
    col.deleteLater()


def test_upcoming_show_row_emits_its_start_date(app):
    from tcginventory.ui.views.dashboard import UpcomingShowsBox

    box = UpcomingShowsBox()
    box.set_shows(
        [
            CardShow(
                source_id=1,
                name="Fall Expo",
                state="NJ",
                starts_on="2026-09-12",
                city="Edison",
                status="attending",
            )
        ]
    )
    seen: list[str] = []
    box.activated.connect(seen.append)

    _click(box._rows.itemAt(0).widget())

    assert seen == ["2026-09-12"]
    box.deleteLater()


def test_inventory_select_item_selects_and_fills_the_dossier(app, conn):
    from tcginventory.ui.views.inventory import InventoryView

    svc = TransactionService(conn)
    buy(svc, "Charizard", 10000, 12000, "2026-01-01")
    wanted = buy(svc, "Blastoise", 4000, 5000, "2026-01-02")
    item_id = wanted.lots[0].item_id

    view = InventoryView(conn)
    assert view.select_item(item_id) is True

    selected = view._selected_row()
    assert selected is not None and selected.item.id == item_id
    # The dossier tracks the selection (its title is filled from the row).
    assert view._dossier_name.text() == "Blastoise"
    # An id with no row here is reported, not silently ignored.
    assert view.select_item(9999) is False
    view.deleteLater()


def test_inventory_select_item_clears_filters_that_hide_the_row(app, conn):
    """A fully-sold Top Seller must still be reachable with 'In stock' on."""
    from tcginventory.ui.views.inventory import InventoryView

    svc = TransactionService(conn)
    result = buy(svc, "Snorlax", 1000, 1200, "2026-01-01")
    item_id = result.lots[0].item_id
    sell(svc, item_id, 1, 2000, "2026-02-01")  # nothing left on hand

    view = InventoryView(conn)
    view._in_stock.setChecked(True)  # the row is now filtered out
    view._search.setText("nothing matches this")
    assert view._proxy.rowCount() == 0

    assert view.select_item(item_id) is True

    assert view._in_stock.isChecked() is False
    assert view._search.text() == ""
    assert view._selected_row().item.id == item_id
    view.deleteLater()


def test_inventory_select_item_prefers_a_lot_still_on_hand(app, conn):
    from tcginventory.ui.views.inventory import InventoryView

    svc = TransactionService(conn)
    first = buy(svc, "Pikachu", 1000, 1200, "2026-01-01", intent=Intent.RESALE)
    item_id = first.lots[0].item_id
    # A second lot of the same card, bought later and still unsold.
    buy(svc, "Pikachu", 1100, 1300, "2026-03-01", intent=Intent.RESALE)
    sell(svc, item_id, 1, 2000, "2026-02-01")  # drains the FIRST lot (FIFO)

    view = InventoryView(conn)
    assert view.select_item(item_id) is True

    row = view._selected_row()
    assert row.in_stock is True  # landed on the copies still held
    view.deleteLater()


def test_calendar_show_date_opens_that_month_with_the_day_selected(app, conn):
    from tcginventory.ui.views.calendar import CalendarView

    view = CalendarView(conn)
    view.show_date("2026-09-12")

    assert (view._year, view._month) == (2026, 9)
    assert view._selected == "2026-09-12"
    view.show_date("not a date")  # ignored, state unchanged
    assert view._selected == "2026-09-12"
    view.deleteLater()


def test_main_window_routes_highlight_clicks_to_the_right_screen(app, conn):
    """End to end: the click lands on the destination screen with the target
    selected — Inventory for a card, Calendar for a show."""
    from tcginventory.ui.main_window import (
        _CALENDAR_INDEX,
        _INVENTORY_INDEX,
        MainWindow,
    )

    result = buy(TransactionService(conn), "Machamp", 3000, 3500, "2026-01-01")
    item_id = result.lots[0].item_id

    win = MainWindow(conn)
    win._dashboard.item_activated.emit(item_id)

    assert win._nav.currentRow() == _INVENTORY_INDEX
    assert win._stack.currentWidget() is win._inventory
    assert win._inventory._selected_row().item.id == item_id

    win._dashboard.show_date_activated.emit("2026-11-07")

    assert win._nav.currentRow() == _CALENDAR_INDEX
    assert win._calendar._selected == "2026-11-07"
    win.deleteLater()
