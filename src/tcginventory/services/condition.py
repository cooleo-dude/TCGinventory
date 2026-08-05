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
# The ordinal-regression grader: a few KB of named coefficients rather than a
# network, because at these corpus sizes a readable linear model over measured
# wear beats a black box (see services/condition_train.py).
LEARNED_MODEL_FILENAME = "condition_ordinal.json"


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


def learned_model_path() -> Path | None:
    """Locate the trained ordinal grader: the data dir first (where a run writes
    it), then a copy bundled with the package. ``None`` if it has never trained."""
    fallback = config.data_dir() / "vision" / "models" / LEARNED_MODEL_FILENAME
    if fallback.is_file():
        return fallback
    try:
        res = resources.files("tcginventory") / "models" / LEARNED_MODEL_FILENAME
        bundled = Path(str(res))
        if bundled.is_file():
            return bundled
    except Exception:  # pragma: no cover - resource lookup edge cases
        pass
    return None


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


class LearnedConditionModel:
    """Grades a card face with the fitted ordinal model.

    Reads the same named measurements the fit was made from, so a prediction is a
    dot product: fast, dependency-free, and inspectable. ``side`` matters — wear
    reads differently on a blue back than a yellow-bordered front, and the model
    holds separate coefficients for each.
    """

    def __init__(self, path: str | Path) -> None:
        import json

        self._spec = json.loads(Path(path).read_text(encoding="utf-8"))
        self.grades = [str(g) for g in self._spec.get("grades", [])]
        self._coef = np.asarray(self._spec["coefficients"], dtype=np.float64)
        self._intercept = float(self._spec["intercept"])
        self._mean = np.asarray(self._spec["mean"], dtype=np.float64)
        self._scale = np.asarray(self._spec["scale"], dtype=np.float64)
        self.trained_on = int(self._spec.get("trained_on", 0))

    def assess(self, image, side: str = "back") -> ConditionEstimate:
        from tcginventory.services import condition_dataset as cds
        from tcginventory.services import condition_features as cf

        with _as_image(image) as img:
            face = cds.flatten_exposure(cds.tighten_to_card(img)).resize(
                (cds.CANONICAL_W, cds.CANONICAL_H), Image.LANCZOS
            )
            measured = cf.face_vector(face)
            vector = cf.sample_vector({side: face})
        standardised = (vector.astype(np.float64) - self._mean) / self._scale
        score = float(standardised @ self._coef + self._intercept)
        index = int(np.clip(round(score), 0, len(self.grades) - 1))
        # How close the score sits to the grade it rounded to, as a confidence.
        confidence = float(max(0.0, 1.0 - abs(score - index)))
        return ConditionEstimate(
            condition=Condition(self.grades[index]),
            # The worst corner/edge whitening actually measured — a real number
            # here, unlike the ONNX path which has none to report.
            whitening=max(
                measured["corner_white_max"], measured["edge_white_max"]
            ),
            confidence=confidence,
        )


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
    """The best grader available: fitted ordinal model, then ONNX, then heuristic.

    The heuristic is genuinely last resort. Measured against 33 graded backs it
    scored **33% where always guessing NM scores 76%**, because border whitening in
    real photos tracks how much mat the box included rather than the card's wear
    (see docs/decisions.md, 2026-08-05). It stays only so the feature does
    *something* before a model exists.
    """
    learned = learned_model_path()
    if learned is not None:
        try:
            return LearnedConditionModel(learned)
        except Exception:  # pragma: no cover - unreadable/incompatible artifact
            pass
    path = condition_model_path()
    if _ort is not None and path is not None:
        try:
            return OnnxConditionModel(path)
        except Exception:  # pragma: no cover - bad/incompatible model
            pass
    return HeuristicConditionModel()


def assess_condition(image, assessor=None, side: str = "back") -> ConditionEstimate:
    """Grade a card face. ``side`` is honoured by graders that model faces
    separately; the heuristic and the ONNX classifier ignore it."""
    grader = assessor or build_condition_assessor()
    try:
        return grader.assess(image, side=side)
    except TypeError:  # a grader that predates the two-face split
        return grader.assess(image)


def export_condition_crops(repo, out_dir: str | Path) -> int:
    """Write condition-labeled back crops to ``out_dir/<condition>/<region>.png``
    (training set for the offline condition classifier). Returns the count."""
    from tcginventory.services.exemplars import _safe, crop_region

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
