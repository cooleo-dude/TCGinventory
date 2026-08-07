"""Fit the condition grader (spec 06, M-V3) — ordinal regression, in-process.

**Why this one doesn't need the training environment.** The detector is a neural
network and needs ultralytics; this is a ridge regression over ~20 named
measurements (:mod:`condition_features`), which numpy fits in milliseconds. So it
trains inside the app, needs no 280 MB download, and its artifact is a few KB of
readable JSON coefficients rather than an ONNX. The heavier CNN path stays open for
when the corpus is large enough to earn it — the run log, promotion gate and
Operations tile are shared either way.

**Why regression and not five-way classification.** The scale is ordered: calling an
NM card LP is a near miss, calling it DMG is a disaster. One continuous wear score
plus rounding shares evidence across grades instead of splitting a small corpus
five ways, and the headline metric is **mean grade distance** rather than accuracy.

Validation is **leave-one-capture-out**: every photo has its own lighting and mat, so
scoring a card against a model that saw its own photo would flatter the result.
Baselines (always-NM, and the shipped whitening heuristic) are measured on the same
folds, so the log records whether the model actually beat doing nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from tcginventory.data.vision import (
    KIND_CONDITION,
    ModelRunRepository,
    VisionRepository,
)
from tcginventory.services import condition_dataset as cds
from tcginventory.services import condition_features as cf
from tcginventory.services.condition import LEARNED_MODEL_FILENAME
from tcginventory.services.condition_dataset import GRADES, ORDINAL

ARTIFACT_VERSION = 1
RIDGE_ALPHA = 3.0  # strong by default: there will be more features than cards


@dataclass
class Fit:
    """A fitted model, ready to serialise."""

    coefficients: list[float]
    intercept: float
    mean: list[float]
    scale: list[float]

    def predict(self, matrix: np.ndarray) -> np.ndarray:
        standardised = (matrix - np.asarray(self.mean)) / np.asarray(self.scale)
        return standardised @ np.asarray(self.coefficients) + self.intercept


@dataclass
class Metrics:
    """How a fit did, in the terms that matter for an ordered scale."""

    samples: int
    accuracy: float
    mean_distance: float  # mean |predicted grade - true grade|, the headline
    within_one: float  # fraction no more than one grade out
    macro_f1: float
    confusion: list[list[int]]
    baseline_accuracy: float  # always predict the most common grade
    baseline_distance: float
    heuristic_accuracy: float | None = None
    heuristic_distance: float | None = None

    def as_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)


@dataclass
class ConditionTrainingResult:
    ok: bool
    reason: str = ""
    metrics: Metrics | None = None
    artifact: Path | None = None
    features: dict[str, float] = field(default_factory=dict)
    samples: int = 0


def _standardise(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = matrix.mean(axis=0)
    scale = matrix.std(axis=0)
    scale[scale < 1e-6] = 1.0  # a constant feature carries no information
    return mean, scale


def fit_ridge(
    matrix: np.ndarray, target: np.ndarray, alpha: float = RIDGE_ALPHA
) -> Fit:
    """Closed-form ridge regression — no solver dependency, deterministic."""
    mean, scale = _standardise(matrix)
    x = (matrix - mean) / scale
    x = np.hstack([x, np.ones((len(x), 1), dtype=np.float64)])
    penalty = alpha * np.eye(x.shape[1])
    penalty[-1, -1] = 0.0  # never penalise the intercept
    weights = np.linalg.solve(x.T @ x + penalty, x.T @ target)
    return Fit(
        coefficients=[float(w) for w in weights[:-1]],
        intercept=float(weights[-1]),
        mean=[float(m) for m in mean],
        scale=[float(s) for s in scale],
    )


def to_grades(scores: np.ndarray) -> np.ndarray:
    """Continuous wear score → grade index, clipped to the scale."""
    return np.clip(np.rint(scores), 0, len(GRADES) - 1).astype(int)


def _macro_f1(true: np.ndarray, pred: np.ndarray) -> float:
    scores = []
    for k in range(len(GRADES)):
        tp = int(((pred == k) & (true == k)).sum())
        fp = int(((pred == k) & (true != k)).sum())
        fn = int(((pred != k) & (true == k)).sum())
        if tp + fn == 0:
            continue  # a grade absent from the truth can't be scored
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn)
        scores.append(
            0.0
            if precision + recall == 0
            else 2 * precision * recall / (precision + recall)
        )
    return float(np.mean(scores)) if scores else 0.0


def _confusion(true: np.ndarray, pred: np.ndarray) -> list[list[int]]:
    size = len(GRADES)
    table = [[0] * size for _ in range(size)]
    for t, p in zip(true, pred, strict=False):
        table[int(t)][int(p)] += 1
    return table


def evaluate_leave_one_capture_out(
    matrix: np.ndarray,
    target: np.ndarray,
    captures: np.ndarray,
    *,
    alpha: float = RIDGE_ALPHA,
    heuristic_grades: np.ndarray | None = None,
) -> Metrics:
    """Score the model on photos it never saw, one capture at a time."""
    predictions = np.zeros(len(target), dtype=int)
    for capture in np.unique(captures):
        held = captures == capture
        if held.all():  # only one capture: nothing to hold out honestly
            fit = fit_ridge(matrix, target, alpha)
            predictions = to_grades(fit.predict(matrix))
            break
        fit = fit_ridge(matrix[~held], target[~held], alpha)
        predictions[held] = to_grades(fit.predict(matrix[held]))

    truth = target.astype(int)
    distance = np.abs(predictions - truth)
    majority = int(np.bincount(truth, minlength=len(GRADES)).argmax())
    baseline = np.full_like(truth, majority)
    metrics = Metrics(
        samples=len(truth),
        accuracy=float((predictions == truth).mean()),
        mean_distance=float(distance.mean()),
        within_one=float((distance <= 1).mean()),
        macro_f1=_macro_f1(truth, predictions),
        confusion=_confusion(truth, predictions),
        baseline_accuracy=float((baseline == truth).mean()),
        baseline_distance=float(np.abs(baseline - truth).mean()),
    )
    if heuristic_grades is not None:
        metrics.heuristic_accuracy = float((heuristic_grades == truth).mean())
        metrics.heuristic_distance = float(np.abs(heuristic_grades - truth).mean())
    return metrics


# ---------------------------------------------------------------------------
# the whole job: corpus -> features -> fit -> artifact -> run log
# ---------------------------------------------------------------------------


def build_matrix(
    repo: VisionRepository, samples: list[cds.Sample] | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[cds.Sample]]:
    """Feature matrix, ordinal targets and capture ids for the graded corpus."""
    samples = samples if samples is not None else cds.collect_samples(repo)
    rows, targets, captures, kept = [], [], [], []
    for sample in samples:
        faces: dict[str, Image.Image] = {}
        for side, face in sample.faces.items():
            try:
                faces[side] = cds.canonical_face(face.source_path, face.box)
            except (FileNotFoundError, OSError):
                continue
        if not faces:
            continue  # no readable photo of this card
        rows.append(cf.sample_vector(faces))
        targets.append(ORDINAL[sample.grade])
        captures.append(sample.capture_id)
        kept.append(sample)
    if not rows:
        empty = np.zeros((0, len(cf.FEATURE_NAMES)), dtype=np.float64)
        return empty, np.zeros(0), np.zeros(0), []
    return (
        np.asarray(rows, dtype=np.float64),
        np.asarray(targets, dtype=np.float64),
        np.asarray(captures),
        kept,
    )


def artifact_path() -> Path:
    """Where a trained grader is written — the data dir, like the detector."""
    from tcginventory import config

    return config.data_dir() / "vision" / "models" / LEARNED_MODEL_FILENAME


def train_condition_model(
    repo: VisionRepository,
    runs: ModelRunRepository | None = None,
    *,
    source: str = "ui",
    alpha: float = RIDGE_ALPHA,
    min_per_grade: int = cds.MIN_PER_GRADE,
    min_images_per_grade: int = cds.MIN_IMAGES_PER_GRADE,
    out_path: Path | None = None,
    force: bool = False,
) -> ConditionTrainingResult:
    """Gate, fit, score, write the artifact and record the run.

    Refuses before logging anything when the corpus can't support a model — a grade
    seen in one photo teaches that photo, not wear. ``force`` overrides the gate for
    experiments; the run is still recorded, and its metrics will show the damage.
    """
    runs = runs or ModelRunRepository(repo.conn)
    samples = cds.collect_samples(repo)
    gate = cds.check_gate(
        samples,
        min_per_grade=min_per_grade,
        min_images_per_grade=min_images_per_grade,
    )
    if not gate.ok and not force:
        return ConditionTrainingResult(
            ok=False, reason=gate.summary, samples=len(samples)
        )

    matrix, target, captures, kept = build_matrix(repo, samples)
    if len(kept) < 2:
        return ConditionTrainingResult(
            ok=False, reason="no readable graded photos to train on", samples=len(kept)
        )

    run_id = runs.start(
        kind=KIND_CONDITION,
        source=source,
        corpus_images=len({f.image_id for s in kept for f in s.faces.values()}),
        corpus_boxes=len(kept),
        corpus_cards=len(kept),
        corpus_fingerprint=cds.samples_fingerprint(kept),
        base_weights=f"ridge(alpha={alpha})",
    )
    try:
        metrics = evaluate_leave_one_capture_out(matrix, target, captures, alpha=alpha)
        fit = fit_ridge(matrix, target, alpha)  # final model uses every card
        out = Path(out_path) if out_path else artifact_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "version": ARTIFACT_VERSION,
                    "kind": "condition_ordinal_ridge",
                    "grades": GRADES,
                    "features": list(cf.FEATURE_NAMES),
                    "coefficients": fit.coefficients,
                    "intercept": fit.intercept,
                    "mean": fit.mean,
                    "scale": fit.scale,
                    "trained_on": len(kept),
                    "alpha": alpha,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # a failed fit is data too
        runs.finish(run_id, status="failed", notes=str(exc)[:300])
        return ConditionTrainingResult(ok=False, reason=str(exc), samples=len(kept))

    from tcginventory.services import fitness
    from tcginventory.services.training import sha256_file

    # One higher-is-better number, expressed as lift over always-guessing the
    # commonest grade. Zero means the model learned nothing a constant could not
    # do; negative means it is worse than that.
    score = fitness.condition_fitness(
        metrics.accuracy,
        metrics.mean_distance,
        metrics.baseline_accuracy,
        metrics.baseline_distance,
    )
    runs.finish(
        run_id,
        status="ok",
        train_images=len(kept),
        val_images=len(np.unique(captures)),
        # NOTE: these four columns carry *grader* metrics, not detection ones.
        # Nothing shared may read them — `fitness` is the comparable number, and
        # metrics_json holds the rest under its own names. Reading `precision` as
        # if it were the always-NM baseline is exactly the bug that had the window
        # announcing a grader "beats guessing" by comparing accuracy against
        # macro-F1 (2026-08-07).
        precision=metrics.macro_f1,
        recall=metrics.within_one,
        map50=metrics.accuracy,
        map50_95=metrics.mean_distance,  # lower is better for this kind
        fitness=score,
        metrics_json=metrics.as_json(),
        artifact_path=str(out),
        artifact_sha256=sha256_file(out),
        artifact_bytes=out.stat().st_size,
        notes=(
            f"{metrics.accuracy:.0%} exact, {metrics.mean_distance:.2f} grades off "
            f"(always-{GRADES[int(np.bincount(target.astype(int)).argmax())]} "
            f"baseline: {metrics.baseline_accuracy:.0%}, "
            f"{metrics.baseline_distance:.2f})"
        ),
    )
    # A grader that cannot beat guessing must not serve, however it compares with
    # an earlier grader that also could not. That gate comes first; only then does
    # a new fit have to improve on the one in use by more than the noise a corpus
    # this small carries.
    incumbent = runs.current_model(KIND_CONDITION)
    floor = fitness.binomial_noise(metrics.accuracy, metrics.samples)
    verdict = fitness.compare(
        score,
        incumbent["fitness"] if incumbent else None,
        floor=floor,
        candidate_data=len(kept),
        incumbent_data=int(incumbent["corpus_cards"] or 0) if incumbent else 0,
    )
    if score is not None and score > 0 and verdict.promote:
        runs.promote(run_id)
    return ConditionTrainingResult(
        ok=True,
        metrics=metrics,
        artifact=out,
        features=dict(zip(cf.FEATURE_NAMES, fit.coefficients, strict=False)),
        samples=len(kept),
    )
