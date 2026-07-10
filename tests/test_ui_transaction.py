"""Drive the write UI end-to-end: purchase → sale → trade → rip, edit, void.

Exercises the Transaction view and History actions (the M4 exit criterion:
a full lifecycle done through the UI, reconciling with the ledger).
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tcginventory.data.db import connect  # noqa: E402
from tcginventory.data.migrator import migrate  # noqa: E402
from tcginventory.data.repositories import (  # noqa: E402
    ItemRepository,
    LotRepository,
    TransactionRepository,
)
from tcginventory.domain.enums import (  # noqa: E402
    Category,
    Intent,
    TransactionStatus,
)
from tcginventory.domain.models import Item  # noqa: E402
from tcginventory.services.inventory import InventoryService  # noqa: E402
from tcginventory.services.reporting import ReportingService  # noqa: E402
from tcginventory.services.transactions import IncomingCard, OutgoingCard  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def conn():
    c = connect(":memory:")
    migrate(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _silence_dialogs(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)


def _inc(name, mv, intent=Intent.RESALE, qty=1) -> IncomingCard:
    return IncomingCard(Item(name=name, category=Category.SINGLE), qty, mv, intent)


def _first_item_id(conn, name):
    for item in ItemRepository(conn).list_all():
        if item.name == name:
            return item.id
    raise AssertionError(f"item {name} not found")


def test_ui_purchase_infers_type_and_allocates(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    view.add_incoming(_inc("A", 6000))
    view.add_incoming(_inc("B", 4000, Intent.PERSONAL))
    view._cash_out.setValue(80.00)

    assert view._badge.text() == "PURCHASE"
    assert view._submit.isEnabled()

    view.submit()

    rows = {r.item.name: r for r in InventoryService(conn).inventory_rows()}
    assert rows["A"].cost_basis_cents == 4800  # 80 × 60/100
    assert rows["B"].cost_basis_cents == 3200  # 80 × 40/100
    # Form cleared after a successful post.
    assert view._badge.text() == "—"


def test_ui_sale_realizes_margin(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    view.add_incoming(_inc("A", 9000))
    view._cash_out.setValue(90.00)
    view.submit()

    item_id = _first_item_id(conn, "A")
    view.add_outgoing(OutgoingCard(item_id, 1, 12000), "A")
    view._cash_in.setValue(120.00)
    assert view._badge.text() == "SALE"
    view.submit()

    assert ReportingService(conn).realized_pl_cents() == 3000
    assert InventoryService(conn).on_hand(item_id) == 0


def test_ui_oversell_blocks_submit(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    view.add_incoming(_inc("A", 1000))
    view._cash_out.setValue(10.00)
    view.submit()

    item_id = _first_item_id(conn, "A")
    view.add_outgoing(OutgoingCard(item_id, 5, 500), "A")  # only 1 on hand
    assert view._submit.isEnabled() is False  # oversell → blocked


def test_ui_trade_and_rip_lifecycle(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)

    # Purchase a card to trade away later.
    view.add_incoming(_inc("Snorlax", 1000))
    view._cash_out.setValue(10.00)
    view.submit()
    snorlax = _first_item_id(conn, "Snorlax")

    # Trade Snorlax (agreed $15) for Gengar (market $12) + $3 cash back.
    view.add_outgoing(OutgoingCard(snorlax, 1, 1500), "Snorlax")
    view.add_incoming(_inc("Gengar", 1200))
    view._cash_in.setValue(3.00)
    assert view._badge.text() == "TRADE"
    view.submit()

    assert ReportingService(conn).realized_pl_cents() == 500  # 1500 − 1000
    # net consideration for Gengar = 1500 − 300 = 1200.
    gengar = _first_item_id(conn, "Gengar")
    (gengar_lot,) = LotRepository(conn).list_for_item(gengar)
    assert gengar_lot.unit_cost_cents == 1200

    # Rip: buy a sealed box tagged ripping → valued at $0 (a loss).
    view.add_incoming(_inc("Booster Box", 12000, Intent.RIPPING))
    view._cash_out.setValue(110.00)
    view.submit()
    box = _first_item_id(conn, "Booster Box")
    pos = InventoryService(conn).item_position(box)
    assert pos.market_value_cents == 0
    assert pos.unrealized_pl_cents == -11000

    # Ripping lot is not sellable.
    assert box not in {i.id for i, _ in InventoryService(conn).sellable_items()}


def test_ui_edit_reposts_and_history_void(app, conn):
    from tcginventory.ui.views.history import HistoryView
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    view.add_incoming(_inc("A", 8000))
    view._cash_out.setValue(80.00)
    view.submit()
    old_id = TransactionRepository(conn).list_all()[0].id

    # Edit: change the price to $90; original is voided, a new txn posted.
    assert view.load_transaction(old_id)
    view._cash_out.setValue(90.00)
    view.submit()

    assert TransactionRepository(conn).get(old_id).status is TransactionStatus.VOIDED
    item_id = _first_item_id(conn, "A")
    (lot,) = LotRepository(conn).open_lots_for_item(item_id)
    assert lot.unit_cost_cents == 9000

    # Void the re-posted purchase from History → lot removed.
    hist = HistoryView(conn)
    posted = [
        r
        for r in TransactionRepository(conn).list_all()
        if r.status is TransactionStatus.POSTED
    ]
    # select the posted row in the table and void it
    for proxy_row in range(hist._proxy.rowCount()):
        src = hist._proxy.mapToSource(hist._proxy.index(proxy_row, 0))
        if hist._model.row_at(src.row()).transaction.id == posted[0].id:
            hist._table.selectRow(proxy_row)
            break
    hist._void_selected()

    voided = TransactionRepository(conn).get(posted[0].id)
    assert voided.status is TransactionStatus.VOIDED
    assert LotRepository(conn).list_for_item(item_id) == []
