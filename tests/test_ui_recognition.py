"""Offscreen tests for the Recognition window's segment card rows."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QStandardItem  # noqa: E402

from tcginventory.data.db import connect  # noqa: E402
from tcginventory.data.migrator import migrate  # noqa: E402


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
