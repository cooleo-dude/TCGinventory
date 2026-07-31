"""Classic-CV card box proposer (M-V1.5): projection-profile segmentation."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from tcginventory.services.detector import (
    _decode_yolo,
    _nms,
    propose_card_boxes,
)


def _scene(rects, size=(240, 160), bg=255, fg=0):
    """A blank-background image with dark filled rectangles (as pixel boxes)."""
    img = Image.new("L", size, color=bg)
    draw = ImageDraw.Draw(img)
    for x, y, w, h in rects:
        draw.rectangle([x, y, x + w, y + h], fill=fg)
    return img


def _centres(boxes):
    return sorted((x + w / 2, y + h / 2) for x, y, w, h in boxes)


def test_detects_two_side_by_side_cards():
    img = _scene([(20, 20, 60, 110), (150, 20, 60, 110)])  # gap at x≈80..150
    boxes = propose_card_boxes(img)
    assert len(boxes) == 2
    (cx1, _), (cx2, _) = _centres(boxes)
    assert abs(cx1 - (20 + 30) / 240) < 0.06  # left card centre ≈ 0.21
    assert abs(cx2 - (150 + 30) / 240) < 0.06  # right card centre ≈ 0.75
    for _, _, w, h in boxes:
        assert 0 < w < 1 and 0 < h < 1


def test_detects_a_two_by_two_grid():
    img = _scene(
        [(20, 20, 60, 50), (150, 20, 60, 50), (20, 100, 60, 50), (150, 100, 60, 50)]
    )
    assert len(propose_card_boxes(img)) == 4


def test_blank_image_yields_no_boxes():
    assert propose_card_boxes(_scene([])) == []


def test_accepts_a_path(tmp_path):
    p = tmp_path / "scene.png"
    _scene([(20, 20, 60, 110), (150, 20, 60, 110)]).save(p)
    assert len(propose_card_boxes(str(p))) == 2


def test_boxes_are_ordered_top_to_bottom_left_to_right():
    img = _scene(
        [(20, 100, 60, 50), (150, 20, 60, 50), (20, 20, 60, 50), (150, 100, 60, 50)]
    )
    boxes = propose_card_boxes(img)
    assert len(boxes) == 4
    ys = [round(y, 2) for _, y, _, _ in boxes]
    assert ys == sorted(ys)  # row-major order


# -- trained-detector (YOLO) postprocessing — pure, no model needed ---------


def test_nms_drops_overlapping_lower_score_boxes():
    boxes = np.array(
        [[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], dtype=np.float32
    )
    scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)
    keep = _nms(boxes, scores, iou_thr=0.5)
    assert keep[0] == 0  # highest score first
    assert 1 not in keep  # heavily overlaps box 0 → suppressed
    assert 2 in keep  # disjoint → kept


def test_decode_yolo_maps_letterbox_pixels_to_normalised_boxes():
    # One clean detection + a near-duplicate NMS should collapse, on a 200x100
    # image letterboxed into 640 (scale 3.2, pad_y = (640-320)/2 = 160).
    scale, pad_x, pad_y = 3.2, 0.0, 160.0
    # card centred at original (100, 50), size 40x60 → letterbox px:
    cx = 100 * scale + pad_x
    cy = 50 * scale + pad_y
    w, h = 40 * scale, 60 * scale
    output = np.array(
        [[[cx, cx + 1], [cy, cy + 1], [w, w], [h, h], [0.9, 0.85]]],
        dtype=np.float32,
    )  # shape [1, 5, 2] — two overlapping candidates
    boxes = _decode_yolo(output, 200, 100, scale, pad_x, pad_y, conf=0.25, iou=0.5)
    assert len(boxes) == 1
    x, y, bw, bh = boxes[0]
    assert abs((x + bw / 2) - 100 / 200) < 0.02  # centre x ≈ 0.5
    assert abs((y + bh / 2) - 50 / 100) < 0.02  # centre y ≈ 0.5
    assert abs(bw - 40 / 200) < 0.02 and abs(bh - 60 / 100) < 0.02


def test_decode_yolo_filters_below_confidence():
    output = np.array([[[300], [300], [50], [50], [0.10]]], dtype=np.float32)
    assert _decode_yolo(output, 200, 100, 3.2, 0.0, 160.0, conf=0.25, iou=0.5) == []
