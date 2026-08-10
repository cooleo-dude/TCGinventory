"""Close the gap between a photograph and a catalog render (identity Layer 3).

The index holds clean renders; every query is a phone photo. Measured on the
corpus, the encoder handles that badly enough to invert the answer: a photo sits
at median cosine **0.792** from its own card's render but **0.877** from the
nearest *wrong* card's. Top-1 of 20.7% was never a tuning problem — the ordering
itself is wrong.

That distortion turns out to be largely systematic, so a single linear map
corrects it without retraining the backbone. It learns from two sources:

* **Synthetic pairs**, manufactured in bulk — take a catalog render, degrade it
  the way a camera would, and ask that its embedding land on the clean one. These
  need no labelled data at all, which makes 49,551 cards of coverage free.
* **Corpus pairs** — a crop you actually photographed against the art of the card
  you said it was. These are the real relationship rather than an imitation of
  it, but there are only a few dozen, so they carry a weight (`CORPUS_WEIGHT`)
  instead of being drowned. Breadth from the first, fidelity from the second.

**Why this is not the render-trap again.** Training on renders and validating on
renders is how a language probe once scored 99.8% and then 51.7% on real photos.
The augmentation was checked against reality first: real photos sit at median
0.792 from their own render and augmented renders at 0.792 too, so the synthetic
gap is the real one. And because the corpus pairs *are* the benchmark's queries,
accuracy is reported from `cross_validate`, which holds out a whole photo session
at a time — an in-sample score here would be marking its own homework.

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
from tcginventory.services import vectors

# Pairs per card. Two samples the augmentation space cheaply; the fit is limited
# by how many *cards* it sees far more than by repeats of the same one.
AUGMENTATIONS = 2

# Below roughly 5,000 pairs the fit is underdetermined and costs more top-5 than
# it returns in top-1 — measured, not assumed.
MIN_PAIRS = 4000

FILENAME = "photo_to_render.npy"


# How much one real (photograph, render) pair from the corpus counts for against
# one synthetic pair. Real pairs are the exact relationship being learned rather
# than an imitation of it, but there are only a few dozen against thousands of
# synthetic ones, so unweighted they do not register. Picked by leave-one-capture-
# out over 58 real pairs, not by taste:
#
#   weight   held-out top-1   top-5
#   0        58.6%            81.0%     (synthetic only)
#   10       63.8%            82.8%     <-
#   40       60.3%            79.3%
#   400      55.2%            70.7%
#
# They help, and they cannot carry the fit: a few dozen pairs do not constrain a
# 384x384 rotation, so leaning on them harder trades away the breadth the
# synthetic pairs provide. Expect this to want raising as the corpus grows.
CORPUS_WEIGHT = 10.0


@dataclass(frozen=True)
class FitResult:
    cards: int
    pairs: int
    dim: int
    residual: float  # mean cosine of a held-back pair after mapping
    corpus_pairs: int = 0

    @property
    def summary(self) -> str:
        extra = (
            f" + {self.corpus_pairs} from your own photos" if self.corpus_pairs else ""
        )
        return (
            f"{self.pairs:,} synthetic pairs from {self.cards:,} cards{extra} · "
            f"agreement {self.residual:.3f}"
        )


@dataclass(frozen=True)
class FoldReport:
    """Held-out accuracy, so corpus pairs can be used without flattering the score."""

    folds: int
    queries: int
    baseline_top1: float
    baseline_top5: float
    fitted_top1: float
    fitted_top5: float
    corpus_pairs: int
    weight: float

    @property
    def summary(self) -> str:
        return (
            f"held out {self.queries} crops over {self.folds} photo sessions · "
            f"top1 {self.baseline_top1:.0%} → {self.fitted_top1:.0%} · "
            f"top5 {self.baseline_top5:.0%} → {self.fitted_top5:.0%}"
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


def synthetic_pairs(
    repo,
    embedder,
    card_ids,
    targets,
    *,
    augmentations: int = AUGMENTATIONS,
    seed: int = 7,
    on_progress=None,
    should_stop=None,
):
    """Manufacture (photo-like, render) pairs from catalog art.

    Returns ``(sources, goals, cards_seen)``, or ``(None, None, cards_seen)`` when
    too few pairs came back to fit anything worth shipping. ``targets`` maps
    card_id to that card's indexed (render) vector. Cards whose art is missing or
    unreadable are skipped rather than failing the run — a sweep always leaves
    some behind.

    This is the slow part (an embedder forward pass per augmentation), so the
    result is handed back rather than consumed: validation refits many times over
    the same pairs.
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
        return None, None, seen
    return np.vstack(sources), np.vstack(goals), seen


def fit_rotation(
    synthetic: tuple[np.ndarray, np.ndarray],
    corpus: tuple[np.ndarray, np.ndarray] | None = None,
    *,
    weight: float = CORPUS_WEIGHT,
    cards: int = 0,
) -> tuple[np.ndarray, FitResult]:
    """The shipped map: synthetic pairs for coverage, corpus pairs for fidelity.

    The synthetic pairs span the whole catalog but only imitate a photograph; the
    corpus pairs are real photographs but cover a few dozen cards. Weighting lets
    both count — breadth from one, accuracy from the other.
    """
    syn_x, syn_y = synthetic
    real_n = 0
    if corpus is not None and len(corpus[0]):
        real_x, real_y = corpus
        real_n = len(real_x)
        source = np.vstack([syn_x, real_x])
        goal = np.vstack([syn_y, real_y])
        weights = np.concatenate(
            [np.ones(len(syn_x), dtype=np.float32), np.full(real_n, weight)]
        ).astype(np.float32)
    else:
        source, goal, weights = syn_x, syn_y, None

    rotation = solve(source, goal, weights)
    mapped = source @ rotation
    mapped /= np.maximum(np.linalg.norm(mapped, axis=1, keepdims=True), 1e-9)
    agreement = float(np.mean(np.sum(mapped * goal, axis=1)))
    return rotation, FitResult(
        cards, len(syn_x), rotation.shape[0], agreement, corpus_pairs=real_n
    )


