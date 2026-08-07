"""The startup screen, and the progress contract it depends on."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tcginventory.ui.main_window import NAV_ITEMS  # noqa: E402


def test_steps_name_the_work_and_fill_the_bar(app):
    from tcginventory.ui.splash import StartupSplash

    splash = StartupSplash(total_steps=3)
    splash.begin("Opening the ledger…")
    assert splash._status.text() == "Opening the ledger…"
    assert splash._bar.value() == 0  # begin() is a state, not a step

    splash.step("Loading Inventory…")
    assert splash._status.text() == "Loading Inventory…"
    assert splash._bar.value() == 1
    assert splash._bar.maximum() == 3

    splash.step("Loading Market…")
    assert splash._bar.value() == 2
    splash.close()


def test_extra_steps_clamp_instead_of_overflowing(app):
    """Better a bar that sits full than one reporting 14 of 11."""
    from tcginventory.ui.splash import StartupSplash

    splash = StartupSplash(total_steps=2)
    for _ in range(6):
        splash.step("…")
    assert splash._bar.value() == splash._bar.maximum() == 2
    splash.close()


def test_finish_hands_over_to_the_window(app):
    from PySide6.QtWidgets import QLabel

    from tcginventory.ui.splash import StartupSplash

    splash = StartupSplash(total_steps=4)
    splash.begin()
    window = QLabel("the real window")

    splash.finish(window)

    assert splash._bar.value() == splash._bar.maximum()  # never stops short
    assert not splash.isVisible()
    assert window.isVisible()
    window.close()


def test_finish_without_a_window_still_closes(app):
    from tcginventory.ui.splash import StartupSplash

    splash = StartupSplash(total_steps=2)
    splash.begin()
    splash.finish()
    assert not splash.isVisible()


def test_mascot_ground_is_keyed_out(app):
    """The GIF ships an opaque grey ground; on the dark card it has to go."""
    from tcginventory.ui.splash import _MASCOT_GROUND, StartupSplash

    splash = StartupSplash(total_steps=2)
    splash._show_frame()
    pixmap = splash._mascot.pixmap()

    assert not pixmap.isNull()
    assert not pixmap.mask().isNull()  # a mask means the ground is transparent
    image = pixmap.toImage()
    assert image.pixelColor(0, 0).alpha() == 0, "the corner should be see-through"
    assert _MASCOT_GROUND.name() == "#696969"
    splash.close()


def test_main_window_reports_every_view_in_nav_order(app, conn):
    """The splash names each view as it lands — and this is also the guard that
    build order, stack order and NAV_ITEMS stay the same sequence, since the nav
    indexes straight into the stack."""
    from tcginventory.ui.main_window import MainWindow

    seen: list[tuple[str, str]] = []
    window = MainWindow(conn, progress=lambda name, phase: seen.append((name, phase)))

    built = [name for name, phase in seen if phase == "Loading"]
    prepared = [name for name, phase in seen if phase == "Preparing"]
    assert built == NAV_ITEMS
    assert prepared == NAV_ITEMS  # every view is pre-loaded, in the same order
    assert window._stack.count() == len(NAV_ITEMS)
    assert window._stack.widget(NAV_ITEMS.index("Operations")) is window._operations
    assert window._stack.widget(NAV_ITEMS.index("Dashboard")) is window._dashboard
    window.close()


def test_every_view_is_preloaded_so_navigation_never_refreshes(app, conn):
    """The point of pre-loading: the first visit to a screen costs nothing,
    because its refresh already happened behind the splash.

    Built without a ``progress`` callback, which is also the guarantee that a
    splash is optional — tests and any other caller construct the window this
    way.
    """
    from tcginventory.ui.main_window import MainWindow

    window = MainWindow(conn)
    assert window._stack.count() == len(NAV_ITEMS)  # no splash, full window

    # Every view is marked fresh at the current data version, which is what
    # _on_nav_changed checks before paying for a refresh.
    assert set(window._view_versions) == set(range(len(NAV_ITEMS)))
    assert set(window._view_versions.values()) == {window._data_version}

    refreshed: list[int] = []
    for index in range(len(NAV_ITEMS)):
        widget = window._stack.widget(index)
        if hasattr(widget, "refresh"):
            widget.refresh = lambda i=index: refreshed.append(i)
    for index in range(len(NAV_ITEMS)):
        window._nav.setCurrentRow(index)
    assert refreshed == []
    window.close()


def test_a_view_that_fails_to_warm_does_not_stop_startup(app, conn):
    """One broken screen must not cost the user the whole window; it is left
    stale so it refreshes on first visit, exactly as it used to."""
    from tcginventory.ui.main_window import MainWindow

    real = MainWindow._warm_views

    def warm(self, announce):
        index = NAV_ITEMS.index("Market")
        self._stack.widget(index).refresh = lambda: 1 / 0
        real(self, announce)

    MainWindow._warm_views = warm
    try:
        window = MainWindow(conn)
    finally:
        MainWindow._warm_views = real

    assert window._stack.count() == len(NAV_ITEMS)  # the window still came up
    assert NAV_ITEMS.index("Market") not in window._view_versions  # left stale
    window.close()
