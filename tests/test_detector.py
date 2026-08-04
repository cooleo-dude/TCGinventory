"""Trained card detector (M-V1.5): YOLO postprocessing, pure and model-free.

The classic-CV proposer these tests used to cover was removed on 2026-08-04 —
detection is trained-only now.
"""

from __future__ import annotations

import numpy as np

from tcginventory.services.detector import (
    _decode_yolo,
    _nms,
    available,
    yolo_model_path,
)


def test_available_tracks_whether_a_model_is_installed():
    assert available() is (yolo_model_path() is not None)


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
