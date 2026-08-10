"""Corpus write-back (Part 2 feedback): confirmed scenes become regions + exemplars."""

from __future__ import annotations

import numpy as np
from PIL import Image

from tcginventory.data.vision import CardLabel
from tcginventory.services import corpus
from tcginventory.services.recognizer import PixelEmbedder


def _scene(path):
    arr = np.random.default_rng(4).integers(0, 256, (80, 160, 3), dtype=np.uint8)
    Image.fromarray(arr, "RGB").save(path)
    return str(path)


def test_ingest_labeled_scene_writes_regions_and_exemplars(repo, tmp_path):
    scene = _scene(tmp_path / "scene.png")
    entries = [
        ((0.0, 0.0, 0.5, 1.0), CardLabel(card_id="A", name="Alpha"), None),
        ((0.5, 0.0, 0.5, 1.0), CardLabel(name="typed only"), None),  # no card_id
    ]
    result = corpus.ingest_labeled_scene(
        repo, PixelEmbedder(24), scene, "transaction", entries, vision_dir=tmp_path
    )

    assert result.regions == 2  # both boxes become detector training data
    assert result.exemplars == 1  # only the identified one gets an exemplar
    assert repo.region_count() == 2
    assert repo.region_embedding_count() == 1
    assert [r[0] for r in repo.region_vectors("pixel-24")] == ["A"]
    assert repo.image_count() == 1


def test_ingest_without_embedder_still_writes_regions(repo, tmp_path):
    scene = _scene(tmp_path / "scene.png")
    result = corpus.ingest_labeled_scene(
        repo,
        None,  # no matcher available
        scene,
        "auction",
        [((0.0, 0.0, 1.0, 1.0), CardLabel(card_id="A"), None)],
        vision_dir=tmp_path,
    )
    assert result.regions == 1 and result.exemplars == 0
    assert repo.region_embedding_count() == 0
