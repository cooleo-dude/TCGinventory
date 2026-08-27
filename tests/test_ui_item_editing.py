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


def test_inventory_edit_market_at_purchase(app, conn):
    from PySide6.QtCore import Qt

    from tcginventory.ui.models.inventory_model import MARKET_AT_PURCHASE_COL
    from tcginventory.ui.views.inventory import InventoryView

    # Paid $80; market-at-purchase recorded as $100 (an NM figure to correct).
    result = buy(TransactionService(conn), "Alakazam", 8000, 10000, "2026-01-01")
    lot_id = result.lots[0].id
    assert LotRepository(conn).get(lot_id).unit_cost_cents == 8000

    view = InventoryView(conn)
    idx = view._model.index(0, MARKET_AT_PURCHASE_COL)
    assert view._model.data(idx, Qt.EditRole) == "100.00"  # dollars, editable
    assert view._model.setData(idx, "62.50", Qt.EditRole)

    lot = LotRepository(conn).get(lot_id)
    assert lot.acquired_market_value_cents == 6250  # corrected snapshot
    assert lot.unit_cost_cents == 8000  # cost basis untouched (record fix only)

    # Invalid / unchanged edits are rejected without a write.
    assert view._model.setData(idx, "-5", Qt.EditRole) is False
    assert view._model.setData(idx, "abc", Qt.EditRole) is False
    assert LotRepository(conn).get(lot_id).acquired_market_value_cents == 6250
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
        TransactionService(conn),
        "Pikachu",
        100,
        200,
        "2026-01-01",
        condition=Condition.NM,
    )
    item_id = r.lots[0].item_id
    view = InventoryView(conn)
    model = view._model
    items = ItemRepository(conn)

    def col(header: str) -> int:
        return next(
            i
            for i in range(model.columnCount())
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
    view._graded.select("Graded")
    assert view._proxy.rowCount() == 1
    view._graded.select("Singles")
    assert view._proxy.rowCount() == 1
    view._graded.select("All")
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


# ---------------------------------------------------------------------------
# the row editor: one screen, every field (ticket 61)
# ---------------------------------------------------------------------------


def _row_editor(conn, name="Charizard", **kw):
    """Post a purchase and open the editor on that row — item *and* lot."""
    from tcginventory.ui.views.item_editor import ItemEditorDialog

    result = buy(TransactionService(conn), name, 4000, 6000, "2026-01-01", **kw)
    lot = result.lots[0]
    return ItemEditorDialog(conn, lot.item_id, lot_id=lot.id), lot


def test_double_click_opens_the_editor_not_a_read_only_dialog(app, conn):
    """The whole point of the ticket: no read-only hop, no "Edit item…" button."""
    from tcginventory.ui.views.inventory import InventoryView
    from tcginventory.ui.views.item_editor import ItemEditorDialog

    buy(TransactionService(conn), "Snorlax", 1000, 1000, "2026-01-01")
    view = InventoryView(conn)
    opened: list = []
    view._open_editor = lambda idx: opened.append(idx)  # capture the wiring
    view._table.doubleClicked.emit(view._proxy.index(0, view._col("Name")))
    assert opened, "double-click is not wired to the row editor"

    # And the retired dialog really is gone, not merely unused.
    with pytest.raises(ModuleNotFoundError):
        __import__("tcginventory.ui.views.item_detail")
    assert not hasattr(ItemEditorDialog, "_edit_item")  # no second-dialog hop
    view.deleteLater()


def test_editor_exposes_every_editable_field_on_one_screen(app, conn):
    """Guard against a field being re-gated behind another dialog later.

    Each name is a column the user is meant to be able to change from the row
    editor; if one loses its widget, this goes red rather than the field quietly
    becoming unreachable.
    """
    dlg, _lot = _row_editor(conn)
    item_fields = [
        "_name",
        "_category",
        "_set_code",
        "_set_name",
        "_card_number",
        "_variant",
        "_language",
        "_graded",
        "_condition",
        "_holo",
        "_edition",
        "_company",
        "_grade",
        "_cert",
        "_market",
        "_location",
        "_tcgplayer",
        "_ebay",
        "_notes",
        "_review",
    ]
    lot_fields = ["_intent", "_lot_market", "_source", "_lot_notes", "_missing"]
    missing = [f for f in item_fields + lot_fields if getattr(dlg, f, None) is None]
    assert not missing, f"no widget for: {missing}"
    dlg.deleteLater()


def test_every_editable_field_is_visible_without_scrolling(app, conn):
    """Having a widget is not the same as being findable.

    Mark-for-review shipped in a Filing section 157px below the fold and was
    reported missing the same day. Only the read-only Ledger may sit below it.
    """
    from PySide6.QtWidgets import QScrollArea

    dlg, _lot = _row_editor(conn)
    dlg.show()
    app.processEvents()
    scroll = dlg.findChild(QScrollArea)
    fold = scroll.viewport().height()

    fields = [
        "_name",
        "_category",
        "_set_code",
        "_set_name",
        "_card_number",
        "_variant",
        "_language",
        "_graded",
        "_condition",
        "_holo",
        "_edition",
        "_company",
        "_grade",
        "_cert",
        "_market",
        "_review",
        "_tcgplayer",
        "_ebay",
        "_location",
        "_notes",
        "_intent",
        "_lot_market",
        "_source",
        "_lot_notes",
        "_missing",
    ]
    below = {}
    for name in fields:
        widget = getattr(dlg, name)
        bottom = widget.mapTo(scroll.widget(), widget.rect().bottomLeft()).y()
        if bottom > fold:
            below[name] = bottom - fold
    assert not below, f"below the fold by (px): {below}"
    dlg.deleteLater()


def test_review_sits_with_the_price_it_protects(app, conn):
    """Review is a pricing control, not a filing one — it belongs beside Market
    value, which is also what keeps it above the fold."""
    dlg, _lot = _row_editor(conn)
    dlg.show()
    app.processEvents()  # geometry is all zeroes until the layout runs
    section = dlg._market.parent().parent()  # the Pricing & links frame
    assert dlg._review.parent() is section
    # …and directly under the value, not above it.
    market_y = dlg._market.mapTo(section, dlg._market.rect().bottomLeft()).y()
    review_y = dlg._review.mapTo(section, dlg._review.rect().topLeft()).y()
    assert review_y > market_y
    dlg.deleteLater()


def test_editor_round_trips_the_per_copy_lot_fields(app, conn):
    """Source and lot notes were editable nowhere in the UI before this."""
    dlg, lot = _row_editor(conn)
    dlg._select(dlg._intent, Intent.PERSONAL.value)
    dlg._lot_market.setValue(72.50)
    dlg._source.setText("Toronto show, table 14")
    dlg._lot_notes.setText("Slight edge wear on this copy")
    dlg._save()

    saved = LotRepository(conn).get(lot.id)
    assert saved.intent is Intent.PERSONAL
    assert saved.acquired_market_value_cents == 7250
    assert saved.source == "Toronto show, table 14"
    assert saved.notes == "Slight edge wear on this copy"
    dlg.deleteLater()


def test_editing_market_at_purchase_does_not_move_cost_basis(app, conn):
    """It is an acquisition *snapshot*, not a valuation input (spec 02)."""
    dlg, lot = _row_editor(conn)
    before = LotRepository(conn).get(lot.id).unit_cost_cents
    dlg._lot_market.setValue(999.00)
    dlg._save()
    after = LotRepository(conn).get(lot.id)
    assert after.acquired_market_value_cents == 99_900
    assert after.unit_cost_cents == before  # allocation was not re-run
    dlg.deleteLater()


def test_editor_missing_toggle_goes_through_the_service(app, conn):
    """`missing` moves money, so it must write an append-only audit row — a raw
    `lot.missing = ...` would flip the flag and skip it (Invariant 9)."""
    dlg, lot = _row_editor(conn)
    before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    dlg._missing.setChecked(True)
    dlg._save()

    saved = LotRepository(conn).get(lot.id)
    assert saved.missing is True
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] > before
    dlg.deleteLater()


