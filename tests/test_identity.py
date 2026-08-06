"""Card identification: the benchmark, and the bottom-band re-ranker (Layer 1).

Synthetic cards throughout, built so the answer is known: two printings share
artwork and differ only in the strip along the bottom, which is exactly the
distinction a whole-card embedding cannot make and this layer exists to restore.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image, ImageDraw

from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.data.vision import (
    KIND_IDENTITY,
    CardLabel,
    ModelRunRepository,
    VisionRepository,
    open_vision,
)
from tcginventory.domain.models import CatalogCard
from tcginventory.services import cardimages
from tcginventory.services import identity_eval as ie
from tcginventory.services import identity_patches as ip


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    conn = connect(":memory:")
    migrate(conn)
    repo = VisionRepository(open_vision(":memory:"))
    yield conn, repo, tmp_path
    conn.close()
    repo.conn.close()


def _card_image(number: str, *, art_seed: int = 7, size=(240, 336)) -> Image.Image:
    """A card: the same artwork for a given seed, with its number along the bottom."""
    rng = np.random.default_rng(art_seed)
    art = rng.integers(40, 210, size=(size[1], size[0], 3), dtype=np.uint8)
    img = Image.fromarray(art, "RGB")
    draw = ImageDraw.Draw(img)
    band_top = int(size[1] * 0.88)
    draw.rectangle((0, band_top, size[0], size[1]), fill=(250, 250, 250))
    draw.text((10, band_top + 6), number, fill=(0, 0, 0))
    # A blocky mark too, so the band differs at low resolution as well as in text.
    offset = 20 * (sum(ord(c) for c in number) % 6)
    draw.rectangle(
        (60 + offset, band_top + 4, 90 + offset, size[1] - 4), fill=(10, 10, 10)
    )
    return img


def _photograph(card: Image.Image, *, mat=(25, 110, 25)) -> Image.Image:
    """The card as a photo would show it: on a mat, under dimmer light.

    The mat is a saturated colour on purpose. A grey one risks matching the card's
    own pale bottom strip, and then the tightening step trims the strip off — which
    is a real failure mode, but one that belongs in its own test rather than
    silently poisoning every other one.
    """
    scene = Image.new("RGB", (card.width + 60, card.height + 60), mat)
    darker = Image.eval(card, lambda v: int(v * 0.82))
    scene.paste(darker, (30, 30))
    return scene


# -- the band ----------------------------------------------------------------


def test_the_band_matches_its_own_printing_over_a_reprint(env):
    """The point of the layer: same art, different number, and the band knows."""
    base = _card_image("4/102")
    reprint = _card_image("025/165")  # identical artwork, different printing

    query = ip.bottom_band(ip.canonical_front(_photograph(base)))
    own = ip.similarity(query, ip.bottom_band(ip.canonical_front(base)))
    other = ip.similarity(query, ip.bottom_band(ip.canonical_front(reprint)))
    assert own > other


def test_the_band_ignores_exposure(env):
    card = _card_image("12/108")
    bright = ip.bottom_band(ip.canonical_front(card))
    dim = ip.bottom_band(ip.canonical_front(Image.eval(card, lambda v: int(v * 0.6))))
    assert ip.similarity(bright, dim) > 0.9  # a darker photo is the same card


def test_the_band_survives_imperfect_framing(env):
    """The reason this uses phase correlation: tightening a photo is never exact,
    and plain correlation of a fixed crop loses the printing signal to a 2px slip."""
    card = _card_image("4/102")
    face = ip.canonical_front(card)
    reprint = ip.bottom_band(ip.canonical_front(_card_image("025/165")))

    width, height = face.size
    slipped = face.crop((6, 6, width - 6, height - 6)).resize(
        (width, height), Image.LANCZOS
    )
    query = ip.bottom_band(slipped)
    assert ip.similarity(query, ip.bottom_band(face)) > ip.similarity(query, reprint)


def test_a_band_is_a_zero_mean_unit_length_image(env):
    band = ip.bottom_band(ip.canonical_front(_card_image("1/1")))
    assert band.shape == (ip.BAND_H, ip.BAND_W)  # 2-D: phase correlation needs it
    assert abs(float(band.mean())) < 1e-6
    assert abs(float(np.linalg.norm(band)) - 1.0) < 1e-5


def test_similarity_is_bounded_and_self_matching(env):
    band = ip.bottom_band(ip.canonical_front(_card_image("9/99")))
    assert ip.similarity(band, band) == pytest.approx(1.0, abs=1e-3)
    assert ip.similarity(band, None) == 0.0  # nothing to compare with


def _install_art(card_id: str, image: Image.Image) -> None:
    path = cardimages.cache_path(card_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def test_rescore_promotes_the_right_printing(env):
    """The embedding prefers the wrong printing; the band puts it right."""
    _install_art("base-4", _card_image("4/102"))
    _install_art("reprint-25", _card_image("025/165"))

    query = _photograph(_card_image("4/102"))
    # The embedding is slightly wrong — the reprint scores higher.
    ranked = ip.rescore(query, [("reprint-25", 0.91), ("base-4", 0.90)])
    assert [s.card_id for s in ranked][0] == "base-4"
    assert ranked[0].band > ranked[1].band


def test_a_candidate_without_local_art_keeps_its_score(env):
    _install_art("known", _card_image("7/64"))
    query = _photograph(_card_image("7/64"))

    ranked = ip.rescore(query, [("known", 0.80), ("no-art", 0.85)])
    unscored = next(s for s in ranked if s.card_id == "no-art")
    assert unscored.band == 0.0
    assert unscored.total == 0.85  # unchanged, not penalised for being unmeasurable


def test_the_band_only_breaks_near_ties(env):
    """A confident embedding must survive a band that disagrees, or the layer
    would trade reprint errors for a new class of wrong-card errors."""
    _install_art("confident", _card_image("1/50", art_seed=11))
    _install_art("band-favoured", _card_image("2/50", art_seed=12))
    query = _photograph(_card_image("2/50", art_seed=12))  # band points elsewhere

    ranked = ip.rescore(query, [("confident", 0.95), ("band-favoured", 0.55)])
    assert ranked[0].card_id == "confident"  # a 0.40 gap is not a tie


def test_a_uniform_band_score_changes_nothing(env):
    """Centring at work: a query that agrees equally with everything leaves the
    embedding's ordering exactly as it found it."""
    art = _card_image("5/5")
    for cid in ("a", "b", "c"):
        _install_art(cid, art)  # identical art -> identical peaks

    ranked = ip.rescore(_photograph(art), [("a", 0.70), ("b", 0.90), ("c", 0.80)])
    assert [s.card_id for s in ranked] == ["b", "c", "a"]
    assert all(s.total == pytest.approx(s.embed) for s in ranked)


