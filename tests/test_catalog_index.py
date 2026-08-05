"""Indexing the catalog for recognition: scopes, resumability, reclaim.

Synthetic art in a temporary data directory throughout — no network, and the real
catalog is never touched.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.data.repositories import CardCatalogRepository, ItemRepository
from tcginventory.data.vision import CardLabel, VisionRepository, open_vision
from tcginventory.domain.models import CatalogCard
from tcginventory.services import cardimages
from tcginventory.services import catalog_index as ci


class _Embedder:
    """A stand-in that hashes pixels — deterministic, and needs no ONNX."""

    version = "test:v1"

    def embed(self, image):
        with Image.open(image) if not hasattr(image, "size") else image as im:
            arr = np.asarray(im.convert("L").resize((8, 8)), dtype=np.float32).ravel()
        norm = np.linalg.norm(arr) or 1.0
        return (arr / norm).astype(np.float32)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    conn = connect(":memory:")
    migrate(conn)
    repo = VisionRepository(open_vision(":memory:"))
    yield conn, repo
    conn.close()
    repo.conn.close()


def _catalog(conn, cards):
    CardCatalogRepository(conn).replace_for_language(
        [
            CatalogCard(
                card_id=cid,
                name=name,
                set_id="s1",
                set_name=set_name,
                number=str(i + 1),
                image_small=f"https://example.invalid/{cid}.png",
            )
            for i, (cid, name, set_name) in enumerate(cards)
        ],
        "EN",
        "2026-08-05T00:00:00+00:00",
    )


def _cache_art(card_id, shade=120):
    path = cardimages.cache_path(card_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 84), color=(shade, shade, 200)).save(path)
    return path


# -- scopes ------------------------------------------------------------------


def test_scopes_select_different_slices(env):
    conn, _repo = env
    _catalog(
        conn,
        [
            ("a-1", "Alpha", "Owned Set"),
            ("b-1", "Beta", "Other Set"),
            ("c-1", "Gamma", "Other Set"),
        ],
    )
    from conftest import make_item

    ItemRepository(conn).add(make_item("Alpha", set_name="Owned Set"))
    _cache_art("b-1")

    assert [c for c, _ in ci.scope_card_ids(conn, ci.SCOPE_ALL)] == [
        "a-1",
        "b-1",
        "c-1",
    ]
    assert [c for c, _ in ci.scope_card_ids(conn, ci.SCOPE_OWNED)] == ["a-1"]
    # "cached" is the no-network scope: only art already on disk.
    assert [c for c, _ in ci.scope_card_ids(conn, ci.SCOPE_CACHED)] == ["b-1"]


# -- the sweep ---------------------------------------------------------------


def test_indexing_keeps_the_art_in_one_place(env):
    """The downloaded PNG *is* the archive. Re-encoding it would have cost more
    disk in duplicates than the format saved, so nothing is copied."""
    conn, repo = env
    _catalog(conn, [("a-1", "Alpha", "S"), ("a-2", "Beta", "S")])
    before = _cache_art("a-1", shade=90).read_bytes()
    _cache_art("a-2", shade=200)

    emb = _Embedder()
    summary = ci.index_catalog(
        conn, repo, ci.SCOPE_CACHED, embedder=emb, allow_download=False
    )
    assert summary.embedded == 2 and summary.failed == 0
    assert repo.get_embedding("a-1", emb.version) is not None

    # Same file, untouched, and no second copy anywhere.
    assert ci.art_path("a-1") == cardimages.cache_path("a-1")
    assert ci.art_path("a-1").read_bytes() == before
    assert len(list(cardimages.cache_dir().glob("*"))) == 2

    # And a re-embed can read it back without the network — the point of keeping it.
    assert ci.source_for("a-1") is not None


def test_a_second_run_skips_what_is_already_indexed(env):
    conn, repo = env
    _catalog(conn, [("a-1", "Alpha", "S")])
    _cache_art("a-1")
    emb = _Embedder()

    first = ci.index_catalog(
        conn, repo, ci.SCOPE_CACHED, embedder=emb, allow_download=False
    )
    second = ci.index_catalog(
        conn, repo, ci.SCOPE_CACHED, embedder=emb, allow_download=False
    )
    assert first.embedded == 1 and second.embedded == 0
    assert second.already_indexed == 1


def test_a_new_model_version_reindexes_without_disturbing_the_old(env):
    """Vectors are keyed by model version, so a retrained identifier adds a fresh
    index rather than corrupting the one in use."""
    conn, repo = env
    _catalog(conn, [("a-1", "Alpha", "S")])
    _cache_art("a-1")

    ci.index_catalog(
        conn, repo, ci.SCOPE_CACHED, embedder=_Embedder(), allow_download=False
    )

    class Newer(_Embedder):
        version = "test:v2"

    ci.index_catalog(
        conn, repo, ci.SCOPE_CACHED, embedder=Newer(), allow_download=False
    )
    assert repo.get_embedding("a-1", "test:v1") is not None
    assert repo.get_embedding("a-1", "test:v2") is not None


def test_a_stop_request_ends_the_sweep_cleanly(env):
    conn, repo = env
    _catalog(conn, [(f"a-{i}", f"Card {i}", "S") for i in range(6)])
    for i in range(6):
        _cache_art(f"a-{i}", shade=100 + i * 10)

    seen: list[int] = []
    summary = ci.index_catalog(
        conn,
        repo,
        ci.SCOPE_CACHED,
        embedder=_Embedder(),
        allow_download=False,
        on_progress=lambda p: seen.append(p.done),
        should_stop=lambda: len(seen) >= 3,
    )
    assert summary.stopped and summary.embedded == 3
    # What it did get through is kept — the sweep resumes rather than restarting.
    assert repo.embedding_count() == 3


def test_a_broken_image_does_not_end_the_sweep(env):
    conn, repo = env
    _catalog(conn, [("good", "Good", "S"), ("bad", "Bad", "S")])
    _cache_art("good")
    broken = cardimages.cache_path("bad")
    broken.write_bytes(b"not an image")

    summary = ci.index_catalog(
        conn, repo, ci.SCOPE_CACHED, embedder=_Embedder(), allow_download=False
    )
    assert summary.embedded == 1 and summary.failed == 1
    assert summary.errors and "bad" in summary.errors[0]


# -- reclaim -----------------------------------------------------------------


def test_reclaim_frees_only_what_the_sweep_fetched(env):
    """The bargain: a sweep can be undone without losing the art you had before it,
    the cards you labelled, or the cards you own."""
    conn, repo = env
    _catalog(
        conn,
        [
            ("had-before", "H", "S"),
            ("labelled", "L", "S"),
            ("owned", "O", "Owned Set"),
            ("swept", "W", "S"),
        ],
    )

    # What you had before any sweep ran.
    _cache_art("had-before")
    ci.take_snapshot()

    # What a sweep then brought in.
    for cid in ("labelled", "owned", "swept"):
        _cache_art(cid)
    ci.index_catalog(
        conn, repo, ci.SCOPE_CACHED, embedder=_Embedder(), allow_download=False
    )

    cap = repo.add_capture(source="manual")
    img = repo.add_image(cap, "sha", "/scene.png", "front")
    repo.add_region(img, 1, (0, 0, 1, 1), None, CardLabel(card_id="labelled"))
    from conftest import make_item

    ItemRepository(conn).add(make_item("O", set_name="Owned Set"))

    result = ci.reclaim(conn, repo)
    assert result.removed == 1 and result.kept == 3
    assert result.bytes_freed > 0
    assert not ci.art_path("swept").exists()  # only the sweep's own download goes
    for cid in ("had-before", "labelled", "owned"):
        assert ci.art_path(cid).exists(), cid
    # Vectors are never touched: they are the part that makes recognition work.
    assert repo.embedding_count() == 4


def test_reclaim_can_be_previewed(env):
    conn, repo = env
    _catalog(conn, [("kept", "K", "S"), ("swept", "W", "S")])
    _cache_art("kept")
    ci.take_snapshot()
    _cache_art("swept")
    ci.index_catalog(
        conn, repo, ci.SCOPE_CACHED, embedder=_Embedder(), allow_download=False
    )

    preview = ci.reclaim(conn, repo, dry_run=True)
    assert preview.removed == 1
    assert ci.art_path("swept").exists()  # nothing actually deleted


def test_the_snapshot_is_taken_once_and_survives_later_sweeps(env):
    """After a sweep everything looks "already downloaded", so the record of what
    predated it has to be made before the first fetch and never overwritten."""
    conn, repo = env
    _catalog(conn, [("first", "F", "S"), ("later", "L", "S")])
    _cache_art("first")

    ci.index_catalog(
        conn, repo, ci.SCOPE_CACHED, embedder=_Embedder(), allow_download=False
    )
    assert ci.snapshot_ids() == {"first"}

    _cache_art("later")
    ci.index_catalog(
        conn, repo, ci.SCOPE_CACHED, embedder=_Embedder(), allow_download=False
    )
    assert ci.snapshot_ids() == {"first"}  # the second sweep does not widen it


# -- status ------------------------------------------------------------------


def test_status_reports_how_much_of_the_catalog_is_searchable(env):
    conn, repo = env
    _catalog(conn, [(f"a-{i}", f"C{i}", "S") for i in range(4)])
    _cache_art("a-0")
    _cache_art("a-1")
    emb = _Embedder()
    ci.index_catalog(conn, repo, ci.SCOPE_CACHED, embedder=emb, allow_download=False)

    status = ci.index_status(conn, repo, embedder=emb)
    assert status.catalog == 4
    assert status.indexed == 2
    assert status.archived == 2 and status.archive_bytes > 0  # the PNG store
    assert not status.complete