def test_editor_unticking_missing_restores_the_lot(app, conn):
    dlg, lot = _row_editor(conn)
    dlg._missing.setChecked(True)
    dlg._save()
    assert LotRepository(conn).get(lot.id).missing is True

    from tcginventory.ui.views.item_editor import ItemEditorDialog

    again = ItemEditorDialog(conn, lot.item_id, lot_id=lot.id)
    assert again._missing.isChecked()  # reflects stored state
    again._missing.setChecked(False)
    again._save()
    restored = LotRepository(conn).get(lot.id)
    assert restored.missing is False
    assert restored.remaining_qty == lot.remaining_qty
    dlg.deleteLater()
    again.deleteLater()


def test_editor_notes_keep_their_line_breaks(app, conn):
    """Notes is multi-line now, and toggling review must not flatten it."""
    dlg, lot = _row_editor(conn)
    dlg._notes.setPlainText("Signed by artist\nCorner ding, back left")
    dlg._review.setChecked(True)
    dlg._save()
    notes = ItemRepository(conn).get(lot.item_id).notes
    assert "Signed by artist\nCorner ding, back left" in notes
    assert "Manual price changed detected." in notes

    from tcginventory.ui.views.item_editor import ItemEditorDialog

    again = ItemEditorDialog(conn, lot.item_id, lot_id=lot.id)
    # The marker is stripped for display, the user's two lines survive.
    assert again._notes.toPlainText() == "Signed by artist\nCorner ding, back left"
    again._review.setChecked(False)
    again._save()
    assert ItemRepository(conn).get(lot.item_id).notes == (
        "Signed by artist\nCorner ding, back left"
    )
    dlg.deleteLater()
    again.deleteLater()


def test_editor_without_a_lot_hides_the_per_copy_section(app, conn):
    """Constructible from a bare item — that is how it is reached outside the
    grid, and the per-copy fields have nothing to point at."""
    from tcginventory.ui.views.item_editor import ItemEditorDialog

    result = buy(TransactionService(conn), "Gengar", 1000, 1000, "2026-01-01")
    dlg = ItemEditorDialog(conn, result.lots[0].item_id)
    assert dlg._lot is None
    dlg._save()  # must not blow up with no lot to write
    dlg.deleteLater()


def test_editor_warns_when_item_fields_are_shared_by_several_copies(app, conn):
    """The visible form of the ticket-52 rule: a row is a lot, but Identity /
    Pricing / Filing are item-wide."""
    from tcginventory.ui.views.item_editor import ItemEditorDialog

    svc = TransactionService(conn)
    first = buy(svc, "Rayquaza", 1000, 1000, "2026-01-01")
    item_id = first.lots[0].item_id
    lone = ItemEditorDialog(conn, item_id, lot_id=first.lots[0].id)
    assert lone._shared_note is None  # one copy → nothing to warn about
    lone.deleteLater()

    buy(svc, "Rayquaza", 1200, 1200, "2026-02-01")  # a second copy of the card
    shared = ItemEditorDialog(conn, item_id, lot_id=first.lots[0].id)
    assert shared._shared_note is not None
    assert "2 copies" in shared._shared_note
    shared.deleteLater()
