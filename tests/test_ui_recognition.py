"""Offscreen tests for the Recognition window's segment card rows."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QStandardItem  # noqa: E402


def _name_field(conn, on_preview):
    from tcginventory.ui.views.recognition import _Box, _NameField

    box = _Box(number=1, rect=QRectF(0, 0, 10, 10))
    field = _NameField(conn, box, on_preview=on_preview)
    item = QStandardItem("Pikachu  ·  Base Set  #58")
    item.setData("base1-58", field._CARD_ID_ROLE)
    field._model.appendRow(item)
    return field


def test_segment_autofill_previews_on_popup_hover(app, conn):
    """Hovering a match in the Segments autofill dropdown previews that card —
    QCompleter's `highlighted` only fires for keyboard nav, so the popup needs
    mouse tracking + `entered` for the pointer to preview too."""
    seen: list[str] = []
    field = _name_field(conn, seen.append)
    popup = field._completer.popup()

    assert popup.hasMouseTracking()  # otherwise `entered` never fires
    popup.entered.emit(popup.model().index(0, 0))
    assert seen == ["base1-58"]
    field.deleteLater()


def test_segment_autofill_previews_on_keyboard_highlight(app, conn):
    """Arrowing through the dropdown still previews (unchanged behaviour)."""
    seen: list[str] = []
    field = _name_field(conn, seen.append)

    field._completer.setCurrentRow(0)
    field._on_highlighted(field._completer.currentIndex())
    assert seen == ["base1-58"]
    field.deleteLater()


# ---------------------------------------------------------------------------
# the Segments panel must not be widened by what a card is called
# ---------------------------------------------------------------------------

_LONG_META = "Scarlet & Violet—Obsidian Flames  ·  #125/197"


def test_elided_label_reports_no_minimum_width(app):
    """Regression: a segment's set·number line reported its whole string as a
    minimum width, which propagated out to the view and pushed the panel off
    screen. The label must not constrain its row."""
    from PySide6.QtWidgets import QSizePolicy

    from tcginventory.ui.views.recognition import _ElidedLabel

    label = _ElidedLabel(_LONG_META)
    assert label.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored
    assert label.minimumWidth() == 0
    assert label.full_text() == _LONG_META  # the text itself is never lost
    label.deleteLater()


def test_elided_label_shortens_to_fit_and_explains_itself(app):
    """Text is measured against the width the label has when it's set — which is
    the real path: the row is built, then ``_refresh_meta`` fills it in."""
    from tcginventory.ui.views.recognition import _ElidedLabel

    label = _ElidedLabel()
    label.resize(120, 18)
    label.setText(_LONG_META)
    assert label.text() != _LONG_META  # cut down to the space available
    assert label.text().endswith("…")
    assert label.toolTip() == _LONG_META  # …with the full value a hover away

    label.resize(600, 18)  # room for all of it
    label.setText(_LONG_META)
    assert label.text() == _LONG_META
    assert label.toolTip() == ""  # no tooltip that only repeats what's shown
    label.deleteLater()


def test_segment_row_is_not_widened_by_a_long_set_name(app, conn, tmp_path):
    """The point of the whole change: a labelled card with a long set name must
    not set the floor for the row — and so for the panel, and so for the window."""
    from PIL import Image

    from tcginventory.data.vision import CardLabel
    from tcginventory.ui.views.recognition import RecognitionView, _Box

    scene = tmp_path / "scene.png"
    Image.new("RGB", (400, 300), "white").save(scene)

    view = RecognitionView(conn, prewarm=False)
    view.add_image_paths([str(scene)])
    session = view._sessions[view._current]
    session.boxes.append(
        _Box(
            number=1,
            rect=QRectF(10, 10, 80, 120),
            card=CardLabel(
                card_id="sv3-125",
                name="Charizard ex",
                set_name="Scarlet & Violet—Obsidian Flames",
                card_number="125/197",
            ),
        )
    )
    view._refresh_panel()

    row = view._seg_rows[1]
    # Was 755px with this card labelled; the floor is now its controls alone.
    assert row.minimumSizeHint().width() < 420
    view.deleteLater()


def test_suggestion_combo_is_bounded_on_both_sides(app, conn):
    """It used to size to its longest ranked guess (~313px); an ignored policy
    then let the layout collapse it to nothing. It has to be usable *and* bounded."""
    from tcginventory.data.vision import CardLabel
    from tcginventory.ui.views.recognition import _Box, _NameField, _SuggestBox

    box = _Box(number=1, rect=QRectF(0, 0, 10, 10))
    field = _NameField(conn, box)
    combo = _SuggestBox(box, field)
    combo.set_suggestions(
        [
            (
                CardLabel(
                    card_id="sv3-223",
                    name="Charizard ex (Special Illustration Rare)",
                    set_name="Scarlet & Violet—Obsidian Flames",
                ),
                0.94,
            )
        ]
    )

    assert combo.minimumWidth() >= 100  # still a control you can hit
    # The style's own hint for a combo is ~194px whatever the items say, so the
    # cap is what bounds it: Qt clamps the effective minimum to maximumWidth.
    assert combo.maximumWidth() <= 200
    assert min(combo.minimumSizeHint().width(), combo.maximumWidth()) <= 190
    # The popup keeps the full ranked label readable even though the combo is narrow.
    assert combo.view().minimumWidth() > combo.maximumWidth()
    combo.deleteLater()
    field.deleteLater()


# ---------------------------------------------------------------------------
# hovering a ranked guess previews it
# ---------------------------------------------------------------------------


def _suggest_box(conn, on_preview, labeled=None):
    from tcginventory.data.vision import CardLabel
    from tcginventory.ui.views.recognition import _Box, _NameField, _SuggestBox

    box = _Box(number=1, rect=QRectF(0, 0, 10, 10), card=labeled or CardLabel())
    field = _NameField(conn, box)
    combo = _SuggestBox(box, field, on_preview=on_preview)
    combo.set_suggestions(
        [
            (CardLabel(card_id="sv3-125", name="Charizard ex"), 0.94),
            (CardLabel(card_id="base1-58", name="Pikachu"), 0.71),
        ]
    )
    return box, field, combo


def test_suggestion_hover_previews_that_card(app, conn):
    """The point of the ranked list is comparing guesses against the photo, so
    highlighting one — by pointer or by arrow key — has to show its art."""
    seen: list[str] = []
    _box, field, combo = _suggest_box(conn, seen.append)

    assert combo.view().hasMouseTracking()  # hover has to make an item current
    combo.highlighted.emit(1)
    combo.highlighted.emit(2)
    assert seen == ["sv3-125", "base1-58"]

    seen.clear()
    combo.highlighted.emit(0)  # the "— pick a match —" placeholder
    assert seen == []  # opening the popup must not disturb the pane
    combo.deleteLater()
    field.deleteLater()


def test_closing_the_suggestions_restores_the_rows_own_card(app, conn):
    """Browsing guesses is a detour; leaving it hands the pane back to the card
    this row is actually labeled with."""
    from tcginventory.data.vision import CardLabel

    seen: list[str] = []
    _box, field, combo = _suggest_box(
        conn, seen.append, labeled=CardLabel(card_id="swsh4-25", name="Pikachu VMAX")
    )

    combo.showPopup()
    seen.clear()  # opening highlights the placeholder row; nothing to preview
    combo.highlighted.emit(1)
    combo.hidePopup()
    assert seen == ["sv3-125", "swsh4-25"]
    combo.deleteLater()
    field.deleteLater()


def test_disabling_the_combo_is_not_a_browse(app, conn):
    """Qt calls hidePopup when a combo is disabled — which ``set_suggestions``
    does on every rescan. That must not push art at the preview pane."""
    from tcginventory.data.vision import CardLabel

    seen: list[str] = []
    _box, field, combo = _suggest_box(
        conn, seen.append, labeled=CardLabel(card_id="swsh4-25", name="Pikachu VMAX")
    )

    combo.set_suggestions([])  # no match in corpus → disabled → hidePopup
    assert seen == []
    combo.deleteLater()
    field.deleteLater()


def test_closing_an_unlabeled_rows_suggestions_keeps_the_last_guess(app, conn):
    """With no identity to fall back to, the pane keeps what was last hovered
    rather than blanking (the platform-wide preview convention)."""
    seen: list[str] = []
    _box, field, combo = _suggest_box(conn, seen.append)

    combo.showPopup()
    combo.highlighted.emit(2)
    combo.hidePopup()
    assert seen == ["base1-58"]  # nothing overwrote it on the way out
    combo.deleteLater()
    field.deleteLater()


# ---------------------------------------------------------------------------
# deleting a box — the stage's Delete only ever acted on the scene selection,
# and draw mode swallowed every click, so nothing could be selected to delete
# ---------------------------------------------------------------------------


def _staged(conn, tmp_path, *rects):
    """A Recognition window with one image and a box per rect."""
    from PIL import Image
    from PySide6.QtTest import QTest

    from tcginventory.ui.views.recognition import RecognitionView

    scene = tmp_path / "scene.png"
    Image.new("RGB", (400, 300), "white").save(scene)

    view = RecognitionView(conn, prewarm=False)
    view.resize(1200, 800)
    view.show()
    QTest.qWaitForWindowExposed(view)
    view.add_image_paths([str(scene)])
    view._fit_view()
    for rect in rects:
        view._add_box(rect)
    return view


def _numbers(view):
    return [b.number for b in view._sessions[view._current].boxes]


def _click_box(view, number):
    """Click a box through the real viewport, the way a user does."""
    from PySide6.QtTest import QTest

    item = view._items[number]
    point = view._view.mapFromScene(item.mapToScene(item.rect().center()))
    QTest.mouseClick(view._view.viewport(), Qt.LeftButton, Qt.NoModifier, point)


def test_a_box_can_be_selected_while_draw_mode_is_on(app, conn, tmp_path):
    """Regression: draw mode accepted every press to start a rubber band, so a
    click on an existing box never reached Qt's selection handling. Draw mode
    persists across images, so this was the state you were in right after
    drawing — and Delete, which acts on the selection, had nothing to act on."""
    view = _staged(conn, tmp_path, QRectF(10, 10, 120, 140), QRectF(150, 10, 120, 140))
    view._set_draw(True)

    _click_box(view, 2)

    assert [i.data(0) for i in view._scene.selectedItems()] == [2]
    view._delete_selected()
    assert _numbers(view) == [1]
    view.deleteLater()


def test_dragging_from_inside_a_box_still_draws_a_new_one(app, conn, tmp_path):
    """The other half of that guard: overlapping cards mean you sometimes start
    a box on top of one you already drew. A press that travels is still a draw;
    only a press released in place is a selection."""
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    view = _staged(conn, tmp_path, QRectF(10, 10, 200, 200))
    view._set_draw(True)
    item = view._items[1]
    start = view._view.mapFromScene(item.mapToScene(item.rect().center()))
    end = QPoint(start.x() + 110, start.y() + 80)

    QTest.mousePress(view._view.viewport(), Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(view._view.viewport(), QPoint(start.x() + 40, start.y() + 30))
    QTest.mouseMove(view._view.viewport(), end)
    QTest.mouseRelease(view._view.viewport(), Qt.LeftButton, Qt.NoModifier, end)

    assert _numbers(view) == [1, 2]
    view.deleteLater()


def test_delete_key_removes_the_selected_box(app, conn, tmp_path):
    """There was no key binding at all — the only paths were two buttons."""
    from PySide6.QtTest import QTest

    view = _staged(conn, tmp_path, QRectF(10, 10, 120, 140), QRectF(150, 10, 120, 140))
    _click_box(view, 1)

    QTest.keyClick(view._view.viewport(), Qt.Key_Delete)

    assert _numbers(view) == [2]
    view.deleteLater()


def test_deleting_a_box_never_renumbers_the_survivors(app, conn, tmp_path):
    """A card's front and back join on (pair_group, box number), so closing the
    gap left by a delete would silently re-pair every card past it. Numbers are
    identity: the hole stays, and the next box drawn takes the next number up."""
    view = _staged(
        conn,
        tmp_path,
        QRectF(10, 10, 100, 120),
        QRectF(120, 10, 100, 120),
        QRectF(230, 10, 100, 120),
    )

    view._delete_box(2)

    assert _numbers(view) == [1, 3]
    view._add_box(QRectF(10, 150, 100, 120))
    assert _numbers(view) == [1, 3, 4]  # never reuses 2
    view.deleteLater()


def test_each_segment_row_can_delete_its_own_card(app, conn, tmp_path):
    """The discoverable path: no mode, no scene selection, just the row."""
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton

    view = _staged(conn, tmp_path, QRectF(10, 10, 120, 140), QRectF(150, 10, 120, 140))
    row = view._seg_rows[1]
    trash = [b for b in row.findChildren(QPushButton) if b.objectName() == "segDel"]
    assert len(trash) == 1

    QTest.mouseClick(trash[0], Qt.LeftButton)

    assert _numbers(view) == [2]
    assert 1 not in view._items  # the box left the scene too, not just the model
    view.deleteLater()


def test_a_selected_box_stays_marked_after_a_hover_passes_over_it(app, conn, tmp_path):
    """Selection and hover both wrote the outline, and hover-out reset it to the
    resting pen — so selecting a box then brushing the cursor over it made the
    selection invisible while it was still very much selected."""
    view = _staged(conn, tmp_path, QRectF(10, 10, 120, 140), QRectF(150, 10, 120, 140))
    item, other = view._items[1], view._items[2]
    item.setSelected(True)
    selected_pen = item.pen().color().name()
    assert selected_pen != other.pen().color().name()  # visibly different at rest

    view._set_highlight(1)  # hover in
    view._set_highlight(None)  # and out again

    assert item.pen().color().name() == selected_pen
    view.deleteLater()


def test_deleting_with_nothing_selected_explains_itself(
    app, conn, tmp_path, monkeypatch
):
    """Silence here is what made Delete read as broken."""
    from tcginventory.ui.views import recognition as mod

    view = _staged(conn, tmp_path, QRectF(10, 10, 120, 140))
    view._scene.clearSelection()
    shown: list[str] = []
    monkeypatch.setattr(
        mod.QMessageBox, "information", lambda *a, **k: shown.append(a[1])
    )

    view._delete_selected()

    assert shown == ["No box selected"]
    assert _numbers(view) == [1]  # and nothing was removed
    view.deleteLater()
