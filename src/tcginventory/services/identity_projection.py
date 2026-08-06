"""Close the gap between a photograph and a catalog render (identity Layer 3).

The index holds clean renders; every query is a phone photo. Measured on the
corpus, the encoder handles that badly enough to invert the answer: a photo sits
at median cosine **0.792** from its own card's render but **0.877** from the
nearest *wrong* card's. Top-1 of 20.7% was never a tuning problem — the ordering
itself is wrong.

That distortion turns out to be largely systematic, so a single linear map
corrects it without retraining the backbone. The map is fit on pairs we can
manufacture in bulk: take a catalog render, degrade it the way a camera would,
and ask that its embedding land on the clean one. Nothing about the fit needs
labelled photographs, which is what makes 49,551 cards of training data free.

**Why this is not the render-trap again.** Training on renders and validating on
renders is how a language probe once scored 99.8% and then 51.7% on real photos.
Here the *validation* is always the labelled photo crops, and the augmentation
was checked against them first: real photos sit at median 0.792 from their own
render and augmented renders at 0.792 too, so the synthetic gap is the real one.

**Why a rotation rather than a free linear map.** An unconstrained ridge fit
collapses — it has 385x384 parameters and, at 6,000 pairs, quietly destroys the
geometry (top-5 74.1% -> 69.0%). The orthogonal Procrustes solution can only
rotate, never rescale or discard directions, so it cannot trade away the
structure that makes the rest of the pool separable. With enough pairs it
improves both metrics; with too few it improves neither, which is worth knowing
before blaming the method.

Qt-free. The fitted matrix is a 384x384 float32 array on disk — no torch, no
training environment, seconds to apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

from tcginventory import config

# Pairs per card. Two samples the augmentation space cheaply; the fit is limited
# by how many *cards* it sees far more than by repeats of the same one.
AUGMENTATIONS = 2

# Below roughly 5,000 pairs the fit is underdetermined and costs more top-5 than
# it returns in top-1 — measured, not assumed.
MIN_PAIRS = 4000

FILENAME = "photo_to_render.npy"


@dataclass(frozen=True)
class FitResult:
    cards: int
    pairs: int
    dim: int
    residual: float  # mean cosine of a held-back pair after mapping

    @property
    def summary(self) -> str:
        return (
            f"{self.pairs:,} pairs from {self.cards:,} cards · "
            f"agreement {self.residual:.3f}"
        )


def matrix_path() -> Path:
    return config.data_dir() / "vision" / "models" / FILENAME


def photo_like(card: Image.Image, rng) -> Image.Image:
    """A render pushed toward what a phone camera would have produced.

    Each step is something a photograph does and a render does not: the card is
    never quite parallel to the sensor, never perfectly lit, never perfectly
    cropped. Verified against the real crops rather than chosen by eye — see the
    module docstring.
    """
    width, height = card.size
    dx = int(width * rng.uniform(0.01, 0.05))
    dy = int(height * rng.uniform(0.01, 0.05))
    card = card.transform(
        (width, height),
        Image.QUAD,
        (
            dx,
            dy,
            int(rng.integers(0, dx + 1)),
            height - dy,
            width - dx,
            height - dy,
            width - dx,
            dy,
        ),
        Image.BICUBIC,
    )
    card = card.rotate(rng.uniform(-4, 4), resample=Image.BICUBIC, expand=False)
    card = card.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 1.6)))
    card = ImageEnhance.Brightness(card).enhance(rng.uniform(0.65, 1.15))
    card = ImageEnhance.Color(card).enhance(rng.uniform(0.75, 1.2))
    card = ImageEnhance.Contrast(card).enhance(rng.uniform(0.8, 1.15))
    card = _glare(card, rng)
    return _on_a_mat(card, rng)


def _glare(card: Image.Image, rng) -> Image.Image:
    """A soft bright band, as a light source reflecting off the sleeve."""
    width, height = card.size
    centre = rng.uniform(0, width)
    columns = np.arange(width, dtype=np.float32)
    strength = np.clip(90.0 - np.abs(columns - centre) / 2.0, 0, None)
    mask = np.tile(strength, (height, 1)).astype(np.uint8)
    white = Image.new("RGB", (width, height), (255, 255, 255))
    return Image.composite(white, card, Image.fromarray(mask, "L"))


def _on_a_mat(card: Image.Image, rng) -> Image.Image:
    """A loose crop on a surface — a drawn box never hugs the card exactly."""
    width, height = card.size
    pad = int(min(width, height) * rng.uniform(0.02, 0.10))
    shade = int(rng.uniform(90, 200))
    scene = Image.new("RGB", (width + 2 * pad, height + 2 * pad), (shade,) * 3)
    scene.paste(card, (pad, pad))
    noisy = np.asarray(scene, dtype=np.float32) + rng.normal(
        0, 4, (scene.height, scene.width, 3)
    )
    return Image.fromarray(np.clip(noisy, 0, 255).astype(np.uint8))


def fit(
    repo,
    embedder,
    card_ids,
    targets,
    *,
    augmentations: int = AUGMENTATIONS,
    seed: int = 7,
    on_progress=None,
    should_stop=None,
) -> tuple[np.ndarray, FitResult] | tuple[None, FitResult]:
    """Learn the rotation carrying photo-domain embeddings onto render-domain.

    ``targets`` maps card_id to that card's indexed (render) vector. Cards whose
    art is missing or unreadable are skipped rather than failing the run — a
    sweep always leaves some behind.
    """
    from tcginventory.services import catalog_index as ci

    rng = np.random.default_rng(seed)
    sources: list[np.ndarray] = []
    goals: list[np.ndarray] = []
    seen = 0
    for card_id in card_ids:
        if should_stop is not None and should_stop():
            break
        target = targets.get(card_id)
        art = ci.source_for(card_id)
        if target is None or art is None:
            continue
        try:
            with Image.open(art) as opened:
                clean = opened.convert("RGB")
                for _ in range(augmentations):
                    vector = embedder.embed(photo_like(clean, rng))
                    sources.append(np.asarray(vector, dtype=np.float32))
                    goals.append(target)
        except (OSError, ValueError):  # pragma: no cover - unreadable art
            continue
        seen += 1
        if on_progress is not None and seen % 100 == 0:
            on_progress(seen, len(sources))

    if len(sources) < MIN_PAIRS:
        return None, FitResult(seen, len(sources), 0, 0.0)

    source = np.vstack(sources)
    goal = np.vstack(goals)
    # Orthogonal Procrustes: the closest rotation taking source onto goal.
    u, _, vt = np.linalg.svd(source.T @ goal, full_matrices=False)
    rotation = (u @ vt).astype(np.float32)
    mapped = source @ rotation
    mapped /= np.maximum(np.linalg.norm(mapped, axis=1, keepdims=True), 1e-9)
    agreement = float(np.mean(np.sum(mapped * goal, axis=1)))
    return rotation, FitResult(seen, len(source), rotation.shape[0], agreement)


def save(rotation: np.ndarray, path: Path | None = None) -> Path:
    target = path or matrix_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    np.save(target, rotation.astype(np.float32))
    return target


def load(path: Path | None = None) -> np.ndarray | None:
    """The installed map, or None when one has never been fitted."""
    target = path or matrix_path()
    if not target.is_file():
        return None
    try:
        rotation = np.load(target)
    except (OSError, ValueError):  # pragma: no cover - corrupt artifact
        return None
    if rotation.ndim != 2 or rotation.shape[0] != rotation.shape[1]:
        return None
    return rotation.astype(np.float32)


def apply(vector: np.ndarray, rotation: np.ndarray | None) -> np.ndarray:
    """Move a photo-domain embedding into render domain, renormalised.

    A shape mismatch means the map was fitted for a different encoder; the vector
    is returned untouched rather than silently scored against the wrong space.
    """
    if rotation is None or vector.shape[0] != rotation.shape[0]:
        return vector
    mapped = vector @ rotation
    norm = float(np.linalg.norm(mapped))
    return (mapped / norm).astype(np.float32) if norm > 1e-9 else vector