# -- wired into the recognizer -----------------------------------------------


class _Flat:
    """An encoder that cannot tell the two printings apart — the situation the
    band exists for. Every card scores identically, so any ordering the
    recognizer produces came from the strip."""

    version = "flat:v1"

    def embed(self, image):
        return np.ones(4, dtype=np.float32) / 2.0


def _art_path(card_id: str):
    path = cardimages.cache_path(card_id)
    return path if path.is_file() else None


def test_suggest_reranks_on_the_band(env):
    _, repo, tmp_path = env
    from tcginventory.services.recognizer import Recognizer

    _install_art("base-4", _card_image("4/102"))
    _install_art("reprint-25", _card_image("025/165"))
    query = tmp_path / "query.png"
    _photograph(_card_image("4/102")).save(query)

    rec = Recognizer(repo, embedder=_Flat(), reference_image=_art_path)
    ordered = rec.suggest(query, ["reprint-25", "base-4"], top=2)
    assert [s.card_id for s in ordered] == ["base-4", "reprint-25"]
    assert ordered[0].band > ordered[1].band

    plain = rec.suggest(query, ["reprint-25", "base-4"], top=2, rerank=False)
    assert [s.card_id for s in plain] == ["reprint-25", "base-4"]  # tie, input order
    assert plain[0].band == 0.0


def test_suggest_keeps_score_a_plain_cosine(env):
    """The UI thresholds on `score` to flag low confidence, so re-ranking must
    reorder without inflating it."""
    _, repo, tmp_path = env
    from tcginventory.services.recognizer import Recognizer

    _install_art("a", _card_image("1/10"))
    _install_art("b", _card_image("2/10"))
    query = tmp_path / "q.png"
    _photograph(_card_image("1/10")).save(query)

    rec = Recognizer(repo, embedder=_Flat(), reference_image=_art_path)
    for suggestion in rec.suggest(query, ["a", "b"], top=2):
        assert suggestion.score == pytest.approx(1.0)  # the flat encoder's cosine


# -- the benchmark -----------------------------------------------------------


# These exercise the benchmark's bookkeeping — ranks, the printing/card split,
# the rerank seam — so the ranking has to be something the test dictates rather
# than something it hopes a stand-in encoder produces. Cards are flat colours and
# the embedding is their hue, which makes "what ranks above what" exact.


def _solid(rgb: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (240, 336), rgb)


def _vector(image: Image.Image) -> np.ndarray:
    arr = np.asarray(image.convert("RGB"), dtype=np.float32).reshape(-1, 3).mean(axis=0)
    return (arr / (np.linalg.norm(arr) or 1.0)).astype(np.float32)


class _Embedder:
    version = "test:v1"

    def embed(self, image):
        if not hasattr(image, "size"):
            with Image.open(image) as im:
                return _vector(im)
        return _vector(image)


def _seed_pool(conn, repo, cards, version="test:v1"):
    """A catalog + index of (card_id, name, colour) triples."""
    CardCatalogRepository(conn).replace_for_language(
        [
            CatalogCard(card_id=cid, name=name, set_id="s", set_name="S", number="1")
            for cid, name, _rgb in cards
        ],
        "EN",
        "2026-08-05T00:00:00+00:00",
    )
    for cid, _name, rgb in cards:
        vec = _vector(_solid(rgb))
        repo.put_embedding(cid, version, vec.size, vec.tobytes())


