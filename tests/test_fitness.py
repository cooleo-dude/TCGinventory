"""What makes one model better than another, and when the difference is real.

Every promotion in the app goes through :func:`fitness.compare`, so the rules it
encodes are worth stating twice — once in the module and once here. The last test
in this file is the one that matters most: the real corpus, the real numbers, and
the model that five retrains could not promote.
"""

from __future__ import annotations

import pytest

from tcginventory.services import fitness

# The two detectors from the real run log (2026-08-07). Same mAP50, same perfect
# recall; the newer one trades 1.5 points of box tightness for 5 points of
# precision, on 39% more data.
SERVING_V1 = {"map50": 0.995, "map50_95": 0.96996, "precision": 0.94894, "recall": 1.0}
REFUSED_RUN = {"map50": 0.995, "map50_95": 0.95522, "precision": 0.99895, "recall": 1.0}


# -- scoring -----------------------------------------------------------------


def test_detector_fitness_weights_finding_cards_above_tight_boxes():
    assert fitness.detector_fitness(SERVING_V1) == pytest.approx(0.980678, abs=1e-5)
    assert fitness.detector_fitness(REFUSED_RUN) == pytest.approx(0.994207, abs=1e-5)


def test_a_missing_metric_is_not_scored_as_a_zero():
    """A run whose CSV lost a column should be judged on what it has, not
    penalised for the gap — otherwise a parsing change looks like a bad model."""
    partial = fitness.detector_fitness({"recall": 1.0, "precision": 1.0})
    assert partial == pytest.approx(1.0, abs=1e-5)
    assert fitness.detector_fitness({}) is None
    assert fitness.detector_fitness(None) is None


def test_condition_fitness_is_lift_over_guessing_so_zero_is_the_gate():
    # Exactly the baseline on both terms: it has learned nothing a constant could
    # not do, and the number says so without needing a threshold.
    assert fitness.condition_fitness(0.61, 1.0, 0.61, 1.0) == pytest.approx(
        0.0, abs=1e-5
    )
    assert fitness.condition_fitness(0.72, 0.5, 0.61, 1.0) > 0  # better
    assert fitness.condition_fitness(0.44, 1.4, 0.61, 1.0) < 0  # worse than a guess
    assert fitness.condition_fitness(None, 0.5, 0.61, 1.0) is None


def test_identity_fitness_leans_on_top1():
    assert fitness.identity_fitness(1.0, 0.0) == pytest.approx(0.70, abs=1e-5)
    assert fitness.identity_fitness(0.0, 1.0) == pytest.approx(0.30, abs=1e-5)
    assert fitness.identity_fitness(None, None) is None


# -- how much of a difference is noise ---------------------------------------


def _curve(values, key="map50_95"):
    """Epochs identical but for one metric — by default mAP50-95, where the
    wobble of a converged detector actually lives."""
    base = {"map50": 0.99, "precision": 0.95, "recall": 1.0, "map50_95": 0.95}
    return [{**base, key: v} for v in values]


def test_tail_noise_measures_the_spread_of_a_converged_run():
    steady = fitness.tail_noise(_curve([0.95, 0.96, 0.95, 0.96, 0.95, 0.96]))
    assert 0 < steady < 0.01  # a tenth-weighted wobble is a small fitness wobble


def test_a_run_too_short_to_have_a_tail_reports_no_noise():
    """Its last epochs still contain the model *learning*, so their spread is
    progress, not measurement error. Better to admit ignorance than to hand back
    a huge floor that swallows every real difference."""
    assert fitness.tail_noise(_curve([0.05, 0.34])) == 0.0


def test_a_run_that_never_converged_reports_no_noise_either():
    """Swinging *recall*, which carries 0.40 of the score — a run still lurching
    about on the metric that matters most has not settled, and its spread is not
    a measurement error to reason with."""
    wild = _curve([0.1, 0.9, 0.2, 0.8, 0.3, 0.95], key="recall")
    assert fitness.tail_noise(wild) == 0.0


def test_a_wobble_confined_to_box_tightness_is_still_usable_noise():
    """The counterpart: mAP50-95 can swing hard without the model having moved,
    because it is only a tenth of the score. That is exactly the noise the floor
    exists to absorb, so it must survive the convergence guard."""
    loose = _curve([0.1, 0.9, 0.2, 0.8, 0.3, 0.95])
    assert 0 < fitness.tail_noise(loose) <= fitness.MAX_NOISE_FLOOR


