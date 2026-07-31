"""Class-agnostic card box proposer (spec 06, M-V1.5) — classic CV, no training.

``propose_card_boxes(image)`` finds candidate "card" bounding boxes on a mostly
blank background and returns them as normalised ``(x, y, w, h)`` tuples in 0..1.
It is the **immediately-available** detector half: pure numpy/Pillow, zero
training, deterministic. The trained YOLO-nano proposer (better on clutter and
overlap) plugs in beside it later; both surface as editable overlays the user
accepts into regions.

Method: a fast **projection-profile** segmentation. Estimate the background
level from the image border, mark foreground (card) pixels by how far they
deviate from it, then split the foreground into a grid — contiguous columns of
card pixels, then contiguous rows within each column. Each cell is tightened to
its foreground bounding box. Best on cards laid out with gaps on a plain mat;
touching/overlapping cards may merge into one box (the trained detector is meant
to cover that case). Qt-free.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

Box = tuple[float, float, float, float]  # normalised (x, y, w, h) in 0..1


def _otsu_threshold(values: np.ndarray) -> float:
    """Otsu's between-class-variance threshold over a 0..255 magnitude image."""
    hist, _ = np.histogram(values, bins=256, range=(0, 256))
    total = values.size
    if total == 0:
        return 0.0
    weight_bg = np.cumsum(hist)
    weight_fg = total - weight_bg
    intensity = np.arange(256)
    sum_total = float((intensity * hist).sum())
    sum_bg = np.cumsum(intensity * hist)
    # Guard the empty-class ends so the division is finite.
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_bg = np.where(weight_bg > 0, sum_bg / np.maximum(weight_bg, 1), 0.0)
        mean_fg = np.where(
            weight_fg > 0, (sum_total - sum_bg) / np.maximum(weight_fg, 1), 0.0
        )
        between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
    return float(np.argmax(between))


def _segments(present: np.ndarray, min_len: int, min_gap: int) -> list[tuple[int, int]]:
    """Contiguous ``True`` runs in ``present``, bridging gaps under ``min_gap`` and
    dropping runs shorter than ``min_len``. Returns ``[start, end)`` index pairs."""
    runs: list[list[int]] = []
    n = len(present)
    i = 0
    while i < n:
        if present[i]:
            j = i
            while j < n and present[j]:
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1
    merged: list[list[int]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] < min_gap:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    return [(s, e) for s, e in merged if e - s >= min_len]


def _foreground_mask(image: Image.Image, max_dim: int) -> np.ndarray:
    """Downscaled boolean mask: True where a pixel differs from the background."""
    gray = image.convert("L")
    w, h = gray.size
    scale = max_dim / max(w, h) if max(w, h) > max_dim else 1.0
    if scale < 1.0:
        gray = gray.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    arr = np.asarray(gray, dtype=np.float32)
    if arr.size == 0:
        return np.zeros((1, 1), dtype=bool)
    border = np.concatenate(
        [arr[0, :], arr[-1, :], arr[:, 0], arr[:, -1]]
    )
    background = float(np.median(border))
    diff = np.abs(arr - background)
    thr = max(_otsu_threshold(diff), 12.0)  # a floor rejects sensor noise
    return diff > thr


def propose_card_boxes(
    image: str | Path | Image.Image,
    *,
    max_dim: int = 640,
    min_area_frac: float = 0.006,
    min_gap_frac: float = 0.012,
    min_side_frac: float = 0.03,
    pad_frac: float = 0.004,
) -> list[Box]:
    """Detect card boxes on a blank background; normalised ``(x, y, w, h)``.

    ``image`` may be a path or a PIL image. Deterministic; returns ``[]`` when no
    card-like foreground is found.
    """
    opened = not isinstance(image, Image.Image)
    img = Image.open(image) if opened else image
    try:
        mask = _foreground_mask(img, max_dim)
    finally:
        if opened:
            img.close()

    h, w = mask.shape
    if not mask.any():
        return []

    col_support = mask.sum(axis=0) > max(1, int(h * 0.04))
    col_runs = _segments(
        col_support, min_len=max(1, int(w * min_side_frac)),
        min_gap=max(1, int(w * min_gap_frac)),
    )

    boxes: list[Box] = []
    for x0, x1 in col_runs:
        strip = mask[:, x0:x1]
        strip_w = x1 - x0
        row_support = strip.sum(axis=1) > max(1, int(strip_w * 0.04))
        row_runs = _segments(
            row_support, min_len=max(1, int(h * min_side_frac)),
            min_gap=max(1, int(h * min_gap_frac)),
        )
        for y0, y1 in row_runs:
            cell = mask[y0:y1, x0:x1]
            cols_any = np.where(cell.any(axis=0))[0]
            rows_any = np.where(cell.any(axis=1))[0]
            if cols_any.size == 0 or rows_any.size == 0:
                continue
            left = x0 + int(cols_any[0])
            right = x0 + int(cols_any[-1]) + 1
            top = y0 + int(rows_any[0])
            bottom = y0 + int(rows_any[-1]) + 1
            pad_x = int(w * pad_frac)
            pad_y = int(h * pad_frac)
            left = max(0, left - pad_x)
            top = max(0, top - pad_y)
            right = min(w, right + pad_x)
            bottom = min(h, bottom + pad_y)
            nx, ny = left / w, top / h
            nw, nh = (right - left) / w, (bottom - top) / h
            if nw * nh >= min_area_frac:
                boxes.append((nx, ny, nw, nh))

    boxes.sort(key=lambda b: (round(b[1], 2), b[0]))  # top-to-bottom, left-to-right
    return boxes
