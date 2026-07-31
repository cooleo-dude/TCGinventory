"""Condition assessment (M-V3): border-whitening heuristic + crop export."""

from __future__ import annotations

from PIL import Image, ImageDraw

from tcginventory.data.vision import CardLabel, VisionRepository, open_vision
from tcginventory.domain.enums import Condition
from tcginventory.services import condition
from tcginventory.services.condition import (
    HeuristicConditionModel,
    assess_condition,
)


def _card(border=None, size=(120, 168)):
    """A dark-blue card back, optionally with a coloured border frame."""
    img = Image.new("RGB", size, (30, 40, 90))  # blue back interior
    if border is not None:
        draw = ImageDraw.Draw(img)
        b = 10
        w, h = size
        draw.rectangle([0, 0, w - 1, b], fill=border)
        draw.rectangle([0, h - 1 - b, w - 1, h - 1], fill=border)
        draw.rectangle([0, 0, b, h - 1], fill=border)
        draw.rectangle([w - 1 - b, 0, w - 1, h - 1], fill=border)
    return img


def test_clean_back_reads_near_mint():
    est = assess_condition(_card(), HeuristicConditionModel())
    assert est.condition == Condition.NM
    assert est.whitening < 0.03


def test_white_edge_fraying_downgrades_condition():
    est = assess_condition(_card(border=(255, 255, 255)), HeuristicConditionModel())
    assert est.condition != Condition.NM  # heavy whitening → worse grade
    assert est.whitening > 0.2


def test_yellow_border_is_not_mistaken_for_whitening():
    # The card's own yellow border is bright but saturated → not counted as wear.
    est = assess_condition(_card(border=(255, 225, 0)), HeuristicConditionModel())
    assert est.condition == Condition.NM
    assert est.whitening < 0.03


def test_export_condition_crops_groups_by_grade(tmp_path):
    conn = open_vision(":memory:")
    repo = VisionRepository(conn)
    scene = tmp_path / "backs.png"
    _card(size=(200, 120)).save(scene)
    cap = repo.add_capture()
    img = repo.add_image(cap, "sha", str(scene), side="back", width=200, height=120)
    repo.add_region(img, 1, (0.0, 0.0, 0.5, 1.0), None, CardLabel(condition="NM"))
    repo.add_region(img, 2, (0.5, 0.0, 0.5, 1.0), None, CardLabel(condition="LP"))
    # A front-side region with a condition is NOT part of the back-condition set.
    front = repo.add_image(cap, "sha2", str(scene), side="front")
    repo.add_region(front, 3, (0, 0, 1, 1), None, CardLabel(condition="MP"))

    out = tmp_path / "crops"
    assert condition.export_condition_crops(repo, out) == 2
    assert (out / "NM").is_dir() and (out / "LP").is_dir()
    assert not (out / "MP").exists()
    conn.close()


def test_build_assessor_falls_back_to_heuristic_without_a_model(monkeypatch):
    monkeypatch.setattr(condition, "condition_model_path", lambda: None)
    assert isinstance(condition.build_condition_assessor(), HeuristicConditionModel)


def test_assess_accepts_a_path(tmp_path):
    p = tmp_path / "back.png"
    _card().save(p)
    est = assess_condition(str(p), HeuristicConditionModel())
    assert isinstance(est.condition, Condition)
