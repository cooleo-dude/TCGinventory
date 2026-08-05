"""The condition grader: features, the ordinal fit, and the training job.

All synthetic — a card is a dark rectangle on a light mat, and "wear" is white
pixels painted into its corners — so the tests are fast, deterministic, and
independent of anyone's photographs.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image, ImageDraw

from tcginventory.data.vision import (
    KIND_CONDITION,
    CardLabel,
    ModelRunRepository,
    VisionRepository,
    open_vision,
)
from tcginventory.services import condition_dataset as cds
from tcginventory.services import condition_features as cf
from tcginventory.services import condition_train as ct


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    conn = open_vision(":memory:")
    yield VisionRepository(conn)
    conn.close()


def _card_photo(path, *, wear: float = 0.0, mat: int = 210, card: int = 40) -> None:
    """A card on a mat, with ``wear`` painting white into its corners and edges."""
    img = Image.new("RGB", (400, 300), color=(mat, mat, mat))
    draw = ImageDraw.Draw(img)
    box = (60, 30, 220, 254)  # a 160x224 card: 1.4 aspect, like the real thing
    draw.rectangle(box, fill=(card, card, card + 40))  # bluish, like a card back
    if wear > 0:
        span = int(30 * wear)
        for cx, cy in (
            (box[0], box[1]),
            (box[2] - span, box[1]),
            (box[0], box[3] - span),
        ):
            draw.rectangle((cx, cy, cx + span, cy + span), fill=(245, 245, 245))
    img.save(path)


def _graded_pair(
    repo, tmp_path, grade: str, *, wear: float, capture: int | None = None
):
    """One card photographed front and back, both faces carrying ``grade``."""
    index = len(list(repo.conn.execute("SELECT 1 FROM image")))
    cap = repo.add_capture(source="manual") if capture is None else capture
    front_path = tmp_path / f"front{index}.png"
    back_path = tmp_path / f"back{index}.png"
    _card_photo(front_path, wear=wear * 0.5)  # fronts show wear less plainly
    _card_photo(back_path, wear=wear)
    pair = 100 + index
    front = repo.add_image(cap, f"shaF{index}", str(front_path), "front", pair)
    back = repo.add_image(cap, f"shaB{index}", str(back_path), "back", pair)
    label = CardLabel(card_id=f"c{index}", name=f"Card {index}", condition=grade)
    repo.add_region(front, 1, (0.15, 0.1, 0.4, 0.75), None, label)
    repo.add_region(back, 1, (0.15, 0.1, 0.4, 0.75), None, label)
    return cap


# -- features ----------------------------------------------------------------


def test_feature_vector_has_a_slot_per_face_and_flags_what_is_missing(tmp_path):
    _card_photo(tmp_path / "c.png")
    face = cds.canonical_face(tmp_path / "c.png", (0.15, 0.1, 0.4, 0.75))

    both = cf.sample_vector({"front": face, "back": face})
    assert len(both) == len(cf.FEATURE_NAMES)
    named = cf.describe(both)
    assert named["has_front"] == 1.0 and named["has_back"] == 1.0

    back_only = cf.describe(cf.sample_vector({"back": face}))
    assert back_only["has_front"] == 0.0 and back_only["has_back"] == 1.0
    # A missing face reads as absent, not as a pristine one.
    assert back_only["front_corner_white_max"] == 0.0


def test_whitened_corners_measure_as_more_wear(tmp_path):
    clean_path, worn_path = tmp_path / "clean.png", tmp_path / "worn.png"
    _card_photo(clean_path, wear=0.0)
    _card_photo(worn_path, wear=1.0)
    box = (0.15, 0.1, 0.4, 0.75)

    clean = cf.face_vector(cds.canonical_face(clean_path, box))
    worn = cf.face_vector(cds.canonical_face(worn_path, box))
    assert worn["corner_white_max"] > clean["corner_white_max"]


def test_tightening_removes_the_mat(tmp_path):
    """The normalisation the old heuristic lacked: measurements must read the card."""
    path = tmp_path / "c.png"
    _card_photo(path)
    from tcginventory.services.exemplars import crop_region

    loose = crop_region(path, (0.05, 0.02, 0.6, 0.95))  # lots of mat included
    tight = cds.tighten_to_card(loose)
    assert tight.size[0] < loose.size[0] and tight.size[1] < loose.size[1]
    # What's left is the dark card, not the light mat.
    assert np.asarray(tight.convert("L")).mean() < np.asarray(loose.convert("L")).mean()


def test_exposure_flattening_makes_two_lightings_comparable(tmp_path):
    bright, dim = tmp_path / "b.png", tmp_path / "d.png"
    _card_photo(bright, mat=230, card=90)
    _card_photo(dim, mat=120, card=30)
    box = (0.15, 0.1, 0.4, 0.75)

    a = np.asarray(cds.canonical_face(bright, box).convert("L")).mean()
    b = np.asarray(cds.canonical_face(dim, box).convert("L")).mean()
    assert abs(a - b) < 40  # the same card under two lights, brought together


# -- the fit -----------------------------------------------------------------


def test_ridge_recovers_a_linear_relationship():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(60, 3))
    y = 2.0 * x[:, 0] - 1.0 * x[:, 1] + 0.5
    fit = ct.fit_ridge(x, y, alpha=0.01)
    assert np.allclose(fit.predict(x), y, atol=0.2)


def test_grades_are_clipped_to_the_scale():
    assert list(ct.to_grades(np.array([-3.0, 0.4, 1.6, 99.0]))) == [0, 0, 2, 4]


def test_leave_one_capture_out_does_not_let_a_photo_grade_itself():
    """Two captures, each internally consistent but contradictory: a model that
    only memorised its own photo must score badly here."""
    matrix = np.array([[0.0], [0.0], [1.0], [1.0]], dtype=float)
    target = np.array([0.0, 0.0, 4.0, 4.0])
    captures = np.array([1, 1, 2, 2])
    metrics = ct.evaluate_leave_one_capture_out(matrix, target, captures, alpha=0.1)
    assert metrics.samples == 4
    assert metrics.accuracy < 1.0  # held-out folds can't see their own answer
    assert metrics.baseline_distance == pytest.approx(2.0)


def test_metrics_report_ordinal_distance_and_a_confusion_table():
    matrix = np.arange(10, dtype=float).reshape(-1, 1)
    target = np.clip(np.arange(10) // 2, 0, 4).astype(float)
    captures = np.array([1, 1, 2, 2, 3, 3, 4, 4, 5, 5])
    metrics = ct.evaluate_leave_one_capture_out(matrix, target, captures)
    assert 0.0 <= metrics.mean_distance <= 4.0
    assert 0.0 <= metrics.within_one <= 1.0
    assert len(metrics.confusion) == len(cds.GRADES)
    assert sum(sum(row) for row in metrics.confusion) == 10


# -- the job ----------------------------------------------------------------


def test_training_refuses_a_thin_corpus_without_logging_a_run(repo, tmp_path):
    runs = ModelRunRepository(repo.conn)
    _graded_pair(repo, tmp_path, "NM", wear=0.0)
    _graded_pair(repo, tmp_path, "DMG", wear=1.0)

    result = ct.train_condition_model(repo, runs, out_path=tmp_path / "m.json")
    assert not result.ok
    assert "of 8 cards" in result.reason  # says exactly what is missing
    assert runs.recent(KIND_CONDITION) == []  # nothing started, nothing logged
    assert not (tmp_path / "m.json").exists()


def test_a_forced_run_is_logged_with_metrics_and_promoted(repo, tmp_path):
    runs = ModelRunRepository(repo.conn)
    for grade, wear in (
        ("NM", 0.0),
        ("LP", 0.3),
        ("MP", 0.6),
        ("HP", 0.8),
        ("DMG", 1.0),
    ):
        for _ in range(2):
            _graded_pair(repo, tmp_path, grade, wear=wear)

    out = tmp_path / "condition.json"
    result = ct.train_condition_model(repo, runs, out_path=out, force=True)
    assert result.ok, result.reason
    assert out.exists()

    row = runs.current_model(KIND_CONDITION)
    assert row is not None and row["version"] == 1
    assert row["kind"] == KIND_CONDITION
    assert row["status"] == "ok"
    assert row["artifact_sha256"] and row["artifact_bytes"] == out.stat().st_size
    # Condition metrics that have no column of their own travel as JSON.
    extra = json.loads(row["metrics_json"])
    assert extra["samples"] == result.metrics.samples
    assert len(extra["confusion"]) == len(cds.GRADES)
    assert "baseline_distance" in extra


def test_a_worse_model_does_not_replace_a_better_one(repo, tmp_path):
    """Promotion compares mean grade distance, where **lower** wins — the opposite
    of the detector's mAP."""
    runs = ModelRunRepository(repo.conn)
    _graded_pair(repo, tmp_path, "NM", wear=0.0)
    _graded_pair(repo, tmp_path, "DMG", wear=1.0)

    good = runs.start(kind=KIND_CONDITION)
    runs.finish(good, map50_95=0.10)  # a very close grader
    runs.promote(good)

    ct.train_condition_model(repo, runs, out_path=tmp_path / "m.json", force=True)
    assert runs.current_model(KIND_CONDITION)["id"] == good  # the good one stays


