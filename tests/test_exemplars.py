"""Crop-exemplar indexing service (M-V2): index / reindex / export crops."""

from __future__ import annotations

import numpy as np
from PIL import Image

from tcginventory.data.vision import CardLabel
from tcginventory.services import exemplars
from tcginventory.services.recognizer import PixelEmbedder


def _scene_with_two_labeled_regions(repo, tmp_path):
    scene = tmp_path / "scene.png"
    arr = np.random.default_rng(1).integers(0, 256, (80, 120, 3), dtype=np.uint8)
    Image.fromarray(arr, "RGB").save(scene)
    cap = repo.add_capture()
    img = repo.add_image(cap, "sha", str(scene), width=120, height=80)
    repo.add_region(img, 1, (0.0, 0.0, 0.5, 1.0), None, CardLabel(card_id="A"))
    repo.add_region(img, 2, (0.5, 0.0, 0.5, 1.0), None, CardLabel(card_id="B"))
    repo.add_region(img, 3, (0.0, 0.0, 0.2, 0.2), None, CardLabel())  # unlabeled
    return scene


def test_reindex_all_embeds_labeled_regions_and_is_idempotent(repo, tmp_path):
    _scene_with_two_labeled_regions(repo, tmp_path)
    emb = PixelEmbedder(24)

    assert exemplars.reindex_all(repo, emb) == 2  # the unlabeled region is skipped
    assert repo.region_embedding_count() == 2
    assert exemplars.reindex_all(repo, emb) == 0  # skip-existing → no re-embed

    cards = sorted(row[0] for row in repo.region_vectors(emb.version))
    assert cards == ["A", "B"]


def test_missing_source_files_are_skipped(repo, tmp_path):
    cap = repo.add_capture()
    img = repo.add_image(cap, "sha", str(tmp_path / "gone.png"), width=64, height=64)
    repo.add_region(img, 1, (0, 0, 1, 1), None, CardLabel(card_id="A"))
    assert exemplars.reindex_all(repo, PixelEmbedder(24)) == 0


def test_export_identity_crops_groups_by_card(repo, tmp_path):
    _scene_with_two_labeled_regions(repo, tmp_path)
    out = tmp_path / "crops"
    assert exemplars.export_identity_crops(repo, out) == 2
    assert (out / "A").is_dir() and (out / "B").is_dir()
    assert len(list(out.rglob("*.png"))) == 2
