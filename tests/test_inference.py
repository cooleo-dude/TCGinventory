"""Inference pipeline (Part 2): detect boxes + identify each crop with confidence."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from tcginventory.data.vision import CardLabel
from tcginventory.services import exemplars, inference
from tcginventory.services.recognizer import PixelEmbedder, Recognizer


def _scene(path):
    arr = np.random.default_rng(3).integers(0, 256, (80, 160, 3), dtype=np.uint8)
    Image.fromarray(arr, "RGB").save(path)
    return str(path)


def test_run_inference_identifies_confident_and_flags_low_confidence(repo, tmp_path):
    scene = _scene(tmp_path / "scene.png")
    emb = PixelEmbedder(24)
    rec = Recognizer(repo, embedder=emb)

    left_box = (0.0, 0.0, 0.5, 1.0)
    right_box = (0.5, 0.0, 0.5, 1.0)
    with Image.open(scene) as im:
        left_crop = exemplars.crop_box(im.convert("RGB"), left_box)
    vec = emb.embed(left_crop)
    cap = repo.add_capture()
    img = repo.add_image(cap, "s", scene, width=160, height=80)
    region_id = repo.add_region(img, 1, left_box, None, CardLabel(card_id="A"))
    repo.put_region_embedding(
        region_id, emb.version, "A", int(vec.shape[0]), vec.astype("float32").tobytes()
    )

    def resolve(card_id):
        name = "Alpha" if card_id == "A" else None
        return CardLabel(card_id=card_id, name=name)

    dets = inference.run_inference(
        scene,
        detect=lambda _p: [left_box, right_box],
        recognizer=rec,
        pool=repo.labeled_card_ids(),
        resolve=resolve,
        conf_threshold=0.9,
    )
    assert len(dets) == 2
    assert dets[0].label.card_id == "A" and dets[0].label.name == "Alpha"
    assert dets[0].score == pytest.approx(1.0, abs=1e-4) and dets[0].confident
    assert dets[1].score < 0.9 and not dets[1].confident  # only candidate, weak match


def test_run_inference_empty_pool_leaves_boxes_unidentified(repo, tmp_path):
    scene = _scene(tmp_path / "scene.png")
    dets = inference.run_inference(
        scene,
        detect=lambda _p: [(0.0, 0.0, 0.5, 1.0)],
        recognizer=Recognizer(repo, embedder=PixelEmbedder(24)),
        pool=[],  # nothing labeled yet
        resolve=lambda cid: CardLabel(card_id=cid),
    )
    assert len(dets) == 1
    assert dets[0].label is None and dets[0].score == 0.0 and not dets[0].confident
