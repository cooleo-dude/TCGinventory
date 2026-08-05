"""Measure wear on a canonical card face (spec 06, M-V3).

Graders look at specific places — corners, edges, then the surface — so these are
the features the condition model is fitted on, rather than raw pixels. At the corpus
sizes this has to work with (tens of cards), a handful of named measurements beats
a network with millions of parameters, and the resulting model can be *read*: a
coefficient on ``back_corner_white_max`` says what the model actually believes.

Every measurement is taken from a face already tightened to the card, exposure
flattened and warped to the canonical rectangle by
:mod:`tcginventory.services.condition_dataset` — without that, brightness differences
between photos dominate everything (the reason the old whitening heuristic scored
worse than guessing).

Pure numpy/Pillow, so both training and inference run in the app with no extra
dependency.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from tcginventory.services.condition_dataset import face_patches

# Named so an artifact stays legible; order is the feature-vector order.
FACE_FEATURES = (
    "corner_white_max",
    "corner_white_mean",
    "corner_bright_max",
    "corner_grad_max",
    "edge_white_max",
    "edge_white_mean",
    "edge_bright_max",
    "edge_grad_max",
    "surface_grad",
    "surface_std",
)
FACES = ("front", "back")
# Two faces of named measurements, plus a flag per face saying whether it was
# photographed at all — a missing face must not read as "measured zero wear".
FEATURE_NAMES = tuple(
    [f"{side}_{name}" for side in FACES for name in FACE_FEATURES]
    + [f"has_{side}" for side in FACES]
)

_WHITE_LEVEL = 170.0  # brightness above which a border pixel counts as whitened
_WHITE_SAT = 0.22  # ...provided it is also this desaturated


def _stats(patch: Image.Image, reference: float) -> tuple[float, float, float]:
    """(white fraction, brightness above the face's own level, edge energy)."""
    arr = np.asarray(patch.convert("RGB"), dtype=np.float32)
    if arr.size == 0:  # pragma: no cover - a degenerate patch
        return 0.0, 0.0, 0.0
    brightness = arr.mean(axis=2)
    high = arr.max(axis=2)
    low = arr.min(axis=2)
    saturation = (high - low) / np.maximum(high, 1.0)
    white = (brightness > _WHITE_LEVEL) & (saturation < _WHITE_SAT)
    grey = brightness
    gy = np.abs(np.diff(grey, axis=0)).mean() if grey.shape[0] > 1 else 0.0
    gx = np.abs(np.diff(grey, axis=1)).mean() if grey.shape[1] > 1 else 0.0
    return (
        float(white.mean()),
        float(brightness.mean() - reference),
        float((gy + gx) / 2.0),
    )


def face_vector(face: Image.Image) -> dict[str, float]:
    """The named measurements for one canonical face."""
    patches = face_patches(face)
    reference = float(np.asarray(face.convert("L"), dtype=np.float32).mean())

    corners = [_stats(patches[k], reference) for k in patches if k.startswith("corner")]
    edges = [_stats(patches[k], reference) for k in patches if k.startswith("edge")]
    surface = np.asarray(patches["surface"].convert("L"), dtype=np.float32)
    surf_gy = np.abs(np.diff(surface, axis=0)).mean() if surface.shape[0] > 1 else 0.0
    surf_gx = np.abs(np.diff(surface, axis=1)).mean() if surface.shape[1] > 1 else 0.0

    return {
        "corner_white_max": max(c[0] for c in corners),
        "corner_white_mean": float(np.mean([c[0] for c in corners])),
        "corner_bright_max": max(c[1] for c in corners),
        "corner_grad_max": max(c[2] for c in corners),
        "edge_white_max": max(e[0] for e in edges),
        "edge_white_mean": float(np.mean([e[0] for e in edges])),
        "edge_bright_max": max(e[1] for e in edges),
        "edge_grad_max": max(e[2] for e in edges),
        "surface_grad": float((surf_gy + surf_gx) / 2.0),
        "surface_std": float(surface.std()),
    }


def sample_vector(faces: dict[str, Image.Image]) -> np.ndarray:
    """A card's full feature vector from whichever faces were photographed.

    A missing face contributes zeros *and* clears its presence flag, so the model
    can learn to lean on the face it actually has. Fronts and backs are kept in
    separate slots on purpose: whitening reads far more clearly against a blue back
    than a yellow-bordered front, and surface wear is mostly a front concern.
    """
    values: list[float] = []
    for side in FACES:
        face = faces.get(side)
        vector = face_vector(face) if face is not None else None
        values.extend(0.0 if vector is None else vector[name] for name in FACE_FEATURES)
    values.extend(1.0 if faces.get(side) is not None else 0.0 for side in FACES)
    return np.asarray(values, dtype=np.float32)


def describe(vector: np.ndarray) -> dict[str, float]:
    """Feature vector → named values, for logging and for reading a model."""
    return dict(zip(FEATURE_NAMES, (float(v) for v in vector), strict=False))
