"""A card's front and back always carry the same condition — the last edit wins.

The Recognition window treats a front photo and its back shot as two views of the
same cards (boxes are copied across and matched by number), so grading either face
must grade the card. Before this, fronts kept their default NM while backs were
graded, and the corpus ended up with 8 cards labelled both NM and DMG.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF  # noqa: E402

from tcginventory.data.db import connect  # noqa: E402
from tcginventory.data.migrator import migrate  # noqa: E402
from tcginventory.data.vision import VisionRepository, open_vision  # noqa: E402
from tcginventory.domain.enums import Condition  # noqa: E402


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)


def _view(tmp_path):
    from PIL import Image

    from tcginventory.ui.views.recognition import RecognitionView

    conn = connect(":memory:")
    migrate(conn)
    scene = tmp_path / "front.png"
    Image.new("RGB", (240, 160), color=(190, 190, 190)).save(scene)
    view = RecognitionView(
        conn, vision_repo=VisionRepository(open_vision(":memory:")), vision_dir=tmp_path
    )
    view._mode.setCurrentIndex(1)  # listing mode: no give/receive divider
    view.add_image_paths([str(scene)])
    return view, conn


def _paired(view):
    """Add the back shot for the current front, and return (front, back)."""
    front = view._sessions[view._current]
    view._include_card_backs()
    back = next(
        s for s in view._sessions if s is not front and s.pair_id == front.pair_id
    )
    return front, back


def test_editing_the_front_condition_grades_the_back(app, tmp_path):
    view, conn = _view(tmp_path)
    box = view._add_box(QRectF(10, 10, 60, 90))
    front, back = _paired(view)
    assert [b.number for b in back.boxes] == [box.number]  # copied across

    combo = view._condition_boxes[box.number]
    combo.setCurrentIndex(combo.findData(Condition.MP.value))

    assert box.condition == Condition.MP.value
    assert back.boxes[0].condition == Condition.MP.value  # the card, not the face
    view.deleteLater()
    conn.close()


def test_editing_the_back_condition_grades_the_front(app, tmp_path):
    """The direction that was actually broken: backs get graded, fronts kept NM."""
    view, conn = _view(tmp_path)
    box = view._add_box(QRectF(10, 10, 60, 90))
    front, back = _paired(view)

    # Switch to the back face and grade it there.
    view._strip.setCurrentRow(view._sessions.index(back))
    combo = view._condition_boxes[box.number]
    combo.setCurrentIndex(combo.findData(Condition.DMG.value))

    assert back.boxes[0].condition == Condition.DMG.value
    assert front.boxes[0].condition == Condition.DMG.value  # no longer stuck at NM
    view.deleteLater()
    conn.close()


def test_the_most_recent_edit_wins_on_both_faces(app, tmp_path):
    view, conn = _view(tmp_path)
    box = view._add_box(QRectF(10, 10, 60, 90))
    front, back = _paired(view)

    combo = view._condition_boxes[box.number]
    combo.setCurrentIndex(combo.findData(Condition.LP.value))  # graded on the front
    view._strip.setCurrentRow(view._sessions.index(back))
    back_combo = view._condition_boxes[box.number]
    assert back_combo.currentData() == Condition.LP.value  # picker shows it too

    back_combo.setCurrentIndex(back_combo.findData(Condition.HP.value))  # regraded
    assert back.boxes[0].condition == Condition.HP.value
    assert front.boxes[0].condition == Condition.HP.value
    view.deleteLater()
    conn.close()


def test_mirroring_a_condition_does_not_announce_it_again(app):
    """The anti-bounce guarantee, at the picker itself.

    A user's edit announces; mirroring the result onto the other face must not,
    or the two pickers would keep re-notifying each other.
    """
    from tcginventory.ui.views.recognition import _Box, _ConditionBox

    announced: list[str | None] = []
    box = _Box(number=1, rect=QRectF(0, 0, 10, 10))
    combo = _ConditionBox(box, on_change=lambda _b, value: announced.append(value))

    combo.setCurrentIndex(combo.findData(Condition.MP.value))  # a user edit
    assert announced == [Condition.MP.value]

    combo.set_condition(Condition.HP.value)  # mirrored from the other face
    assert announced == [Condition.MP.value]  # silent
    assert box.condition == Condition.HP.value  # but the box did follow
    assert combo.currentData() == Condition.HP.value  # and so did the picker
    combo.deleteLater()


def test_an_unpaired_card_is_unaffected(app, tmp_path):
    """No back shot yet: grading the front is just grading the front."""
    view, conn = _view(tmp_path)
    box = view._add_box(QRectF(10, 10, 60, 90))

    combo = view._condition_boxes[box.number]
    combo.setCurrentIndex(combo.findData(Condition.MP.value))

    assert box.condition == Condition.MP.value
    assert len(view._sessions) == 1
    view.deleteLater()
    conn.close()
