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
    assert rows["A"].purchase_price_cents == 4800  # 80 × 60/100
    assert rows["B"].purchase_price_cents == 3200  # 80 × 40/100
    # Form cleared after a successful post.
    assert view._badge.text() == "—"


def test_ui_sale_realizes_margin(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    view.add_incoming(_inc("A", 9000))
    view._cash_out.setValue(90.00)
    view.submit()

    item_id = _first_item_id(conn, "A")
    view.add_outgoing(OutgoingCard(item_id, 1), "A")
    view._cash_in.setValue(120.00)
    assert view._badge.text() == "SALE"
    view.submit()

    assert ReportingService(conn).realized_pl_cents() == 3000
    assert InventoryService(conn).on_hand(item_id) == 0


def test_ui_give_row_caps_quantity_to_on_hand(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    view.add_incoming(_inc("A", 1000))
    view._cash_out.setValue(10.00)
    view.submit()

    item_id = _first_item_id(conn, "A")
    view.add_outgoing(OutgoingCard(item_id, 5), "A")  # only 1 on hand
    # The inline row caps quantity at what's available, so oversell can't be
    # entered; a valid one-card sale is submittable.
    assert view._give_rows[0].qty.value() == 1
    view._cash_in.setValue(5.00)
    assert view._submit.isEnabled() is True


def test_ui_trade_and_rip_lifecycle(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)

    # Purchase a card to trade away later.
    view.add_incoming(_inc("Snorlax", 1000))
    view._cash_out.setValue(10.00)
    view.submit()
    snorlax = _first_item_id(conn, "Snorlax")

    # Trade Snorlax (market $10) for Gengar (market $12) + $3 cash back.
    view.add_outgoing(OutgoingCard(snorlax, 1), "Snorlax")
    view.add_incoming(_inc("Gengar", 1200))
    view._cash_in.setValue(3.00)
    assert view._badge.text() == "TRADE"
    view.submit()

    # Snorlax proceeds = cash_in 300 + Gengar market 1200 = 1500; − 1000 cost.
    assert ReportingService(conn).realized_pl_cents() == 500
    # Gengar basis = Snorlax market 1000 − cash back 300 = 700.
    gengar = _first_item_id(conn, "Gengar")
    (gengar_lot,) = LotRepository(conn).list_for_item(gengar)
    assert gengar_lot.unit_cost_cents == 700

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


def test_incoming_dialog_returns_enums_and_defaults_resale(app):
    from tcginventory.domain.enums import Category, Condition, Language
    from tcginventory.ui.views._card_dialogs import IncomingCardDialog

    dlg = IncomingCardDialog()
    dlg._name.setText("Test Card")
    dlg._market.setValue(50.0)
    card = dlg.result_card()

    assert card.intent is Intent.RESALE  # default is Resale
    # Real enums, not the plain strings QComboBox.currentData() hands back.
    assert isinstance(card.item.category, Category)
    assert isinstance(card.item.condition, Condition)
    assert isinstance(card.item.language, Language)
    assert card.market_value_cents == 5000
    dlg.deleteLater()


def test_incoming_card_renders_and_posts_via_dialog(app, conn):
    from tcginventory.data.repositories import ItemRepository
    from tcginventory.ui.views._card_dialogs import IncomingCardDialog
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    dlg = IncomingCardDialog()
    dlg._name.setText("Blastoise")
    dlg._set_name.setText("Base")
    dlg._market.setValue(50.0)
    view.add_incoming(dlg.result_card())

    # The inline receive row carries the card's fields as editable widgets.
    row = view._receive_rows[0]
    assert row.name.text() == "Blastoise"
    assert row.condition.currentText() == "NM"
    assert row.market.value() == 50.0
    assert row.intent.currentText() == "Resale"

    view._cash_out.setValue(50.0)
    view.submit()
    assert any(i.name == "Blastoise" for i in ItemRepository(conn).list_all())
    dlg.deleteLater()


def test_ui_give_dialog_sku_lookup(app, conn):
    from conftest import buy
    from tcginventory.services.inventory import InventoryService
    from tcginventory.services.transactions import TransactionService
    from tcginventory.ui.views._card_dialogs import OutgoingCardDialog

    svc = TransactionService(conn)
    buy(svc, "Snorlax", 1000, 1000, "2026-01-01", set_code="BS", card_number="011/102")
    lot_sku = InventoryService(conn).inventory_rows()[0].sku

    sellable = InventoryService(conn).sellable_items()
    dialog = OutgoingCardDialog(sellable, conn)
    dialog._sku.setText(lot_sku)
    dialog._lookup_sku()
    # The combo now points at the looked-up card.
    card, name = dialog.result_card()
    assert name == "Snorlax"
    dialog.deleteLater()


def test_removing_a_middle_receive_row_does_not_crash(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    r0 = view._add_receive_row()
    view._add_receive_row()
    r2 = view._add_receive_row()
    r0.name.setText("first")
    r2.name.setText("third")

    # Remove the first (not the last) row — used to delete surviving widgets.
    view._remove_row("in", r0)
    assert len(view._receive_rows) == 2
    assert view._in_table.rowCount() == 2
    # Surviving rows' widgets are still live and correct.
    assert view._receive_rows[-1].name.text() == "third"
    # Deleting again still works.
    view._remove_row("in", r2)
    assert len(view._receive_rows) == 1
    view.deleteLater()


def test_receive_row_autofills_set_and_number_from_catalog(app, conn):
    from tcginventory.data.repositories import CardCatalogRepository
    from tcginventory.domain.models import CatalogCard
    from tcginventory.ui.views.transaction import TransactionView

    CardCatalogRepository(conn).replace_all(
        [
            CatalogCard(
                card_id="sv9-163", name="Iono's Kilowattrel", set_id="sv9",
                set_name="Journey Together", set_code="JTG", number="163",
            )
        ],
        "2026-07-13T00:00:00Z",
    )
    conn.commit()

    view = TransactionView(conn)
    row = view._add_receive_row()
    row.name.setText("Iono's Kilowattrel")
    row._autofill()
    assert row.set.text() == "Journey Together"
    assert row.number.text() == "163"


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
