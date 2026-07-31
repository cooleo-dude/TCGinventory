"""Identity matcher (M-V1a): embedder + reference cache + cosine-NN ranking."""

from __future__ import annotations

import numpy as np
import pytest

from tcginventory.data.vision import CardLabel, VisionRepository, open_vision
from tcginventory.services.recognizer import (
    PixelEmbedder,
    Recognizer,
    Suggestion,
    model_path,
)

try:
    import onnxruntime as _ort
except Exception:  # pragma: no cover
    _ort = None


@pytest.fixture()
def repo():
    conn = open_vision(":memory:")
    yield VisionRepository(conn)
    conn.close()


def _pattern_png(path, seed: int, size=64):
    """A deterministic, non-uniform grayscale image (same seed → same image)."""
    from PIL import Image

    arr = np.random.default_rng(seed).integers(0, 256, (size, size), dtype=np.uint8)
    Image.fromarray(arr, mode="L").save(str(path))
    return str(path)


def test_pixel_embedder_is_deterministic_and_normalised(tmp_path):
    emb = PixelEmbedder(size=32)
    a = emb.embed(_pattern_png(tmp_path / "g.png", 7))
    b = emb.embed(_pattern_png(tmp_path / "g2.png", 7))  # same seed → same image
    assert a.shape == (32 * 32,)
    assert (a == b).all()
    assert abs(float(np.linalg.norm(a)) - 1.0) < 1e-5  # unit-norm on real content


def test_embed_accepts_a_pil_image_like_a_path(tmp_path):
    from PIL import Image

    emb = PixelEmbedder(size=32)
    path = _pattern_png(tmp_path / "g.png", 3)
    from_path = emb.embed(path)
    with Image.open(path) as im:
        from_image = emb.embed(im.copy())  # in-memory crop, no temp file
    assert from_path.shape == from_image.shape
    assert np.allclose(from_path, from_image)


def test_suggest_ranks_the_matching_card_first(repo, tmp_path):
    refs = {
        "A": _pattern_png(tmp_path / "a.png", 1),
        "B": _pattern_png(tmp_path / "b.png", 2),
        "C": _pattern_png(tmp_path / "c.png", 3),
    }
    query = _pattern_png(tmp_path / "q.png", 2)  # identical to card B

    rec = Recognizer(repo, embedder=PixelEmbedder(32), reference_image=refs.get)
    out = rec.suggest(query, ["A", "B", "C"], top=3)

    assert isinstance(out[0], Suggestion)
    assert out[0].card_id == "B"  # best match ranks first
    assert out[0].score >= out[1].score >= out[2].score
    assert out[0].score == pytest.approx(1.0, abs=1e-4)  # identical image


def test_suggest_caches_reference_embeddings(repo, tmp_path):
    refs = {"A": _pattern_png(tmp_path / "a.png", 5)}
    rec = Recognizer(repo, embedder=PixelEmbedder(32), reference_image=refs.get)
    rec.suggest(_pattern_png(tmp_path / "q.png", 9), ["A"])
    assert repo.embedding_count() == 1
    assert repo.get_embedding("A", rec.model_version) is not None

    # A second call with the reference file now deleted still works (cache hit).
    import os

    os.remove(refs["A"])
    out = rec.suggest(_pattern_png(tmp_path / "q2.png", 9), ["A"])
    assert out and out[0].card_id == "A"


def test_suggest_skips_candidates_without_a_reference(repo, tmp_path):
    refs = {"A": _pattern_png(tmp_path / "a.png", 4)}  # B has no image
    rec = Recognizer(repo, embedder=PixelEmbedder(32), reference_image=refs.get)
    out = rec.suggest(_pattern_png(tmp_path / "q.png", 4), ["A", "B"])
    assert [s.card_id for s in out] == ["A"]


def test_suggest_uses_crop_exemplars_over_catalog_reference(repo, tmp_path):
    emb = PixelEmbedder(32)
    # Card B has a crop exemplar identical to the query; card A only has a
    # catalog reference. B should win on its exemplar even though A has a ref.
    cap = repo.add_capture()
    img = repo.add_image(cap, "sha", "/x.png", width=64, height=64)
    region_id = repo.add_region(img, 1, (0, 0, 1, 1), None, CardLabel(card_id="B"))
    vec_b = emb.embed(_pattern_png(tmp_path / "b.png", 2))
    repo.put_region_embedding(
        region_id,
        emb.version,
        "B",
        int(vec_b.shape[0]),
        vec_b.astype("float32").tobytes(),
    )
    refs = {"A": _pattern_png(tmp_path / "a.png", 1)}  # A: catalog reference only
    rec = Recognizer(repo, embedder=emb, reference_image=refs.get)

    out = rec.suggest(_pattern_png(tmp_path / "q.png", 2), ["A", "B"], top=2)
    assert out[0].card_id == "B"
    assert out[0].score == pytest.approx(1.0, abs=1e-4)  # identical to the exemplar


@pytest.mark.skipif(
    _ort is None or model_path() is None,
    reason="DINOv2 ONNX model / onnxruntime not available",
)
def test_dinov2_embedder_produces_normalised_384d_embeddings(tmp_path):
    from tcginventory.services.recognizer import OnnxEmbedder

    emb = OnnxEmbedder(model_path())
    a = emb.embed(_pattern_png(tmp_path / "a.png", 11))
    a2 = emb.embed(_pattern_png(tmp_path / "a2.png", 11))  # identical image
    assert emb.dim == 384
    assert a.shape == (384,)
    assert abs(float(np.linalg.norm(a)) - 1.0) < 1e-4
    assert float(np.dot(a, a2)) == pytest.approx(1.0, abs=1e-4)