def test_the_artifact_grades_a_card_through_the_public_api(repo, tmp_path):
    from tcginventory.services.condition import LearnedConditionModel

    runs = ModelRunRepository(repo.conn)
    for grade, wear in (("NM", 0.0), ("DMG", 1.0)):
        for _ in range(3):
            _graded_pair(repo, tmp_path, grade, wear=wear)
    out = tmp_path / "condition.json"
    assert ct.train_condition_model(repo, runs, out_path=out, force=True).ok

    spec = json.loads(out.read_text(encoding="utf-8"))
    assert spec["features"] == list(cf.FEATURE_NAMES)  # readable, not a blob
    assert spec["grades"] == cds.GRADES

    model = LearnedConditionModel(out)
    photo = tmp_path / "query.png"
    _card_photo(photo, wear=1.0)
    from tcginventory.services.exemplars import crop_region

    estimate = model.assess(crop_region(photo, (0.15, 0.1, 0.4, 0.75)), side="back")
    assert estimate.condition.value in cds.GRADES
    assert 0.0 <= estimate.confidence <= 1.0
    assert 0.0 <= estimate.whitening <= 1.0


def test_a_trained_grader_is_preferred_over_the_heuristic(repo, tmp_path, monkeypatch):
    """The heuristic scores worse than guessing on real photos, so anything fitted
    takes precedence."""
    from tcginventory.services import condition as cond

    runs = ModelRunRepository(repo.conn)
    for grade, wear in (("NM", 0.0), ("DMG", 1.0)):
        for _ in range(3):
            _graded_pair(repo, tmp_path, grade, wear=wear)
    assert ct.train_condition_model(repo, runs, force=True).ok  # writes to the data dir

    assert cond.learned_model_path() is not None
    assert isinstance(cond.build_condition_assessor(), cond.LearnedConditionModel)


def test_the_corpus_fingerprint_moves_when_a_card_is_regraded(repo, tmp_path):
    _graded_pair(repo, tmp_path, "NM", wear=0.0)
    samples = cds.collect_samples(repo)
    before = cds.samples_fingerprint(samples)

    repo.conn.execute("UPDATE region SET condition = 'DMG'")
    repo.conn.commit()
    after = cds.samples_fingerprint(cds.collect_samples(repo))
    assert after != before  # a regrade must make a condition model stale
