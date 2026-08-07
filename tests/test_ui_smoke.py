"""Smoke test: the main window constructs under the offscreen Qt platform.

Skipped cleanly if PySide6 isn't installed, so the pure-Python test suite still
runs in minimal environments.

One window for the module. Constructing it warms all eleven views, which is the
single most expensive thing the suite does, and none of these three assertions
needs a pristine one — they read the title, the mascot, and the mascot's
response to focus.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

# Must be set before any QApplication is created; harmless if already headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def window(app):
    from tcginventory.ui.main_window import MainWindow

    w = MainWindow()
    yield w
    w.close()


def test_main_window_constructs(window):
    from tcginventory.ui.main_window import NAV_ITEMS

    assert window.windowTitle().startswith("TCGinventory")
    assert NAV_ITEMS[0] == "Dashboard"


def test_nav_shows_animated_scrafty_mascot(window):
    assert window._scrafty.movie() is window._scrafty_movie
    assert window._scrafty_movie.isValid()  # the bundled gif loaded
    assert window._scrafty_movie.frameCount() > 1  # animated


def test_scrafty_pauses_when_window_unfocused(window):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMovie

    window.isActiveWindow = lambda: True  # focused → animating
    window.changeEvent(QEvent(QEvent.Type.ActivationChange))
    assert window._scrafty_movie.state() == QMovie.MovieState.Running

    window.isActiveWindow = lambda: False  # unfocused → paused
    window.changeEvent(QEvent(QEvent.Type.ActivationChange))
    assert window._scrafty_movie.state() == QMovie.MovieState.Paused

    # Hand the shared window back as it was found, so this stays independent of
    # the order the module's tests run in.
    del window.isActiveWindow