def corpus_pairs(repo, model_version: str, targets: dict[str, np.ndarray]):
    """Real (photograph, render) pairs from the cards you have already labelled.

    These are the relationship the map exists to learn, measured rather than
    imitated — a crop you actually photographed, against the catalog art for the
    card you said it was. Every labelled front is already embedded in
    ``region_embedding``, so they cost nothing to collect.

    **Backs are excluded.** Card backs are identical across the game, so pairing
    one with a particular card's front art would teach the map that a generic
    back means that specific card. The capture id comes along so validation can
    hold out a whole photo session at a time.
    """
    rows = repo.conn.execute(
        "SELECT e.card_id, e.dim, e.vector, e.dtype, i.capture_id "
        "FROM region_embedding e "
        "JOIN region r ON r.id = e.region_id "
        "JOIN image i ON i.id = r.image_id "
        "WHERE e.model_version = ? AND COALESCE(i.side, 'front') != 'back' "
        "ORDER BY e.region_id",
        (model_version,),
    ).fetchall()
    sources, goals, captures = [], [], []
    for row in rows:
        target = targets.get(row["card_id"])
        if target is None:
            continue  # the card is not in the index, so there is nothing to aim at
        try:
            vec = vectors.unpack(row["vector"], row["dtype"], int(row["dim"]))
        except ValueError:  # pragma: no cover - corrupt row
            continue
        if vec.size != target.size:
            continue
        sources.append(vec)
        goals.append(target)
        captures.append(int(row["capture_id"]))
    if not sources:
        empty = np.zeros((0, 0), dtype=np.float32)
        return empty, empty, []
    return np.vstack(sources), np.vstack(goals), captures


def solve(
    sources: np.ndarray, goals: np.ndarray, weights: np.ndarray | None = None
) -> np.ndarray:
    """The rotation best carrying ``sources`` onto ``goals`` (weighted Procrustes).

    Weighting is applied to the cross-covariance rather than by duplicating rows,
    so a handful of high-value pairs can matter without inflating the problem.
    """
    if weights is None:
        cross = sources.T @ goals
    else:
        cross = (sources * weights[:, None]).T @ goals
    u, _, vt = np.linalg.svd(cross, full_matrices=False)
    return (u @ vt).astype(np.float32)


def cross_validate(
    conn,
    repo,
    embedder,
    synthetic: tuple[np.ndarray, np.ndarray],
    *,
    weight: float = CORPUS_WEIGHT,
) -> FoldReport | None:
    """Score the map on crops it was not fitted on, one photo session at a time.

    Corpus pairs make the fit better and the usual benchmark meaningless at the
    same time: its queries *are* those crops. Holding out a whole capture — not a
    single crop — is what keeps it honest, because two photos from one session
    share lighting, mat and camera, and a per-crop split would leave near-copies
    of the held-out card in the training half.
    """
    from tcginventory.services import identity_eval as ie

    ids, bank = ie.load_pool(repo, embedder.version)
    if not ids:
        return None
    index = {cid: i for i, cid in enumerate(ids)}
    targets = {cid: bank[i] for i, cid in enumerate(ids)}
    real_x, real_y, captures = corpus_pairs(repo, embedder.version, targets)
    queries = [q for q in ie.collect_queries(repo, embedder) if q.card_id in index]
    if len(real_x) == 0 or not queries:
        return None

    syn_x, syn_y = synthetic
    base_hits = [0, 0]
    fit_hits = [0, 0]
    scored = 0
    folds = sorted(set(captures))
    for held in folds:
        keep = np.array([c != held for c in captures])
        fold_x = np.vstack([syn_x, real_x[keep]])
        fold_y = np.vstack([syn_y, real_y[keep]])
        fold_w = np.concatenate(
            [np.ones(len(syn_x), dtype=np.float32), np.full(int(keep.sum()), weight)]
        )
        rotation = solve(fold_x, fold_y, fold_w)
        for query in queries:
            if query.capture_id != held:
                continue
            scored += 1
            for slot, vector in ((base_hits, query.vector), (fit_hits, None)):
                probe = vector if vector is not None else apply(query.vector, rotation)
                ranked = np.argsort(-(bank @ probe))[:5]
                names = [ids[i] for i in ranked]
                slot[0] += names[0] == query.card_id
                slot[1] += query.card_id in names
    if not scored:
        return None
    return FoldReport(
        folds=len(folds),
        queries=scored,
        baseline_top1=base_hits[0] / scored,
        baseline_top5=base_hits[1] / scored,
        fitted_top1=fit_hits[0] / scored,
        fitted_top5=fit_hits[1] / scored,
        corpus_pairs=len(real_x),
        weight=weight,
    )


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
