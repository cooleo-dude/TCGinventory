"""Background pre-fetch of a collection's missing card art (ticket 65).

Nothing here touches the network: ``ensure_cached`` is replaced, and the fetch
body is exercised directly rather than by starting a thread.
"""

from __future__ import annotations

import sqlite3

import pytest

from tcginventory.data.db import connect, now_iso
from tcginventory.data.repositories import CardCatalogRepository, CollectionRepository
from tcginventory.domain.models import CatalogCard, Collection
from tcginventory.services import cardimages
from tcginventory.services.collections import CollectionService, art_gap
from tcginventory.ui.views._art_warm import ArtWarmWorker


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    from tcginventory.data.migrator import migrate

    migrate(c)
    yield c
    c.close()


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    """Point the image cache at a temp dir so nothing reads the real 2 GB one."""
    monkeypatch.setattr(cardimages, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        cardimages, "cache_path", lambda card_id: tmp_path / f"{card_id}.png"
    )
    return tmp_path


def _slots(conn, cards):
    CardCatalogRepository(conn).replace_for_language(cards, "EN", now_iso())
    coll = CollectionRepository(conn).add(
        Collection(name="T", template="set", selector="S")
    )
    conn.commit()
    return CollectionService(conn).slots_for(coll)


# ---------------------------------------------------------------------------
# what counts as work (Qt-free)
# ---------------------------------------------------------------------------


def test_a_card_with_no_cached_art_but_a_url_is_fetchable(conn, cache):
    slots = _slots(
        conn,
        [
            CatalogCard(
                card_id="a-1",
                name="A",
                set_id="s",
                set_name="S",
                number="1",
                printed_total=1,
                image_small="http://x/a1.png",
            )
        ],
    )
    gap = art_gap(slots)
    assert gap.fetchable == [("a-1", "http://x/a1.png")]
    assert gap.no_source == 0


def test_a_card_with_no_url_is_not_work(conn, cache):
    """The large majority of missing art. Queueing it would retry a permanent
    gap on every open: a 5,805-slot rarity run had 224 missing, 220 with no URL."""
    slots = _slots(
        conn,
        [
            CatalogCard(
                card_id="a-1",
                name="A",
                set_id="s",
                set_name="S",
                number="1",
                printed_total=1,
                image_small=None,
            )
        ],
    )
    gap = art_gap(slots)
    assert gap.fetchable == []
    assert gap.no_source == 1
    assert len(gap) == 0


def test_an_already_cached_card_is_not_refetched(conn, cache):
    (cache / "a-1.png").write_bytes(b"not really a png")
    slots = _slots(
        conn,
        [
            CatalogCard(
                card_id="a-1",
                name="A",
                set_id="s",
                set_name="S",
                number="1",
                printed_total=1,
                image_small="http://x/a1.png",
            )
        ],
    )
    assert art_gap(slots).fetchable == []


def test_one_card_appearing_as_several_slots_is_fetched_once(conn, cache):
    """Master mode makes a card two slots. Fetching its art twice is waste."""
    from tcginventory.data.repositories import CardTreatmentRepository
    from tcginventory.services.collections import SLOT_MASTER

    CardCatalogRepository(conn).replace_for_language(
        [
            CatalogCard(
                card_id="a-1",
                name="A",
                set_id="s",
                set_name="S",
                number="1",
                printed_total=1,
                image_small="http://x/a1.png",
            )
        ],
        "EN",
        now_iso(),
    )
    CardTreatmentRepository(conn).replace_for_language(
        [("a-1", "Normal"), ("a-1", "Reverse Holofoil")], "EN"
    )
    coll = CollectionRepository(conn).add(
        Collection(name="T", template="set", selector="S", slot_mode=SLOT_MASTER)
    )
    conn.commit()
    slots = CollectionService(conn).slots_for(coll)
    assert len(slots) == 2
    assert len(art_gap(slots).fetchable) == 1


def test_art_gap_is_qt_free():
    """QT-FREE-CORE — this runs in the service layer."""
    import inspect

    from tcginventory.services import collections as mod

    assert "PySide6" not in inspect.getsource(mod)


# ---------------------------------------------------------------------------
# the fetch body
# ---------------------------------------------------------------------------


def test_the_worker_fetches_every_target(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        cardimages, "ensure_cached", lambda cid, url: calls.append(cid) or "path"
    )
    worker = ArtWarmWorker([(f"c-{i}", f"http://x/{i}.png") for i in range(9)])
    fetched, failed = worker._fetch_all()
    assert sorted(calls) == sorted(f"c-{i}" for i in range(9))
    assert (fetched, failed) == (9, 0)


def test_one_failing_image_does_not_stop_the_rest(monkeypatch):
    """A card whose art 404s must not cost the other 200."""

    def flaky(card_id, url):
        if card_id == "c-3":
            raise RuntimeError("404")
        return "path"

    monkeypatch.setattr(cardimages, "ensure_cached", flaky)
    worker = ArtWarmWorker([(f"c-{i}", f"http://x/{i}.png") for i in range(6)])
    fetched, failed = worker._fetch_all()
    assert (fetched, failed) == (5, 1)


def test_a_falsy_result_counts_as_a_miss_not_a_crash(monkeypatch):
    monkeypatch.setattr(cardimages, "ensure_cached", lambda cid, url: None)
    worker = ArtWarmWorker([("c-1", "http://x/1.png")])
    assert worker._fetch_all() == (0, 1)


def test_progress_is_reported_once_per_image(monkeypatch):
    monkeypatch.setattr(cardimages, "ensure_cached", lambda cid, url: "path")
    worker = ArtWarmWorker([(f"c-{i}", f"http://x/{i}.png") for i in range(5)])
    seen: list[tuple[int, int]] = []
    worker.progress.connect(lambda d, t: seen.append((d, t)))
    worker._fetch_all()
    assert [d for d, _ in seen] == [1, 2, 3, 4, 5]
    assert {t for _, t in seen} == {5}


def test_an_empty_target_list_does_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(
        cardimages, "ensure_cached", lambda cid, url: called.append(cid)
    )
    assert ArtWarmWorker([])._fetch_all() == (0, 0)
    assert called == []


def test_an_interrupted_warm_stops_early(monkeypatch):
    """Leaving the screen must not leave a thread downloading for a minute."""
    monkeypatch.setattr(cardimages, "ensure_cached", lambda cid, url: "path")
    worker = ArtWarmWorker([(f"c-{i}", f"http://x/{i}.png") for i in range(50)])
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: True)
    assert worker._fetch_all() == (0, 0)
