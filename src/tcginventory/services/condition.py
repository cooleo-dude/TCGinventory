"""Card condition assessment from the **back** (spec 06, M-V3).

Identity comes from card fronts; **condition** (edge/corner whitening, wear) reads
best off the back, where fraying shows as white against the dark blue. Two
backends, chosen at build time — mirroring the detector/identifier:

* **Heuristic** — dependency-free, no training: measures near-white, low-saturation
  pixels in the card's border band (so the *yellow* border is not mistaken for
  whitening) and maps that fraction to a condition grade. Weak but real, usable
  from day one.
* **OnnxConditionModel** — a trained classifier over back crops, used when
  ``condition_model.onnx`` is present (bundled like DINOv2). Supersedes the
  heuristic.

Qt-free. The scale is best→worst: NM, LP, MP, HP, DMG.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import numpy as np
from PIL import Image

from tcginventory import config
from tcginventory.domain.enums import Condition

try:
    import onnxruntime as _ort
except Exception:  # pragma: no cover - onnxruntime missing (e.g. minimal env)
    _ort = None

# Grading scale, best → worst (SEALED is not a wear level, so it's excluded).
CONDITION_SCALE = [
    Condition.NM,
    Condition.LP,
    Condition.MP,
    Condition.HP,
    Condition.DMG,
]

# Border-whitening fraction → grade. Thresholds are deliberately conservative;
# the trained model replaces them once available.
_WHITENING_BREAKS = [
    (0.03, Condition.NM),
    (0.10, Condition.LP),
    (0.20, Condition.MP),
    (0.35, Condition.HP),
]

CONDITION_MODEL_FILENAME = "condition_model.onnx"


@dataclass
class ConditionEstimate:
    condition: Condition
    whitening: float  # 0..1 border-wear fraction (0.0 when a model is used)
    confidence: float


def condition_model_path() -> Path | None:
    """Locate the trained condition model: bundled package copy first, then a
    user-dropped file under the data dir. ``None`` if neither exists."""
    try:
        res = resources.files("tcginventory") / "models" / CONDITION_MODEL_FILENAME
        bundled = Path(str(res))
        if bundled.is_file():
            return bundled
    except Exception:  # pragma: no cover - resource lookup edge cases
        pass
    fallback = config.data_dir() / "vision" / "models" / CONDITION_MODEL_FILENAME
    return fallback if fallback.is_file() else None


def _border_whitening(image: Image.Image, band_frac: float = 0.06) -> float:
    """Fraction of the card's border band that is near-white and low-saturation
    (edge fraying), so the bright *yellow* card border is not counted."""
    hsv = np.asarray(image.convert("HSV"), dtype=np.float32)
    if hsv.ndim != 3:
        return 0.0
    h, w = hsv.shape[:2]
    sat = hsv[:, :, 1] / 255.0
    val = hsv[:, :, 2] / 255.0
    band = max(1, int(round(min(h, w) * band_frac)))
    border = np.zeros((h, w), dtype=bool)
    border[:band, :] = border[-band:, :] = True
    border[:, :band] = border[:, -band:] = True
    whitish = (val > 0.7) & (sat < 0.25)
    denom = int(border.sum())
    return float((whitish & border).sum()) / denom if denom else 0.0


def _grade(whitening: float) -> Condition:
    for threshold, condition in _WHITENING_BREAKS:
        if whitening < threshold:
            return condition
    return Condition.DMG


class HeuristicConditionModel:
    """Border-whitening baseline (no training). Advisory confidence."""

    version = "heuristic-whitening"

    def assess(self, image) -> ConditionEstimate:
        with _as_image(image) as img:
            whitening = _border_whitening(img)
        return ConditionEstimate(_grade(whitening), whitening, confidence=0.4)


class OnnxConditionModel:
    """Trained back-condition classifier via onnxruntime (5-class softmax)."""

    _MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    _STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

    def __init__(self, path: str | Path, size: int = 224) -> None:
        if _ort is None:  # pragma: no cover - guarded by callers
            raise RuntimeError("onnxruntime is not installed")
        self._session = _ort.InferenceSession(
            str(path), providers=["CPUExecutionProvider"]
        )
        self._input = self._session.get_inputs()[0].name
        self._size = size
        self.version = f"onnx:{Path(path).stem}"

    def assess(self, image) -> ConditionEstimate:  # pragma: no cover - needs a model
        with _as_image(image) as img:
            rgb = img.convert("RGB").resize((self._size, self._size))
        arr = np.asarray(rgb, dtype=np.float32) / 255.0
        arr = (arr - self._MEAN) / self._STD
        tensor = arr.transpose(2, 0, 1)[None, ...].astype(np.float32)
        logits = np.asarray(
            self._session.run(None, {self._input: tensor})[0], dtype=np.float32
        ).reshape(-1)
        probs = _softmax(logits[: len(CONDITION_SCALE)])
        idx = int(np.argmax(probs))
        return ConditionEstimate(CONDITION_SCALE[idx], 0.0, float(probs[idx]))


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x))
    total = e.sum()
    return e / total if total else e


class _as_image:
    """Context manager yielding a PIL image, closing it only if opened here."""

    def __init__(self, src) -> None:
        self._src = src
        self._opened = False

    def __enter__(self) -> Image.Image:
        if isinstance(self._src, Image.Image):
            return self._src
        self._img = Image.open(self._src)
        self._opened = True
        return self._img

    def __exit__(self, *exc) -> None:
        if self._opened:
            self._img.close()


def build_condition_assessor():
    """The trained model when present + onnxruntime available, else the heuristic."""
    path = condition_model_path()
    if _ort is not None and path is not None:
        try:
            return OnnxConditionModel(path)
        except Exception:  # pragma: no cover - bad/incompatible model
            pass
    return HeuristicConditionModel()


def assess_condition(image, assessor=None) -> ConditionEstimate:
    return (assessor or build_condition_assessor()).assess(image)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "unknown"


def export_condition_crops(repo, out_dir: str | Path) -> int:
    """Write condition-labeled back crops to ``out_dir/<condition>/<region>.png``
    (training set for the offline condition classifier). Returns the count."""
    from tcginventory.services.exemplars import crop_region

    out = Path(out_dir)
    written = 0
    for r in repo.condition_regions():
        try:
            crop = crop_region(r["image_path"], (r["x"], r["y"], r["w"], r["h"]))
        except (FileNotFoundError, OSError):
            continue
        folder = out / _safe(r["condition"])
        folder.mkdir(parents=True, exist_ok=True)
        crop.save(folder / f"{r['region_id']:06d}.png")
        written += 1
    return written
