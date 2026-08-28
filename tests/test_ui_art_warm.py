"""The Personal Collection window's art warm and its busy chip (ticket 65)."""

from __future__ import annotations

import pytest

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import CardCatalogRepository, CollectionRepository
from tcginventory.domain.models import CatalogCard, Collection
from tcginventory.services import cardimages
from tcginventory.ui.spinner import BusyChip, Spinner

# isVisible() is not the right predicate here: these widgets sit inside a
# QStackedWidget page on a view that tests never show(), so isVisible() is False
# however the chip was set. isHidden() asks the question that matters — whether
# the widget hid *itself*.


@pytest.fixture()
def qt_wait(app):
    """Spin the event loop for a while so single-shot timers actually fire.

    The chip reveals itself on a QTimer, so a test that asserts it appeared has
    to let the loop run; time.sleep would block it instead.
    """

    def wait(ms: int) -> None:
        import time

        deadline = time.monotonic() + ms / 1000
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()

    return wait


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cardimages, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        cardimages, "cache_path", lambda card_id: tmp_path / f"{card_id}.png"
    )
    return tmp_path


@pytest.fixture()
def view(app, conn):
    from tcginventory.ui.views.collections import CollectionsView

    v = CollectionsView(conn)
    yield v
    v.deleteLater()


def _seed(conn, n=10, with_urls=True):
    CardCatalogRepository(conn).replace_for_language(
        [
            CatalogCard(
                card_id=f"c-{i}",
                name=f"Card {i}",
                set_id="s",
                set_name="S",
                number=str(i),
                printed_total=n,
                image_small=f"http://x/{i}.png" if with_urls else None,
            )
            for i in range(1, n + 1)
        ],
        "EN",
        now_iso(),
    )
    coll = CollectionRepository(conn).add(
        Collection(name="S", template="set", selector="S")
    )
    conn.commit()
    return coll


# ---------------------------------------------------------------------------
# the spinner
# ---------------------------------------------------------------------------


def test_the_spinner_stops_when_hidden(app):
    """A QTimer left running on a hidden widget repaints forever."""
    s = Spinner()
    s.show()
    assert s._timer.isActive()
    s.hide()
    assert not s._timer.isActive()
    s.deleteLater()


def test_the_spinner_advances_while_running(app):
    s = Spinner()
    s.show()
    before = s._step
    s._advance()
    assert s._step != before
    s.deleteLater()


def test_the_spinner_paints_without_error(app):
    """An unended QPainter is its own crash, so exercise the paint path."""
    s = Spinner()
    s.resize(14, 14)
    s.show()
    app.processEvents()
    assert not s.grab().isNull()
    s.deleteLater()


# ---------------------------------------------------------------------------
# the busy chip
# ---------------------------------------------------------------------------


def test_the_chip_is_hidden_until_something_happens(app):
    chip = BusyChip()
    assert chip.isHidden()
    chip.deleteLater()


def test_a_delayed_begin_does_not_show_immediately(app):
    """The point of the delay: a warm that finishes fast never flashes a spinner
    at anyone."""
    chip = BusyChip()
    chip.begin("Fetching", delay_ms=400)
    assert chip.isHidden()
    chip.end()
    app.processEvents()
    assert chip.isHidden()
    chip.deleteLater()


def test_a_delayed_begin_shows_once_the_delay_elapses(app, qt_wait):
    chip = BusyChip()
    chip.show()
    chip.begin("Fetching card art · 0 of 40", delay_ms=30)
    qt_wait(140)
    assert not chip.isHidden()
    assert "0 of 40" in chip._label.text()
    chip.end()
    chip.deleteLater()


def test_end_hides_the_chip_and_stops_the_spinner(app):
    chip = BusyChip()
    chip.begin("Fetching", delay_ms=0)
    assert not chip.isHidden()
    chip.end()
    assert chip.isHidden()
    assert not chip._spinner._timer.isActive()
    chip.deleteLater()


def test_the_text_can_be_updated_while_running(app):
    chip = BusyChip()
    chip.begin("Fetching card art · 0 of 9", delay_ms=0)
    chip.update_text("Fetching card art · 5 of 9")
    assert "5 of 9" in chip._label.text()
    chip.end()
    chip.deleteLater()


