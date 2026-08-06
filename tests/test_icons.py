"""A missing icon must not be able to take a window down.

An unknown icon name used to raise out of ``pixmap``, part-way through building
a widget. The visible symptom was not a missing glyph but an access violation in
an unrelated test several files later, because Qt was left holding a
half-constructed object.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tcginventory.ui import icons  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_a_known_icon_renders(app):
    pm = icons.pixmap("database", "#ff8800", 18)
    assert not pm.isNull() and pm.size().width() == 18


def test_an_unknown_icon_is_blank_rather_than_fatal(app):
    pm = icons.pixmap("no-such-icon-anywhere", "#ff8800", 18)
    assert not pm.isNull()  # a real pixmap, just empty
    assert pm.size().width() == 18


def test_an_unknown_icon_still_makes_a_usable_qicon(app):
    """Call sites build a QIcon and hand it to a widget — that must keep working
    so a typo costs a glyph, not the window."""
    assert icons.icon("no-such-icon-anywhere", "#ffffff", 16) is not None


def test_every_icon_the_operations_registry_asks_for_exists(app):
    """The registry names icons as strings, so a typo is invisible until the
    tile is drawn. This is the check that would have caught one."""
    from tcginventory.services.operations import OPERATIONS

    # Asking the renderer proves nothing now that it degrades quietly — the
    # bundled file is the thing to check.
    unbundled = [
        f"{op.id} -> {op.icon}"
        for op in OPERATIONS
        if op.icon and icons._svg_text(op.icon) is None
    ]
    assert unbundled == []
