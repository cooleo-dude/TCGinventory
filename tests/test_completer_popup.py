"""Autofill popup sizing — the shared backing for every text entry that completes.

The regression these guard: a resize made from ``textEdited`` is undone by
``QLineEdit``'s own ``completer->complete()`` later in the same key press, which
sizes the popup to the *field*. Every case here types for real (``QTest``) rather
than calling the slot, because calling the slot never reproduced the bug.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QStandardItem, QStandardItemModel  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QCompleter, QLineEdit  # noqa: E402

from tcginventory.ui import completer_popup  # noqa: E402
from tcginventory.ui.completer_popup import show_fitted  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _field(app, labels, *, width=80):
    """A narrow QLineEdit wired to a completer exactly like the real entry rows:
    setCompleter first, then our own textEdited slot that opens the popup."""
    field = QLineEdit()
    model = QStandardItemModel(field)
    for label in labels:
        model.appendRow(QStandardItem(label))
    completer = QCompleter(model, field)
    completer.setCaseSensitivity(Qt.CaseInsensitive)
    completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
    field.setCompleter(completer)
    field.textEdited.connect(lambda *_: show_fitted(completer, field))
    field.resize(width, 26)
    field.show()
    app.processEvents()
    field.setFocus()
    return field, completer


_LONG = [
    "Basic Lightning Energy (Pikachu Stamped)  ·  sH: Sword & Shield Family  #LIG/LIG1",
    "Arcanine  ·  smP2: Great Detective Pikachu  #008/024",
    "Ash's Pikachu  ·  SM Black Star Promos  #SM108/SM248",
]


def test_typing_keeps_the_popup_at_content_width(app):
    """The bug: QLineEdit re-shows the popup at field width after our resize."""
    field, completer = _field(app, _LONG, width=80)
    QTest.keyClicks(field, "pika")
    app.processEvents()

    popup = completer.popup()
    # The width the helper decided on is the width the popup keeps — QLineEdit's
    # own complete() doesn't get to clamp it back to the 80px field.
    assert popup.width() == completer_popup.fitted_width(completer, field)
    assert popup.width() > 300  # these labels need far more than the field
    field.deleteLater()


def test_without_the_fitter_qlineedit_clamps_to_the_field(app, monkeypatch):
    """Negative control for the case above: disable the fitter's re-apply and the
    popup collapses to the field width again — that was the original bug."""
    monkeypatch.setattr(completer_popup._PopupFitter, "apply", lambda self: None)
    field, completer = _field(app, _LONG, width=80)
    QTest.keyClicks(field, "pika")
    app.processEvents()

    assert completer.popup().width() == field.width() == 80
    field.deleteLater()


def test_popup_is_never_narrower_than_its_field(app):
    field, completer = _field(app, ["Abra", "Onix"], width=420)
    QTest.keyClicks(field, "ab")
    app.processEvents()

    assert completer.popup().width() >= field.width()
    field.deleteLater()


def test_popup_never_exceeds_the_screen(app):
    field, completer = _field(app, ["Pikachu " * 200], width=80)
    QTest.keyClicks(field, "pi")
    app.processEvents()

    popup = completer.popup()
    avail = field.screen().availableGeometry()
    assert popup.width() <= avail.width()
    assert popup.geometry().right() <= avail.right()  # stays on screen
    field.deleteLater()


def test_max_width_still_caps_when_a_caller_asks(app):
    field = QLineEdit()
    model = QStandardItemModel(field)
    model.appendRow(QStandardItem("A very wide label " * 12))
    completer = QCompleter(model, field)
    completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
    field.setCompleter(completer)
    field.textEdited.connect(lambda *_: show_fitted(completer, field, max_width=300))
    field.resize(80, 26)
    field.show()
    app.processEvents()
    field.setFocus()
    QTest.keyClicks(field, "a ")
    app.processEvents()

    assert completer.popup().width() == 300
    field.deleteLater()


def test_the_fitter_is_installed_once_per_completer(app):
    field, completer = _field(app, _LONG)
    for _ in range(3):
        show_fitted(completer, field)
    fitters = completer.findChildren(
        completer_popup._PopupFitter, options=Qt.FindDirectChildrenOnly
    )
    assert len(fitters) == 1
    field.deleteLater()


def test_scrollbar_width_is_reserved_before_the_popup_opens(app):
    """More rows than maxVisibleItems → a scrollbar will appear, so its width has
    to be in the measurement or the widest row is elided by exactly that much."""
    rows = [
        f"Row {i}  ·  a reasonably long label to measure  #{i:03d}" for i in range(30)
    ]
    field, completer = _field(app, rows)
    popup = completer.popup()
    assert popup.model().rowCount() > completer.maxVisibleItems()

    bare = popup.sizeHintForColumn(0) + 2 * popup.frameWidth() + completer_popup._EXTRA
    assert completer_popup._content_width(completer, popup) > bare
    field.deleteLater()
