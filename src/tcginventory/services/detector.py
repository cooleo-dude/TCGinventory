"""Trained card-box detector (spec 06, M-V1.5) — YOLO-nano over ONNX/CPU.

``OnnxCardDetector(path).detect(image)`` returns candidate "card" bounding boxes
as normalised ``(x, y, w, h)`` tuples in 0..1. Single class, class-agnostic: it
answers *where the cards are*, and the identifier says *which card*.

The model is produced offline by ``training/train_detector.py`` from the labeled
region corpus and looked up by :func:`yolo_model_path`; :func:`available` reports
whether one is installed. Postprocessing (``_decode_yolo``, ``_nms``) is pure so
it unit-tests without a model file. Qt-free.

There is no untrained fallback proposer: a classic-CV projection-profile
proposer lived here until 2026-08-04 and was removed — it only worked on cards
laid out with gaps on a plain mat, merged anything touching, and its proposals
cost more to correct than to draw by hand.
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


def available() -> bool:
    """Whether a trained detector is installed — detection is a no-op without
    one (nothing proposes boxes until a model has been trained)."""
    return yolo_model_path() is not None


def trained_model_destination() -> Path:
    """Where a training run should write its model.

    The data dir, not the package: a frozen install's ``_internal`` is replaced
    wholesale on upgrade, and writing there would lose the model. This is the
    second location :func:`yolo_model_path` checks, so a model trained from the
    Operations window is picked up on the next lookup.
    """
    return config.data_dir() / "vision" / "models" / YOLO_MODEL_FILENAME


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
