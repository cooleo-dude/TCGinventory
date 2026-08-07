"""How a model run is scored, and when a new one replaces the one in use.

One module for a judgement that used to live in three places and disagree with
itself. Every kind gets a single **higher-is-better** number — :func:`fitness` —
and every promotion goes through :func:`compare`, which knows the two things the
old gate did not: that a score is only comparable with another score measured the
same way, and that a difference smaller than the run's own noise is not a
difference at all.

Why this exists (2026-08-07). The detector promoted on mAP50-95 alone. That
metric averages average-precision across IoU 0.5→0.95, so above about 0.75 IoU it
is measuring how tightly a *human* drew the training boxes rather than whether a
card was found — and it was read off ``best.pt``, the single luckiest epoch of a
hundred. On this corpus the incumbent's 0.970 sat 2.1 standard deviations above
its own tail mean, on a five-image validation split that no longer exists. Five
consecutive retrains, each on 39% more data and each with five points *better*
precision, lost to it by 0.0147 — less than one run's epoch-to-epoch noise. The
detector could not be replaced, and because staleness was measured against the
promoted run, the window called it stale forever.

Qt-free and free of SQL, so every one of these judgements tests headless.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# The validation split written by the current exporter: membership decided per
# image by hash, so an image that lands in val stays in val as the corpus grows.
# Runs recorded before that shipped carry no policy at all, and their scores are
# not comparable with anything after it — see :func:`comparable`.
SPLIT_HASHED_V1 = "hashed-v1"

# ---------------------------------------------------------------------------
# what "better" means, per kind
# ---------------------------------------------------------------------------

# The detector's job is to hand whole, findable cards to the identifier. Missing
# a card costs a row you never get; a phantom box costs a row you have to delete;
# a slightly loose crop costs the encoder almost nothing. The weights say exactly
# that, and they are deliberately *not* ultralytics' own fitness (0.1·mAP50 +
# 0.9·mAP50-95), which is tuned for benchmark leaderboards where box tightness is
# the whole game.
DETECTOR_WEIGHTS = {
    "recall": 0.40,  # a card not found is a card not identified
    "precision": 0.30,  # a phantom box is a row you have to delete by hand
    "map50": 0.20,  # found it, at a threshold that means "found it"
    "map50_95": 0.10,  # crop tightness: it only has to be good enough
}

# NM · LP · MP · HP · DMG — the worst a grade can be wrong by.
CONDITION_GRADE_SPAN = 4.0

# Identity's headline is top-1; top-5 carries the rest because the window offers
# a ranked list and the second guess is one click away.
IDENTITY_WEIGHTS = {"top1": 0.70, "top5": 0.30}

# Below this, two scores are the same score whatever the arithmetic says.
MIN_MARGIN = 0.002
# How many epochs off the end of the curve estimate a run's own noise.
TAIL_EPOCHS = 10
# Fewer epochs than this and the tail is not a tail — it still contains the model
# learning, and its spread measures progress rather than noise.
MIN_TAIL_EPOCHS = 5
# A tail this wide means the run had not converged, so the spread is again
# progress and not noise. Trusting it would let a floor swallow real differences.
MAX_NOISE_FLOOR = 0.05


def detector_fitness(metrics: dict | None) -> float | None:
    """Weighted detection quality from one epoch's (or one run's) metrics.

    Returns ``None`` when nothing usable was recorded — an unmeasured run has no
    score, which is different from a score of zero.
    """
    if not metrics:
        return None
    total = 0.0
    weight = 0.0
    for key, w in DETECTOR_WEIGHTS.items():
        value = metrics.get(key)
        if value is None:
            continue
        total += w * float(value)
        weight += w
    # Re-normalise over the metrics we actually have, so a run missing one column
    # is scored on its merits rather than penalised for the gap.
    return total / weight if weight else None


def condition_fitness(
    accuracy: float | None,
    mean_distance: float | None,
    baseline_accuracy: float | None,
    baseline_distance: float | None,
) -> float | None:
    """How much better than guessing the commonest grade this grader is.

    Expressed as **lift over the baseline**, so the number is zero when the model
    has learned nothing and negative when it is actively worse. That makes the
    promotion rule self-evident — a grader has to score above zero to serve —
    rather than a threshold someone has to remember.
    """
    if accuracy is None or baseline_accuracy is None:
        return None
    lift_exact = float(accuracy) - float(baseline_accuracy)
    lift_distance = 0.0
    if mean_distance is not None and baseline_distance is not None:
        # Distance improves *downward*; divide by the span so a grade of error
        # weighs the same as a proportion of exact answers.
        lift_distance = (
            float(baseline_distance) - float(mean_distance)
        ) / CONDITION_GRADE_SPAN
    return 0.6 * lift_exact + 0.4 * lift_distance


def identity_fitness(top1: float | None, top5: float | None) -> float | None:
    """Ranked-suggestion quality: mostly top-1, partly top-5."""
    if top1 is None and top5 is None:
        return None
    total = 0.0
    weight = 0.0
    for value, w in (
        (top1, IDENTITY_WEIGHTS["top1"]),
        (top5, IDENTITY_WEIGHTS["top5"]),
    ):
        if value is None:
            continue
        total += w * float(value)
        weight += w
    return total / weight if weight else None


# ---------------------------------------------------------------------------
# how much of a difference is real
# ---------------------------------------------------------------------------


def tail_noise(epochs: list[dict], *, tail: int = TAIL_EPOCHS) -> float:
    """The run's own epoch-to-epoch spread in fitness, near convergence.

    Consecutive late epochs are near-identical models scored on an identical
    validation set, so whatever they disagree by is measurement noise and nothing
    else. It is the only noise estimate available from a single run, and on this
    corpus it is worth about 0.01–0.02 of mAP50-95 — larger than the gap that was
    rejecting better models.

    Two guards keep the estimate honest. A short run has no tail: its last epochs
    still contain the model *learning*, and their spread measures progress. A very
    wide tail says the same thing about a long run that never settled. In both
    cases this returns 0.0 and the caller falls back to :data:`MIN_MARGIN` — an
    unusable noise estimate must not become a licence to ignore real differences.
    """
    scored = [
        f for f in (detector_fitness(e) for e in epochs[-tail:] if e) if f is not None
    ]
    if len(scored) < max(2, MIN_TAIL_EPOCHS):
        return 0.0
    mean = sum(scored) / len(scored)
    spread = math.sqrt(sum((v - mean) ** 2 for v in scored) / len(scored))
    return spread if spread <= MAX_NOISE_FLOOR else 0.0


def binomial_noise(rate: float | None, samples: int | None) -> float:
    """Standard error of a proportion measured on ``samples`` trials.

    Identity and the grader score a handful of items rather than a curve, and the
    handful is small: 58 labelled crops put the standard error of a 45% top-1 at
    about 6.5 points. Two benchmarks either side of that are the same benchmark,
    and the window should not report a win.
    """
    if rate is None or not samples or samples < 2:
        return 0.0
    p = min(max(float(rate), 0.0), 1.0)
    return math.sqrt(p * (1.0 - p) / float(samples))


def comparable(candidate_policy: str | None, incumbent_policy: str | None) -> bool:
    """Whether two recorded scores were measured the same way.

    Scores from different validation splits are different exams. The old exporter
    shuffled the whole corpus and sliced the first 20%, so growing the corpus from
    28 images to 39 replaced the validation set wholesale — the incumbent's number
    described five images that are now in training. Runs from that era carry no
    policy, and no policy never matches.
    """
    if not candidate_policy or not incumbent_policy:
        return False
    return candidate_policy == incumbent_policy


@dataclass(frozen=True)
class Verdict:
    """The promotion decision, and the sentence explaining it."""

    promote: bool
    reason: str
    margin: float = 0.0  # candidate − incumbent, in fitness
    floor: float = 0.0  # how big a difference had to be to count
    comparable: bool = True


def compare(
    candidate: float | None,
    incumbent: float | None,
    *,
    floor: float = MIN_MARGIN,
    candidate_data: int = 0,
    incumbent_data: int = 0,
    like_for_like: bool = True,
) -> Verdict:
    """Whether a candidate should replace the model in use.

    Four rules, in order:

    1. **A first model always wins.** There is nothing to lose against, and an
       unscored model in hand beats no model at all. (Reversing this once meant a
       working detector was reported as never trained — 2026-08-06.)
    2. **An unmeasured candidate never beats an incumbent.** No score is no
       evidence, and the model in use is known-good.
    3. **Scores measured differently are not compared at all.** When the two runs
       used different validation splits the incumbent's number describes an exam
       the candidate never sat, so the corpus decides: the model that has seen
       more of it wins.
    4. **Otherwise, a difference smaller than the noise is not a difference.**
       Outside the band the better score wins. Inside it the models are
       indistinguishable, so the one trained on more data wins — it has strictly
       more evidence behind it, and preferring it is what lets the detector move
       at all once the metrics saturate.
    """
    if incumbent is None:
        return Verdict(True, "first model — nothing to compare against")
    if candidate is None:
        return Verdict(
            False,
            "this run recorded no usable metrics, so there is no evidence it "
            "improves on the model in use",
        )

    margin = float(candidate) - float(incumbent)
    floor = max(float(floor), MIN_MARGIN)

    if not like_for_like:
        wins = candidate_data >= incumbent_data
        return Verdict(
            wins,
            (
                "the model in use was scored on a different validation split, so "
                "the two numbers are not comparable — this run saw "
                f"{candidate_data} images against {incumbent_data}, so it takes over"
                if wins
                else "the two runs used different validation splits, and this one "
                f"saw less of the corpus ({candidate_data} images against "
                f"{incumbent_data}), so the model in use keeps serving"
            ),
            margin=margin,
            floor=floor,
            comparable=False,
        )

    if margin > floor:
        return Verdict(
            True,
            f"scored {margin:+.3f} on fitness, clear of the ±{floor:.3f} this "
            "run's own epochs vary by",
            margin=margin,
            floor=floor,
        )
    if margin < -floor:
        return Verdict(
            False,
            f"scored {margin:+.3f} on fitness, outside the ±{floor:.3f} that "
            "could be noise — the model in use is genuinely better",
            margin=margin,
            floor=floor,
        )

    wins = candidate_data > incumbent_data
    return Verdict(
        wins,
        (
            f"the two are within noise ({margin:+.3f} against a ±{floor:.3f} "
            f"band), and this run saw more of the corpus ({candidate_data} images "
            f"against {incumbent_data}) — so it takes over"
            if wins
            else f"the two are within noise ({margin:+.3f} against a ±{floor:.3f} "
            "band) and this run saw no more of the corpus, so there is nothing to "
            "gain by switching"
        ),
        margin=margin,
        floor=floor,
    )
