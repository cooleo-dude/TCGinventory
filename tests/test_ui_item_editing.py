"""Editing item attributes from the Inventory view: inline intent + full editor.

Covers the ability to change any field of a card after acquisition — the
per-lot Intent drop-down in the grid, and Condition/Language/grading via the
Item editor dialog.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import buy  # noqa: E402
from tcginventory.data.repositories import ItemRepository, LotRepository  # noqa: E402
from tcginventory.domain.enums import (  # noqa: E402
    Condition,
    GradingCompany,
    Intent,
    Language,
)
from tcginventory.services.inventory import InventoryService  # noqa: E402
from tcginventory.services.transactions import TransactionService  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_inventory_intent_edit_updates_lot(app, conn):
    from tcginventory.ui.models.inventory_model import INTENT_COL
    from tcginventory.ui.views.inventory import InventoryView

    result = buy(TransactionService(conn), "Snorlax", 1000, 1000, "2026-01-01")
    lot_id = result.lots[0].id

    view = InventoryView(conn)
    idx = view._model.index(0, INTENT_COL)
    # Simulate the combo-box delegate committing a new value.
    assert view._model.setData(idx, "personal")

    assert LotRepository(conn).get(lot_id).intent is Intent.PERSONAL
    # Ripping re-values the lot at $0 (a loss shown in the grid).
    view._model.setData(idx, "ripping")
    (row,) = InventoryService(conn).inventory_rows()
    assert row.intent == "ripping"
    assert row.current_price_cents == 0
    view.deleteLater()


def test_inventory_review_filter(app, conn):
    from tcginventory.ui.views.inventory import InventoryView

    r1 = buy(TransactionService(conn), "Pikachu", 100, 200, "2026-01-01")
    buy(TransactionService(conn), "Charizard", 100, 200, "2026-01-02")
    items = ItemRepository(conn)
    it = items.get(r1.lots[0].item_id)
    it.review = True
    items.update(it)
    conn.commit()

    view = InventoryView(conn)
    assert view._proxy.rowCount() == 2
    view._review_only.setChecked(True)  # only entries flagged for review
    assert view._proxy.rowCount() == 1
    view._review_only.setChecked(False)
    assert view._proxy.rowCount() == 2
    view.deleteLater()


def test_inventory_inline_item_edits(app, conn):
    from PySide6.QtCore import Qt

    from tcginventory.domain.enums import Condition, HoloPattern
    from tcginventory.ui.views.inventory import InventoryView

    r = buy(
        TransactionService(conn), "Pikachu", 100, 200, "2026-01-01",
        condition=Condition.NM,
    )
    item_id = r.lots[0].item_id
    view = InventoryView(conn)
    model = view._model
    items = ItemRepository(conn)

    def col(header: str) -> int:
        return next(
            i for i in range(model.columnCount())
            if model.headerData(i, Qt.Horizontal) == header
        )

    # Text field.
    assert model.setData(model.index(0, col("Name")), "Pikachu V", Qt.EditRole)
    assert items.get(item_id).name == "Pikachu V"

    # Enum field.
    assert model.setData(
        model.index(0, col("Holo")), HoloPattern.REVERSE_HOLO.value, Qt.EditRole
    )
    assert items.get(item_id).holo_pattern is HoloPattern.REVERSE_HOLO

    # Condition → non-NM auto-marks review; back to NM clears it.
    model.setData(model.index(0, col("Cond/Grade")), Condition.LP.value, Qt.EditRole)
    got = items.get(item_id)
    assert got.condition is Condition.LP and got.review is True
    model.setData(model.index(0, col("Cond/Grade")), Condition.NM.value, Qt.EditRole)
    assert items.get(item_id).review is False

    # Editing the current market price marks the item for review.
    model.setData(model.index(0, col("Current Market")), 12.50, Qt.EditRole)
    got = items.get(item_id)
    assert got.market_value_cents == 1250 and got.review is True
    view.deleteLater()


def test_inventory_graded_filter(app, conn):
    from tcginventory.domain.enums import GradingCompany
    from tcginventory.ui.views.inventory import InventoryView

    r1 = buy(TransactionService(conn), "Pikachu", 100, 200, "2026-01-01")
    buy(TransactionService(conn), "Charizard", 100, 200, "2026-01-02")
    items = ItemRepository(conn)
    it = items.get(r1.lots[0].item_id)
    it.is_graded = True
    it.condition = None
    it.grading_company = GradingCompany.PSA
    it.grade = "10"
    items.update(it)
    conn.commit()

    view = InventoryView(conn)
    assert view._proxy.rowCount() == 2  # All
    view._graded.setCurrentText("Graded")
    assert view._proxy.rowCount() == 1
    view._graded.setCurrentText("Singles")
    assert view._proxy.rowCount() == 1
    view._graded.setCurrentText("All")
    assert view._proxy.rowCount() == 2
    view.deleteLater()


def test_item_editor_changes_condition_and_language(app, conn):
    from tcginventory.ui.views.item_editor import ItemEditorDialog

    result = buy(
        TransactionService(conn),
        "Charizard",
        4000,
        6000,
        "2026-01-01",
        set_code="BS",
        card_number="004/102",
        condition=Condition.NM,
    )
    item_id = result.lots[0].item_id

    dlg = ItemEditorDialog(conn, item_id)
    dlg._select(dlg._condition, Condition.LP.value)
    dlg._select(dlg._language, Language.JP.value)
    dlg._save()

    item = ItemRepository(conn).get(item_id)
    assert item.condition is Condition.LP
    assert item.language is Language.JP
    dlg.deleteLater()


def test_item_editor_non_nm_condition_auto_marks_review(app, conn):
    from tcginventory.ui.views.item_editor import ItemEditorDialog

    result = buy(
        TransactionService(conn), "Charizard", 4000, 6000, "2026-01-01",
        set_code="BS", card_number="004/102", condition=Condition.NM,
    )
    item_id = result.lots[0].item_id

    dlg = ItemEditorDialog(conn, item_id)
    assert not dlg._review.isChecked()
    dlg._select(dlg._condition, Condition.MP.value)  # worse than NM
    assert dlg._review.isChecked()  # auto-flagged
    dlg._select(dlg._condition, Condition.NM.value)  # back to NM
    assert not dlg._review.isChecked()  # auto-cleared
    dlg._select(dlg._condition, Condition.HP.value)
    dlg._save()
    assert ItemRepository(conn).get(item_id).review is True
    dlg.deleteLater()


def test_item_editor_toggles_grading(app, conn):
    from tcginventory.ui.views.item_editor import ItemEditorDialog

    result = buy(
        TransactionService(conn),
        "Blastoise",
        3000,
        3000,
        "2026-01-01",
        condition=Condition.NM,
    )
    item_id = result.lots[0].item_id

    dlg = ItemEditorDialog(conn, item_id)
    dlg._graded.setChecked(True)
    dlg._select(dlg._company, GradingCompany.PSA.value)
    dlg._grade.setText("10")
    dlg._save()

    item = ItemRepository(conn).get(item_id)
    assert item.is_graded is True
    assert item.condition is None  # cleared when graded
    assert item.grading_company is GradingCompany.PSA
    assert item.grade == "10"
    dlg.deleteLater()