def test_binomial_noise_is_wide_when_the_sample_is_small():
    # 58 labelled crops put the error bar on a 45% top-1 at about 6.5 points —
    # two benchmarks inside that band are the same benchmark.
    assert fitness.binomial_noise(0.45, 58) == pytest.approx(0.0653, abs=1e-3)
    assert fitness.binomial_noise(0.45, 1) == 0.0
    assert fitness.binomial_noise(None, 500) == 0.0


def test_scores_from_unstamped_runs_are_never_comparable():
    assert fitness.comparable(fitness.SPLIT_HASHED_V1, fitness.SPLIT_HASHED_V1)
    assert not fitness.comparable(fitness.SPLIT_HASHED_V1, None)
    assert not fitness.comparable(None, None)  # two unknowns are not a match
    assert not fitness.comparable("hashed-v1", "shuffled-v0")


# -- the decision ------------------------------------------------------------


def test_a_first_model_always_wins_even_unmeasured():
    """Regression (2026-08-06): reversed, this left a working detector installed
    but unpromoted, and Operations called it "never run"."""
    assert fitness.compare(0.9, None).promote
    assert fitness.compare(None, None).promote


def test_an_unmeasured_candidate_never_displaces_a_known_good_model():
    verdict = fitness.compare(None, 0.9)
    assert not verdict.promote
    assert "no evidence" in verdict.reason


def test_a_clear_win_and_a_clear_loss_are_both_decided_on_score():
    win = fitness.compare(0.95, 0.90, floor=0.01)
    assert win.promote and win.margin == pytest.approx(0.05, abs=1e-5)

    loss = fitness.compare(0.85, 0.90, floor=0.01)
    assert not loss.promote and "genuinely better" in loss.reason


def test_inside_the_noise_band_the_model_that_saw_more_data_wins():
    """This is what lets the detector move at all once the metrics saturate: two
    indistinguishable models are separated by evidence, not by luck."""
    verdict = fitness.compare(
        0.9005, 0.9000, floor=0.01, candidate_data=39, incumbent_data=28
    )
    assert verdict.promote
    assert "within noise" in verdict.reason and "39 images against 28" in verdict.reason


def test_inside_the_band_with_nothing_new_learned_the_incumbent_stays():
    verdict = fitness.compare(
        0.9005, 0.9000, floor=0.01, candidate_data=28, incumbent_data=28
    )
    assert not verdict.promote
    assert "nothing to gain" in verdict.reason


def test_incomparable_splits_are_settled_by_the_corpus_not_the_score():
    """The incumbent's number describes an exam the candidate never sat, so
    comparing them at all would be meaningless — however tempting the gap."""
    verdict = fitness.compare(
        0.10, 0.99, candidate_data=39, incumbent_data=28, like_for_like=False
    )
    assert verdict.promote and not verdict.comparable
    assert "not comparable" in verdict.reason

    keeps = fitness.compare(
        0.99, 0.10, candidate_data=9, incumbent_data=28, like_for_like=False
    )
    assert not keeps.promote and "saw less of the corpus" in keeps.reason


# -- the case this was all built for -----------------------------------------


def test_the_five_refused_retrains_would_now_be_promoted():
    """Regression, 2026-08-07, with the real numbers.

    Five consecutive runs on 39% more data, each five points better on precision,
    were refused because mAP50-95 — box tightness, and a metric already saturated
    on this corpus — dropped by 0.0147. That gap was smaller than the incumbent's
    own epoch-to-epoch standard deviation. Judged on finding cards rather than on
    drawing them tightly, the newer model wins outright.
    """
    served = fitness.detector_fitness(SERVING_V1)
    candidate = fitness.detector_fitness(REFUSED_RUN)

    assert REFUSED_RUN["map50_95"] < SERVING_V1["map50_95"]  # the old gate's view
    assert candidate > served  # and the new one's

    verdict = fitness.compare(
        candidate, served, floor=0.001, candidate_data=39, incumbent_data=28
    )
    assert verdict.promote
    assert verdict.margin == pytest.approx(0.0135, abs=1e-3)
    # Won on the score itself, not on the more-data tiebreak.
    assert "clear of" in verdict.reason
