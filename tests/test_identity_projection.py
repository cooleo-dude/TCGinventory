"""Carrying a photograph into the catalog's render domain (identity Layer 3).

The map is fitted from synthetic pairs, so these tests build a world where the
right answer is known outright: a fixed rotation between the two domains, which
the fit must recover.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from tcginventory.services import cardimages
from tcginventory.services import identity_projection as ip


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))


def _art(card_id: str, rgb) -> None:
    path = cardimages.cache_path(card_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (120, 168), rgb).save(path)


def _rotation(dim: int, seed: int = 3) -> np.ndarray:
    """A known orthogonal matrix to hide behind the two domains."""
    q, _ = np.linalg.qr(np.random.default_rng(seed).normal(size=(dim, dim)))
    return q.astype(np.float32)


class _ColourEmbedder:
    """Embeds a card by its hue, so a card has one identity across augmentations."""

    version = "test:v1"

    def embed(self, image):
        arr = np.asarray(image.convert("RGB"), dtype=np.float32)
        vec = arr.reshape(-1, 3).mean(axis=0)
        norm = np.linalg.norm(vec) or 1.0
        return (vec / norm).astype(np.float32)


# -- the augmentation --------------------------------------------------------


def test_augmentation_changes_the_picture_without_destroying_it(tmp_path):
    rng = np.random.default_rng(0)
    card = Image.new("RGB", (120, 168), (200, 60, 60))
    out = ip.photo_like(card, rng)

    assert out.size[0] > card.size[0]  # sits on a mat, as a drawn box would
    embedder = _ColourEmbedder()
    # Still recognisably the same card: hue survives the degradation.
    assert float(embedder.embed(out) @ embedder.embed(card)) > 0.9


def test_augmentation_is_reproducible_from_its_seed(tmp_path):
    card = Image.new("RGB", (120, 168), (60, 160, 220))
    first = ip.photo_like(card, np.random.default_rng(11))
    second = ip.photo_like(card, np.random.default_rng(11))
    assert np.array_equal(np.asarray(first), np.asarray(second))


def test_two_augmentations_of_one_card_differ(tmp_path):
    card = Image.new("RGB", (120, 168), (60, 160, 220))
    rng = np.random.default_rng(5)
    a, b = ip.photo_like(card, rng), ip.photo_like(card, rng)
    assert a.size != b.size or not np.array_equal(np.asarray(a), np.asarray(b))


# -- the fit -----------------------------------------------------------------


def _world(n_cards=40):
    """``n_cards`` colour cards, with render vectors a known rotation away."""
    rng = np.random.default_rng(1)
    embedder = _ColourEmbedder()
    truth = _rotation(3)
    card_ids, targets = [], {}
    for i in range(n_cards):
        cid = f"c-{i}"
        colour = tuple(int(v) for v in rng.integers(40, 250, size=3))
        _art(cid, colour)
        card_ids.append(cid)
        with Image.open(cardimages.cache_path(cid)) as im:
            targets[cid] = (embedder.embed(im.convert("RGB")) @ truth).astype(
                np.float32
            )
    return embedder, card_ids, targets, truth


def test_the_fit_recovers_the_rotation_between_the_domains(monkeypatch):
    monkeypatch.setattr(ip, "MIN_PAIRS", 10)
    embedder, card_ids, targets, truth = _world()

    x, y, cards = ip.synthetic_pairs(None, embedder, card_ids, targets, augmentations=2)
    rotation, result = ip.fit_rotation((x, y), cards=cards)

    assert rotation is not None
    assert result.cards == len(card_ids) and result.pairs == 2 * len(card_ids)
    # It maps the photo domain onto the render domain, which is what matters —
    # the augmentation adds noise, so agreement is high rather than exact.
    assert result.residual > 0.9
    probe = embedder.embed(Image.new("RGB", (120, 168), (210, 90, 40)))
    assert float(ip.apply(probe, rotation) @ ip.apply(probe, truth)) > 0.9


def test_a_rotation_stays_a_rotation(monkeypatch):
    """The constraint that keeps the fit safe: it cannot rescale or collapse
    directions, so it can't quietly destroy the pool's separability."""
    monkeypatch.setattr(ip, "MIN_PAIRS", 10)
    embedder, card_ids, targets, _ = _world()

    x, y, cards = ip.synthetic_pairs(None, embedder, card_ids, targets)
    rotation, _ = ip.fit_rotation((x, y), cards=cards)
    identity = np.eye(rotation.shape[0], dtype=np.float32)
    assert np.allclose(rotation @ rotation.T, identity, atol=1e-4)


def test_too_few_pairs_produces_no_map_at_all(monkeypatch):
    """Under-fitting costs more top-5 than it returns in top-1, so a thin fit is
    refused rather than shipped."""
    embedder, card_ids, targets, _ = _world(n_cards=5)

    x, y, cards = ip.synthetic_pairs(None, embedder, card_ids, targets)

    assert x is None and y is None  # nothing to ship
    assert cards == 5


def test_cards_without_art_are_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(ip, "MIN_PAIRS", 10)
    embedder, card_ids, targets, _ = _world()
    card_ids = card_ids + ["never-downloaded"]

    x, _y, cards = ip.synthetic_pairs(None, embedder, card_ids, targets)
    assert x is not None
    assert cards == len(card_ids) - 1


def test_a_stop_request_ends_the_fit(monkeypatch):
    monkeypatch.setattr(ip, "MIN_PAIRS", 10)
    embedder, card_ids, targets, _ = _world()
    seen: list[int] = []

    def stop():
        seen.append(1)
        return len(seen) > 5

    _x, _y, cards = ip.synthetic_pairs(
        None, embedder, card_ids, targets, should_stop=stop
    )
    assert cards <= 6  # stopped early, and did not raise


# -- the artifact ------------------------------------------------------------


def test_the_map_round_trips_through_disk():
    rotation = _rotation(8)
    ip.save(rotation)
    loaded = ip.load()
    assert loaded is not None and np.allclose(loaded, rotation, atol=1e-6)


def test_no_map_installed_reads_as_none():
    assert ip.load() is None


def test_a_malformed_artifact_is_ignored_rather_than_crashing():
    path = ip.matrix_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, np.zeros((4, 9), dtype=np.float32))  # not square
    assert ip.load() is None


# -- applying it -------------------------------------------------------------


def test_applying_the_map_keeps_the_vector_on_the_unit_sphere():
    rotation = _rotation(16)
    vec = np.random.default_rng(2).normal(size=16).astype(np.float32)
    vec /= np.linalg.norm(vec)
    out = ip.apply(vec, rotation)
    assert abs(float(np.linalg.norm(out)) - 1.0) < 1e-5


def test_a_map_for_a_different_encoder_is_ignored():
    """A stale artifact must not silently score a query in the wrong space."""
    vec = np.ones(16, dtype=np.float32) / 4.0
    assert np.array_equal(ip.apply(vec, _rotation(8)), vec)
    assert np.array_equal(ip.apply(vec, None), vec)


# -- learning from the labelled corpus ---------------------------------------


def _corpus_world(tmp_path):
    """A vision repo holding one labelled front and one labelled back."""
    from tcginventory.data.vision import CardLabel, VisionRepository, open_vision

    repo = VisionRepository(open_vision(":memory:"))
    embedder = _ColourEmbedder()
    targets = {}
    scene = tmp_path / "scene.png"
    Image.new("RGB", (100, 140), (200, 60, 60)).save(scene)

    cap = repo.add_capture(source="manual")
    for side, card_id, colour in (
        ("front", "front-card", (210, 70, 60)),
        ("back", "back-card", (40, 60, 210)),
    ):
        img = repo.add_image(cap, f"sha-{side}", str(scene), side)
        region = repo.add_region(
            img, 1, (0.0, 0.0, 1.0, 1.0), None, CardLabel(card_id=card_id)
        )
        vec = np.asarray(embedder.embed(Image.new("RGB", (10, 10), colour)))
        repo.put_region_embedding(region, "test:v1", card_id, vec.size, vec.tobytes())
        targets[card_id] = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    return repo, targets


def test_corpus_pairs_come_from_the_cards_you_labelled(tmp_path):
    repo, targets = _corpus_world(tmp_path)
    x, y, captures = ip.corpus_pairs(repo, "test:v1", targets)

    assert len(x) == 1 and len(y) == 1  # the front only
    assert len(set(captures)) == 1
    repo.conn.close()


def test_card_backs_are_never_used_as_pairs(tmp_path):
    """Every card back looks the same, so pairing one with a particular card's
    front art would teach the map that a generic back means that card."""
    repo, targets = _corpus_world(tmp_path)
    x, _y, _caps = ip.corpus_pairs(repo, "test:v1", targets)

    assert len(x) == 1, "the back leaked into the training pairs"
    repo.conn.close()


def test_a_labelled_card_missing_from_the_index_is_skipped(tmp_path):
    repo, _targets = _corpus_world(tmp_path)
    x, _y, _caps = ip.corpus_pairs(repo, "test:v1", {})  # nothing indexed

    assert len(x) == 0  # nothing to aim at, so no pair
    repo.conn.close()


def test_corpus_pairs_pull_the_fit_toward_themselves(monkeypatch):
    """The whole point of weighting: a handful of real pairs must actually move
    the answer, or adding them was cosmetic."""
    monkeypatch.setattr(ip, "MIN_PAIRS", 10)
    embedder, card_ids, targets, _ = _world()
    x, y, cards = ip.synthetic_pairs(None, embedder, card_ids, targets)

    # A real pair that disagrees with the synthetic consensus.
    real_x = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
    real_y = np.array([[0.0, 1.0, 0.0]], dtype=np.float32)

    plain, _ = ip.fit_rotation((x, y), cards=cards)
    pulled, result = ip.fit_rotation(
        (x, y), (real_x, real_y), weight=500.0, cards=cards
    )

    assert result.corpus_pairs == 1
    before = float(ip.apply(real_x[0], plain) @ real_y[0])
    after = float(ip.apply(real_x[0], pulled) @ real_y[0])
    assert after > before  # the real pair is better served than it was


def test_no_corpus_pairs_leaves_the_synthetic_fit_alone(monkeypatch):
    monkeypatch.setattr(ip, "MIN_PAIRS", 10)
    embedder, card_ids, targets, _ = _world()
    x, y, cards = ip.synthetic_pairs(None, embedder, card_ids, targets)

    plain, _ = ip.fit_rotation((x, y), cards=cards)
    empty = np.zeros((0, 3), dtype=np.float32)
    same, result = ip.fit_rotation((x, y), (empty, empty), cards=cards)

    assert result.corpus_pairs == 0
    assert np.allclose(plain, same, atol=1e-6)


# -- reported like the models it sits beside ---------------------------------


def test_the_tile_reports_never_before_anything_is_fitted(monkeypatch):
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.services import operations as ops

    conn = connect(":memory:")
    migrate(conn)
    fit_op = ops.BY_ID["identity.fit_projection"]
    assert ops.status_for(conn, fit_op).state == ops.NEVER

    # And there is nothing to remove, so that tile offers nothing to do.
    forget = ops.status_for(conn, ops.BY_ID["identity.forget_projection"])
    assert forget.actionable is False
    conn.close()


def test_the_tile_quotes_held_out_accuracy_once_fitted(monkeypatch, tmp_path):
    """The point of the rework: it states what it bought, like the trained
    models do, and the figure is the held-out one."""
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.data.vision import KIND_IDENTITY, ModelRunRepository, open_vision
    from tcginventory.services import operations as ops

    conn = connect(":memory:")
    migrate(conn)
    ip.save(_rotation(8))

    vision = open_vision()
    runs = ModelRunRepository(vision)
    run_id = runs.start(kind=KIND_IDENTITY, source="projection")
    runs.finish(
        run_id,
        status="ok",
        map50=0.67,
        map50_95=0.83,
        precision=0.21,
        recall=0.76,
        notes="held out 63 crops",
    )
    monkeypatch.setattr(ops, "_open_vision", lambda: vision)

    status = ops.status_for(conn, ops.BY_ID["identity.fit_projection"])
    assert "21%" in status.value and "67%" in status.value  # before and after
    vision.close()
    conn.close()


def test_removing_the_map_puts_ranking_back_on_the_raw_model(tmp_path):
    from tcginventory.services.modeljobs import forget_photo_projection

    assert forget_photo_projection(None) == "no photo matching was fitted"
    ip.save(_rotation(8))
    assert ip.load() is not None
    assert "removed" in forget_photo_projection(None)
    assert ip.load() is None  # and suggestions fall back to the encoder