def _label(repo, card_id: str, rgb: tuple[int, int, int], tmp_path, number: int = 1):
    """A labelled crop the benchmark will pick up, showing the given colour."""
    scene = tmp_path / f"scene-{card_id}-{number}.png"
    _solid(rgb).save(scene)
    cap = repo.add_capture(source="manual")
    img = repo.add_image(cap, f"sha{card_id}{number}", str(scene), "front")
    repo.add_region(img, number, (0.0, 0.0, 1.0, 1.0), None, CardLabel(card_id=card_id))


def _palette(n: int) -> list[tuple[str, str, tuple[int, int, int]]]:
    """n cards spread far apart in hue, so nearest-neighbour is unambiguous."""
    wheel = [
        (220, 40, 40),
        (40, 220, 40),
        (40, 40, 220),
        (220, 220, 40),
        (220, 40, 220),
    ]
    return [(f"c-{i}", f"Card {i}", wheel[i % len(wheel)]) for i in range(n)]


def test_benchmark_scores_queries_against_the_index(env):
    conn, repo, tmp_path = env
    cards = _palette(5)
    _seed_pool(conn, repo, cards)
    _label(repo, "c-2", cards[2][2], tmp_path)

    result = ie.evaluate(conn, repo, embedder=_Embedder())
    assert result.queries == 1 and result.pool == 5
    assert result.top1 == 1.0 and result.top5 == 1.0
    assert result.median_rank == 1.0 and result.mrr == 1.0


def test_benchmark_separates_a_wrong_printing_from_a_wrong_card(env):
    """The split that makes a layer's contribution attributable: patch
    verification fixes reprints, a better encoder fixes everything else."""
    conn, repo, tmp_path = env
    _seed_pool(
        conn,
        repo,
        [
            ("real", "Charizard", (220, 40, 40)),
            ("reprint", "Charizard", (200, 60, 40)),  # same name, near-identical art
            ("other", "Blastoise", (40, 40, 220)),
        ],
    )
    # The truth is the reprint, but the crop sits nearer the original: the top hit
    # is the same-named twin, which is a wrong *printing*, not a wrong card.
    _label(repo, "reprint", (218, 42, 40), tmp_path)

    result = ie.evaluate(conn, repo, embedder=_Embedder())
    assert result.top1 == 0.0
    assert result.wrong_printing == 1 and result.wrong_card == 0
    assert result.misses[0]["predicted"] == "real"
    assert result.misses[0]["same_name"] is True


def test_a_wrong_card_is_not_counted_as_a_wrong_printing(env):
    conn, repo, tmp_path = env
    _seed_pool(
        conn,
        repo,
        [("red", "Charizard", (220, 40, 40)), ("blue", "Blastoise", (40, 40, 220))],
    )
    _label(repo, "blue", (215, 45, 40), tmp_path)  # truth is blue, crop reads red

    result = ie.evaluate(conn, repo, embedder=_Embedder())
    assert result.wrong_card == 1 and result.wrong_printing == 0


def test_a_query_whose_answer_is_not_indexed_is_skipped(env):
    """Scoring a card the index has never seen would measure the index's
    coverage, not the identifier's accuracy."""
    conn, repo, tmp_path = env
    _seed_pool(conn, repo, _palette(3))
    _label(repo, "c-0", (220, 40, 40), tmp_path)
    _label(repo, "not-indexed", (40, 220, 40), tmp_path, number=2)

    result = ie.evaluate(conn, repo, embedder=_Embedder())
    assert result.queries == 1 and result.top1 == 1.0


def test_a_reranker_can_change_the_outcome(env):
    conn, repo, tmp_path = env
    cards = _palette(4)
    _seed_pool(conn, repo, cards)
    _label(repo, "c-1", cards[1][2], tmp_path)

    def reverse(query, order, ids, scores):  # deliberately terrible
        return order[::-1]

    good = ie.evaluate(conn, repo, embedder=_Embedder())
    bad = ie.evaluate(conn, repo, embedder=_Embedder(), rerank=reverse)
    assert good.top1 == 1.0
    assert bad.top1 == 0.0  # the hook really does drive the ranking


def test_a_benchmark_run_is_logged_like_a_model(env):
    conn, repo, tmp_path = env
    cards = _palette(3)
    _seed_pool(conn, repo, cards)
    _label(repo, "c-0", cards[0][2], tmp_path)

    result = ie.evaluate(conn, repo, embedder=_Embedder())
    runs = ModelRunRepository(repo.conn)
    run_id = ie.record(repo, result, runs)

    row = runs.get(run_id)
    assert row["kind"] == KIND_IDENTITY
    assert row["status"] == "ok" and row["source"] == "benchmark"
    assert row["map50"] == result.top1  # top-1 in the shared column
    payload = json.loads(row["metrics_json"])
    assert payload["pool"] == 3 and "wrong_printing" in payload


def test_an_empty_index_reports_nothing_rather_than_crashing(env):
    conn, repo, tmp_path = env
    result = ie.evaluate(conn, repo, embedder=_Embedder())
    assert result.queries == 0 and result.pool == 0 and result.top1 == 0.0