# ---------------------------------------------------------------------------
# the warm, through the view
# ---------------------------------------------------------------------------


def test_creating_a_collection_starts_a_warm(view, conn, cache, monkeypatch):
    started: list[list] = []
    monkeypatch.setattr(
        "tcginventory.ui.views.collections.ArtWarmWorker.start",
        lambda self: started.append(self._targets),
    )
    coll = _seed(conn, n=10)
    view.refresh()
    assert view._warm_art(coll) == 10
    assert started and len(started[0]) == 10


def test_a_collection_with_nothing_to_fetch_starts_no_thread(
    view, conn, cache, monkeypatch
):
    """Most collections are already 100% cached; a warm must be free then."""
    started: list[int] = []
    monkeypatch.setattr(
        "tcginventory.ui.views.collections.ArtWarmWorker.start",
        lambda self: started.append(1),
    )
    coll = _seed(conn, n=6, with_urls=False)  # no URLs -> nothing fetchable
    view.refresh()
    assert view._warm_art(coll) == 0
    assert started == []


def test_a_small_warm_is_not_announced(view, conn, cache, monkeypatch):
    """Below the threshold the work finishes before a spinner would fade in."""
    monkeypatch.setattr(
        "tcginventory.ui.views.collections.ArtWarmWorker.start", lambda self: None
    )
    coll = _seed(conn, n=2)
    view.refresh()
    view._warm_art(coll)
    assert view._busy.isHidden()


def test_a_large_warm_is_announced_after_the_delay(
    view, conn, cache, monkeypatch, qt_wait
):
    monkeypatch.setattr(
        "tcginventory.ui.views.collections.ArtWarmWorker.start", lambda self: None
    )
    monkeypatch.setattr(view, "_WARM_REVEAL_MS", 20)
    coll = _seed(conn, n=25)
    view.refresh()
    view._open(coll)  # so the detail page (and the chip) is the visible one
    view._warm_art(coll)
    qt_wait(140)
    assert not view._busy.isHidden()
    assert "of 25" in view._busy._label.text()


def test_progress_updates_the_chip_text(view, conn, cache, monkeypatch):
    monkeypatch.setattr(
        "tcginventory.ui.views.collections.ArtWarmWorker.start", lambda self: None
    )
    coll = _seed(conn, n=25)
    view.refresh()
    view._warm_art(coll)
    view._on_warm_progress(7, 25)
    assert "7 of 25" in view._busy._label.text()


def test_finishing_clears_the_thumbnail_cache_so_new_art_appears(
    view, conn, cache, monkeypatch
):
    """card_thumbs caches "no art" as a decoded absence, so it has to be dropped
    once files land or the grid keeps showing the fallback."""
    cleared: list[int] = []
    monkeypatch.setattr(
        "tcginventory.ui.views.collections.card_thumbs.clear",
        lambda: cleared.append(1),
    )
    view._on_warm_finished(4, 0)
    assert cleared == [1]
    assert view._busy.isHidden()


def test_finishing_with_nothing_fetched_leaves_the_cache_alone(view, monkeypatch):
    cleared: list[int] = []
    monkeypatch.setattr(
        "tcginventory.ui.views.collections.card_thumbs.clear",
        lambda: cleared.append(1),
    )
    view._on_warm_finished(0, 3)
    assert cleared == []


def test_a_second_warm_retires_the_first_rather_than_dropping_it(
    view, conn, cache, monkeypatch
):
    """WORKER-RETIRE-NOT-REPLACE: overwriting a running QThread aborts the
    process, so the outgoing worker must be parked until it actually stops."""
    monkeypatch.setattr(
        "tcginventory.ui.views.collections.ArtWarmWorker.start", lambda self: None
    )
    retired: list[object] = []
    monkeypatch.setattr(
        "tcginventory.ui.views.collections.retire",
        lambda worker, parked: retired.append(worker),
    )
    coll = _seed(conn, n=10)
    view.refresh()
    view._warm_art(coll)
    first = view._warm
    view._warm_art(coll)
    assert retired and retired[-1] is first
    assert view._warm is not first
