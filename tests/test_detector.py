"""Classic-CV card box proposer (M-V1.5): projection-profile segmentation."""

from __future__ import annotations

from PIL import Image, ImageDraw

from tcginventory.services.detector import propose_card_boxes


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
