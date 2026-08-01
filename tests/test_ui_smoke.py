"""Smoke test: the main window constructs under the offscreen Qt platform.

Skipped cleanly if PySide6 isn't installed, so the pure-Python test suite still
runs in minimal environments.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

# Must be set before any QApplication is created; harmless if already headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_main_window_constructs():
    from PySide6.QtWidgets import QApplication

    from tcginventory.ui.main_window import NAV_ITEMS, MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert window.windowTitle().startswith("TCGinventory")
    assert NAV_ITEMS[0] == "Dashboard"
    window.close()
    del window
    app.processEvents()


def test_nav_shows_animated_scrafty_mascot():
    from PySide6.QtWidgets import QApplication

    from tcginventory.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert window._scrafty.movie() is window._scrafty_movie
    assert window._scrafty_movie.isValid()  # the bundled gif loaded
    assert window._scrafty_movie.frameCount() > 1  # animated
    window.close()
    del window
    app.processEvents()


def test_scrafty_pauses_when_window_unfocused():
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMovie
    from PySide6.QtWidgets import QApplication

    from tcginventory.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()

    window.isActiveWindow = lambda: True  # focused → animating
    window.changeEvent(QEvent(QEvent.Type.ActivationChange))
    assert window._scrafty_movie.state() == QMovie.MovieState.Running

    window.isActiveWindow = lambda: False  # unfocused → paused
    window.changeEvent(QEvent(QEvent.Type.ActivationChange))
    assert window._scrafty_movie.state() == QMovie.MovieState.Paused

    window.close()
    del window
    app.processEvents()
