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

from importlib import resources
from pathlib import Path

import numpy as np
from PIL import Image

from tcginventory import config

try:
    import onnxruntime as _ort
except Exception:  # pragma: no cover - onnxruntime missing (e.g. minimal env)
    _ort = None

Box = tuple[float, float, float, float]  # normalised (x, y, w, h) in 0..1

YOLO_MODEL_FILENAME = "card_yolo.onnx"


def yolo_model_path() -> Path | None:
    """Locate the trained YOLO card detector: the bundled package copy first
    (dev tree or PyInstaller bundle), then a user-dropped file under the data
    dir. ``None`` if neither exists (Phase B not yet trained)."""
    try:
        res = resources.files("tcginventory") / "models" / YOLO_MODEL_FILENAME
        bundled = Path(str(res))
        if bundled.is_file():
            return bundled
    except Exception:  # pragma: no cover - resource lookup edge cases
        pass
    fallback = config.data_dir() / "vision" / "models" / YOLO_MODEL_FILENAME
    return fallback if fallback.is_file() else None


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


# --------------------------------------------------------------------------
# Trained YOLO-nano proposer (Phase B) — ONNX/CPU inference.
# The heavy work (postprocessing) is factored into pure functions so it can be
# unit-tested without a model file.
# --------------------------------------------------------------------------


def _nms(boxes_xyxy: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    """Greedy non-max suppression; returns kept indices, highest score first."""
    if len(boxes_xyxy) == 0:
        return []
    x1, y1, x2, y2 = boxes_xyxy.T
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[i] + areas[rest] - inter
        iou = np.where(union > 0, inter / union, 0.0)
        order = rest[iou <= iou_thr]
    return keep


def _decode_yolo(
    output: np.ndarray,
    orig_w: int,
    orig_h: int,
    scale: float,
    pad_x: float,
    pad_y: float,
    conf: float,
    iou: float,
) -> list[Box]:
    """Decode a single-class YOLOv8 head into normalised ``(x, y, w, h)`` boxes.

    ``output`` is the raw ONNX tensor (``[1, 5, N]`` or ``[1, N, 5]``; rows are
    ``cx, cy, w, h, score`` in letterboxed input pixels). ``scale``/``pad`` are the
    letterbox params that mapped the original image into the model input.
    """
    arr = np.asarray(output, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr[0]
    if arr.shape[0] == 5 and arr.shape[1] != 5:
        arr = arr.T  # [5, N] -> [N, 5]
    if arr.shape[1] < 5:
        return []
    scores = arr[:, 4]
    keep = scores >= conf
    arr, scores = arr[keep], scores[keep]
    if arr.shape[0] == 0:
        return []
    cx, cy, w, h = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]
    x1 = (cx - w / 2 - pad_x) / scale
    y1 = (cy - h / 2 - pad_y) / scale
    x2 = (cx + w / 2 - pad_x) / scale
    y2 = (cy + h / 2 - pad_y) / scale
    boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)
    out: list[Box] = []
    for i in _nms(boxes_xyxy, scores, iou):
        bx1, by1, bx2, by2 = boxes_xyxy[i]
        nx = max(0.0, min(1.0, bx1 / orig_w))
        ny = max(0.0, min(1.0, by1 / orig_h))
        nw = max(0.0, min(1.0, (bx2 - bx1) / orig_w))
        nh = max(0.0, min(1.0, (by2 - by1) / orig_h))
        if nw > 0 and nh > 0:
            out.append((nx, ny, nw, nh))
    out.sort(key=lambda b: (round(b[1], 2), b[0]))
    return out


def _letterbox(image: Image.Image, size: int):
    """Resize into a ``size×size`` square keeping aspect (grey pad). Returns the
    NCHW float tensor plus the (scale, pad_x, pad_y) mapping back to the source."""
    w, h = image.size
    scale = size / max(w, h)
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    resized = image.convert("RGB").resize((new_w, new_h))
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    pad_x, pad_y = (size - new_w) // 2, (size - new_h) // 2
    canvas.paste(resized, (pad_x, pad_y))
    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    tensor = arr.transpose(2, 0, 1)[None, ...]  # NCHW
    return tensor, scale, float(pad_x), float(pad_y)


class OnnxCardDetector:
    """Trained single-class card detector via onnxruntime (YOLOv8-nano export)."""

    def __init__(self, path: str | Path, size: int = 640) -> None:
        if _ort is None:  # pragma: no cover - guarded by callers
            raise RuntimeError("onnxruntime is not installed")
        self._session = _ort.InferenceSession(
            str(path), providers=["CPUExecutionProvider"]
        )
        self._input = self._session.get_inputs()[0].name
        self._size = size

    def detect(
        self, image: str | Path | Image.Image, conf: float = 0.25, iou: float = 0.5
    ) -> list[Box]:  # pragma: no cover - exercised once a model exists
        opened = not isinstance(image, Image.Image)
        img = Image.open(image) if opened else image
        try:
            orig_w, orig_h = img.size
            tensor, scale, pad_x, pad_y = _letterbox(img, self._size)
        finally:
            if opened:
                img.close()
        raw = self._session.run(None, {self._input: tensor.astype(np.float32)})[0]
        return _decode_yolo(raw, orig_w, orig_h, scale, pad_x, pad_y, conf, iou)
