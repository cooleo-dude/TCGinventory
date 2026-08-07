"""The Models destination: what it says about each model, and that it renders.

The judgements live in :mod:`tcginventory.services.modelsummary`, so most of this
tests that layer headless — "stale", "blocked" and "what is still needed" are
claims the screen makes on the user's behalf, and they should be provably right.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.data.vision import (
    KIND_CONDITION,
    KIND_DETECTOR,
    KIND_IDENTITY,
    CardLabel,
    ModelRunRepository,
    VisionRepository,
    open_vision,
)
from tcginventory.services import modelsummary as ms
from tcginventory.services import operations as ops


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    conn = connect(":memory:")
    migrate(conn)
    # A real file under the tmp data dir, not ":memory:": the view opens and
    # closes its own vision connection on every refresh, exactly as it does in
    # the app, and a shared in-memory handle would be dead after the first one.
    vision = open_vision()
    yield conn, VisionRepository(vision), ModelRunRepository(vision), tmp_path
    conn.close()
    vision.close()


def _install_model() -> None:
    """A promoted run only counts as *serving* if its file is actually there."""
    from tcginventory.services import detector

    path = detector.trained_model_destination()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a real onnx, but a real file")


def _finish_grader(
    runs, run_id, *, exact, baseline, macro_f1=0.3, distance=0.8, baseline_distance=1.0
):
    """Close a condition run the way ``condition_train`` does.

    Worth a helper because the shared columns carry *grader* metrics here —
    ``precision`` is macro-F1, ``map50_95`` is mean grade distance where lower
    wins — and the always-NM baseline lives only in ``metrics_json``. Writing
    them by hand in each test is how a test can pass while the screen quotes the
    wrong number.
    """
    import json

    from tcginventory.services import fitness

    runs.finish(
        run_id,
        map50=exact,
        map50_95=distance,
        precision=macro_f1,
        recall=0.9,
        fitness=fitness.condition_fitness(exact, distance, baseline, baseline_distance),
        metrics_json=json.dumps(
            {
                "accuracy": exact,
                "baseline_accuracy": baseline,
                "mean_distance": distance,
                "baseline_distance": baseline_distance,
                "macro_f1": macro_f1,
                "samples": 63,
            }
        ),
    )


def _corpus(repo, tmp_path, images=3, boxes_each=2, card="c-1"):
    from PIL import Image

    scene = tmp_path / "scene.png"
    Image.new("RGB", (120, 80), (180, 180, 180)).save(scene)
    capture = repo.add_capture(source="manual")
    for i in range(images):
        image = repo.add_image(capture, f"sha{i}", str(scene), "front")
        for b in range(boxes_each):
            repo.add_region(
                image, b + 1, (0.0, 0.0, 0.5, 0.5), None, CardLabel(card_id=card)
            )


# -- the registry split ------------------------------------------------------


def test_operations_no_longer_carries_the_model_sections():
    """They moved to Models outright — a pointer left behind would be a second
    place to look for the same thing."""
    assert [g.id for g in ops.GROUPS] == [
        "data_in",
        "data_out",
        "prices",
        "reference",
        "automation",
        "maintenance",
    ]
    assert [g.id for g in ops.MODEL_GROUPS] == [
        "model_regions",
        "model_condition",
        "model_identity",
        "training_env",
    ]


def test_every_model_operation_still_has_a_registry_entry():
    """The run log, statuses and staleness all key off these ids, so moving the
    screen must not orphan them."""
    for group in ops.MODEL_GROUPS:
        assert ops.in_group(group.id), group.id


# -- the detector ------------------------------------------------------------


def test_a_detector_that_has_seen_the_whole_corpus_is_serving(env):
    _conn, repo, runs, tmp_path = env
    _install_model()
    _corpus(repo, tmp_path, images=3)
    run = runs.start(kind=KIND_DETECTOR, corpus_images=3, corpus_boxes=6)
    runs.finish(run, map50=0.99, map50_95=0.95, precision=0.9, recall=1.0)
    runs.promote(run)

    summary = ms.detector_summary(repo, runs)
    assert summary.state == "served"
    assert summary.unseen_images == 0
    # Fitness leads, because it is what the promotion gate judges: recall and
    # precision weighted above box tightness. 0.4·1.0 + 0.3·0.9 + 0.2·0.99 +
    # 0.1·0.95 = 0.963.
    assert [m.label for m in summary.metrics][:2] == ["fitness", "recall"]
    assert [m.value for m in summary.metrics][:2] == ["96.3%", "100.0%"]


def test_a_detector_falls_behind_when_the_corpus_grows(env):
    """The most actionable thing the screen can say, so it has to be right."""
    _conn, repo, runs, tmp_path = env
    _install_model()
    _corpus(repo, tmp_path, images=1)
    run = runs.start(kind=KIND_DETECTOR, corpus_images=1, corpus_boxes=2)
    runs.finish(run, map50=0.99)
    runs.promote(run)
    assert ms.detector_summary(repo, runs).state == "served"

    _corpus(repo, tmp_path, images=ms.STALE_IMAGE_DRIFT, card="c-2")
    summary = ms.detector_summary(repo, runs)
    assert summary.state == "stale"
    assert summary.unseen_images == ms.STALE_IMAGE_DRIFT


def test_no_detector_reads_as_no_model(env):
    _conn, repo, runs, _tmp = env
    assert ms.detector_summary(repo, runs).state == "none"


# -- the condition gate ------------------------------------------------------


def test_the_condition_gate_reports_the_shortfall_per_grade(env):
    """ "Not trained" is useless on its own; the number of cards still needed is
    the only thing that changes it."""
    _conn, repo, runs, _tmp = env
    summary = ms.condition_summary(repo, runs)

    assert summary.state == "blocked"
    grades = {c.grade: c for c in summary.coverage}
    assert set(grades) == {"NM", "LP", "MP", "HP", "DMG"}
    assert grades["NM"].cards_needed == summary.min_cards  # nothing graded yet
    assert grades["NM"].shortfall.startswith(f"{summary.min_cards} more")
    assert summary.cards_needed == 5 * summary.min_cards


def test_a_trained_grader_reports_itself_as_serving(env):
    _conn, repo, runs, _tmp = env
    run = runs.start(kind=KIND_CONDITION)
    _finish_grader(runs, run, exact=0.61, baseline=0.44)
    runs.promote(run)

    summary = ms.condition_summary(repo, runs)
    assert summary.trained and summary.beats_guessing and summary.state == "served"
    assert [m.label for m in summary.metrics][:2] == ["exact", "always-NM"]
    assert [m.value for m in summary.metrics][:2] == ["61.0%", "44.0%"]


def test_a_grader_no_better_than_guessing_is_not_called_serving(env):
    """The baseline lives in metrics_json; the `precision` column holds macro-F1.
    Reading the column instead compared exact accuracy against an unrelated
    number and announced the difference as beating a constant guess."""
    _conn, repo, runs, _tmp = env
    run = runs.start(kind=KIND_CONDITION)
    _finish_grader(runs, run, exact=0.44, baseline=0.61, macro_f1=0.12)
    runs.promote(run)

    summary = ms.condition_summary(repo, runs)
    assert summary.trained and not summary.beats_guessing
    assert summary.state == "stale"
    # The quoted baseline is the real one, not the macro-F1 sitting in `precision`.
    assert [m.value for m in summary.metrics][:2] == ["44.0%", "61.0%"]


# -- identity ----------------------------------------------------------------


def _identity_run(runs, layers, **kw):
    import json

    run = runs.start(kind=KIND_IDENTITY, source="benchmark")
    runs.finish(
        run,
        map50=0.76,
        map50_95=0.85,
        metrics_json=json.dumps({"layers": layers, "queries": 58, **kw}),
    )
    return run


def test_identity_reports_each_layer_and_what_it_added(env):
    conn, repo, runs, _tmp = env
    _identity_run(
        runs, {"encoder": 0.207, "scope": 0.672, "projection": 0.707, "band": 0.759}
    )
    summary = ms.identity_summary(conn, repo, runs)

    assert [layer.label for layer in summary.layers] == [
        "DINOv2-small encoder",
        "+ language scope",
        "+ photo matching",
        "+ bottom band",
    ]
    assert summary.layers[0].delta is None  # the base has nothing to improve on
    assert summary.layers[1].delta == pytest.approx(0.465, abs=1e-3)
    assert [m.value for m in summary.metrics] == ["76.0%", "85.0%"]


def test_identity_says_when_its_own_score_is_in_sample(env):
    """Photo matching is fitted partly on the benchmark's own crops. A screen
    that quoted the resulting figure without saying so would be flattering
    itself."""
    conn, repo, runs, _tmp = env
    _identity_run(runs, {"encoder": 0.2, "band": 0.93}, in_sample_from="projection")

    summary = ms.identity_summary(conn, repo, runs)
    assert summary.in_sample_from == "projection"


def test_identity_without_a_benchmark_still_lists_its_parts(env):
    conn, repo, runs, _tmp = env
    summary = ms.identity_summary(conn, repo, runs)

    assert [c.name for c in summary.components] == [
        "encoder",
        "catalog index",
        "language scope",
        "photo matching",
        "bottom band",
    ]
    assert summary.layers == []  # nothing measured, so nothing claimed


# -- the corpus --------------------------------------------------------------


def test_the_corpus_says_what_each_model_is_waiting_for(env):
    """Counts alone do not tell you the detector has never seen a third of your
    photos, which is the whole reason this section exists."""
    conn, repo, runs, tmp_path = env
    _install_model()
    _corpus(repo, tmp_path, images=6, boxes_each=2)
    run = runs.start(kind=KIND_DETECTOR, corpus_images=1, corpus_boxes=2)
    runs.finish(run, map50=0.9)
    runs.promote(run)

    summary = ms.corpus_summary(conn, repo, runs)
    assert summary.images == 6 and summary.boxes == 12
    feeds = {f.model: f for f in summary.feeds}
    assert feeds["Region detection"].state == "warn"
    assert "5 images" in feeds["Region detection"].detail
    assert feeds["Condition"].state == "no"
    assert feeds["Card identity"].state == "no"  # nothing indexed in this fixture


# -- the screen --------------------------------------------------------------


@pytest.fixture()
def app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_screen_builds_every_section(app, env, monkeypatch):
    conn, repo, runs, tmp_path = env
    _corpus(repo, tmp_path, images=2)
    vision = repo.conn
    monkeypatch.setattr("tcginventory.data.vision.open_vision", lambda *a, **k: vision)

    from tcginventory.ui.views.models import ModelsView

    view = ModelsView(conn)
    assert [s.property("navLabel") for s in view._sections] == [
        "Overview",
        "Region detection",
        "Condition",
        "Card identity",
        "Training environment",
        "Data corpus",
    ]
    assert view._nav.count() == 6
    view.deleteLater()


def test_the_loss_chart_falls_back_when_a_run_predates_training_losses(app):
    """Runs recorded before the training columns existed have only validation
    figures — show those rather than an empty chart."""
    from tcginventory.ui.views.models import _LossChart

    old = [
        {"epoch": i, "box_loss": 1.0 / i, "map50": i / 10, "train_box_loss": None}
        for i in range(1, 6)
    ]
    chart = _LossChart(old)
    assert chart._shown == {"box_loss", "map50"}
    assert "train_box_loss" not in chart._buttons  # no switch for an absent line


def test_every_line_on_the_chart_can_be_switched_off(app):
    from tcginventory.ui.views.models import _LossChart

    epochs = [
        {
            "epoch": i,
            "train_box_loss": 1.0 / i,
            "train_cls_loss": 2.0 / i,
            "map50": i / 10,
        }
        for i in range(1, 6)
    ]
    chart = _LossChart(epochs)
    assert chart._shown == {"train_box_loss", "train_cls_loss", "map50"}

    for key in list(chart._shown):
        chart._toggle(key)
    assert chart._shown == set()  # an empty chart is allowed; it says something

    chart._toggle("map50")
    assert chart._shown == {"map50"}


def test_the_bars_survive_a_grade_with_nothing_in_it(app):
    """A fresh corpus has zero of every grade, and a zero-width bar must not
    divide by anything."""
    from tcginventory.ui.views.models import _Bars

    bars = _Bars([("NM", 0.0, "no", "0 cards"), ("LP", 1.0, "ok", "9 cards")])
    assert bars is not None


def test_a_percentage_reads_as_a_dash_when_it_was_never_measured():
    assert ms._pct(None) == "—"
    assert ms._pct(0.995) == "99.5%"


def test_the_summary_layer_holds_no_numpy_surprises(env):
    """The screen renders whatever these return, so they must be plain types."""
    conn, repo, runs, _tmp = env
    summary = ms.detector_summary(repo, runs)
    assert not isinstance(summary.corpus_images, np.generic)


# -- the training environment, now owned by this screen ----------------------


def _install_fake_env(tmp_path):
    """An environment on disk plus the recorded interpreter, as setup leaves it."""
    from tcginventory.services import training, trainingenv

    env = tmp_path / trainingenv.ENV_DIRNAME
    (env / ("Scripts" if os.name == "nt" else "bin")).mkdir(parents=True)
    python = trainingenv.env_python(env)
    python.write_bytes(b"x" * 2048)
    training.remember_training_env(
        training.TrainingEnv(str(python), True, "8.4.115", "ultralytics 8.4.115")
    )
    return env


def test_the_environment_section_offers_a_switch_once_one_is_installed(
    app, env, tmp_path, monkeypatch
):
    """Building and switching are the same environment from opposite sides —
    with one in place there is nothing to *build*, only something to swap."""
    conn, repo, _runs, _tmp = env
    _install_fake_env(tmp_path)

    summary = ms.environment_summary()
    assert summary.ready
    assert summary.ultralytics == "8.4.115"
    assert summary.size_bytes > 0


def test_an_absent_environment_reads_as_not_set_up(app, env, monkeypatch):
    conn, repo, _runs, _tmp = env

    summary = ms.environment_summary()
    assert not summary.ready  # nothing installed in this tmp data dir


def test_removing_the_environment_keeps_the_models_and_says_so(tmp_path, monkeypatch):
    from tcginventory.services import modeljobs, trainingenv

    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    _install_fake_env(tmp_path)

    sentence = modeljobs.remove_training_env(None)

    assert not trainingenv.is_bootstrapped()
    assert "models kept" in sentence


def test_the_screen_refuses_to_train_without_an_environment(app, env, monkeypatch):
    """Training the detector needs the interpreter; saying so beats a failure
    twenty seconds into a background job."""
    conn, repo, _runs, _tmp = env

    from PySide6.QtWidgets import QMessageBox

    from tcginventory.ui.views.models import ModelsView

    told: list = []
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: told.append(a[2]))
    )
    view = ModelsView(conn)
    started: list = []
    monkeypatch.setattr(view, "_task", lambda *a, **k: started.append(a))

    view._train_detector()

    assert not started, "no job should start without an environment"
    assert told and "environment" in told[0].lower()
    view.deleteLater()


# -- feedback while a job runs -----------------------------------------------


def _view(conn, repo=None, monkeypatch=None):
    """The screen, reading the tmp data dir like the real one reads yours."""
    from tcginventory.ui.views.models import ModelsView

    return ModelsView(conn)


def _actions(view):
    from PySide6.QtWidgets import QPushButton

    return [b for b in view.findChildren(QPushButton) if b.property("action")]


def test_a_running_job_is_visible_and_locks_the_other_buttons(app, env, monkeypatch):
    """Training the detector takes minutes on a worker thread. Without this the
    screen looks identical before and after the click, which reads as a button
    that does nothing — which is exactly how it was first reported."""
    from PySide6.QtWidgets import QFrame

    conn, repo, _runs, _tmp = env
    view = _view(conn, repo, monkeypatch)
    assert any(b.isEnabled() for b in _actions(view))

    view._running = "models.detector.train"
    view.refresh()

    assert not any(b.isEnabled() for b in _actions(view))
    assert [f for f in view.findChildren(QFrame) if f.property("tone") == "busy"]

    view._running = None
    view.refresh()
    assert any(b.isEnabled() for b in _actions(view))
    view.deleteLater()


def test_starting_a_job_marks_the_screen_busy(app, env, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    conn, repo, _runs, _tmp = env
    view = _view(conn, repo, monkeypatch)
    # _task explains itself with a modal when there is nowhere to run a job, and
    # an unpatched modal blocks the whole run.
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(view, "_db_file", lambda: None)  # no real thread

    view._benchmark_identity()
    assert view._running is None  # refused: nowhere to run it

    started = {}
    monkeypatch.setattr(
        view, "_task", lambda op_id, job: started.setdefault("op", op_id)
    )
    view._benchmark_identity()
    assert started["op"] == "identity.benchmark"
    view.deleteLater()


def test_a_blocked_train_button_says_what_it_wants(app, env, monkeypatch):
    """A greyed button with no reason is indistinguishable from a broken one."""
    from PySide6.QtWidgets import QPushButton

    conn, repo, _runs, _tmp = env
    view = _view(conn, repo, monkeypatch)

    # With no detector installed its button also reads "Train"; the grader's is
    # the one that explains itself.
    train = [
        b for b in view.findChildren(QPushButton) if b.text() == "Train" and b.toolTip()
    ][0]
    assert not train.isEnabled()
    tip = train.toolTip()
    assert "more graded cards" in tip and "per grade" in tip
    view.deleteLater()


def test_every_job_the_screen_can_start_has_a_banner_of_its_own(app):
    """A generic "Working…" would leave you guessing which of six jobs is under
    way, and how long it might take."""
    from tcginventory.ui.views import models as mv

    started = [
        "models.detector.train",
        "models.condition.train",
        "identity.benchmark",
        "identity.fit_projection",
        "identity.index_catalog",
        "vision.reindex",
        "models.training_env",
        "models.training_env_gpu",
        "models.training_env_remove",
    ]
    for op_id in started:
        banner = mv._running_banner(op_id)
        labels = [
            child.text()
            for child in banner.children()
            if hasattr(child, "text") and child.text()
        ]
        assert labels, op_id
        assert "Working" not in labels[0], f"{op_id} falls back to the generic banner"


def test_a_retrain_that_lost_leaves_nothing_stale(env, tmp_path):
    """Regression, 2026-08-07: this is the loop.

    A retrain that sees the whole corpus can still be refused promotion. Drift
    used to be measured against the *promoted* run, so the moment that happened
    the detector was marked stale permanently — with a highlighted Retrain beside
    it — and no amount of retraining could clear it. The real corpus collected
    five identical runs that way. What the screen owes you here is "there is
    nothing left to train on", not "stale".
    """
    _conn, repo, runs, _tmp = env
    _install_model()
    _corpus(repo, tmp_path, images=1)
    served = runs.start(kind=KIND_DETECTOR, corpus_images=1, corpus_boxes=2)
    runs.finish(served, map50=0.99, map50_95=0.97, precision=0.95, recall=1.0)
    runs.promote(served)

    # More photos arrive, and a retrain sees all of them but scores lower.
    _corpus(repo, tmp_path, images=ms.STALE_IMAGE_DRIFT, card="c-2")
    loser = runs.start(kind=KIND_DETECTOR, corpus_images=5, corpus_boxes=10)
    runs.finish(loser, map50=0.98, map50_95=0.95, precision=0.94, recall=1.0)

    summary = ms.detector_summary(repo, runs)
    assert summary.state == "served"  # everything labelled has been trained on
    assert summary.unseen_images == 0  # so there is nothing to retrain *for*
    assert summary.evaluated_images == 5  # the losing run still saw all five
    assert summary.declined is not None  # but the screen can still explain itself
    assert summary.declined["id"] == loser
    assert summary.version == 1  # and the better model is still the one serving
    # The served model *is* older than the corpus, which is worth showing and is
    # never on its own a reason to retrain.
    assert summary.behind_images == 4


def test_a_run_that_beats_the_served_model_is_worth_promoting(env, tmp_path):
    """The migration case, and the one the real corpus is in.

    Every run refused under the old mAP50-95 comparison is still in the log, and
    the best of them beats the serving model on fitness — it lost on box
    tightness while winning on precision. That is worth a nudge, and unlike the
    old staleness flag this one clears the moment you retrain.
    """
    _conn, repo, runs, _tmp = env
    _install_model()
    _corpus(repo, tmp_path, images=3)
    served = runs.start(kind=KIND_DETECTOR, corpus_images=3, corpus_boxes=6)
    # v1: perfect recall, loose boxes, five points worse precision.
    runs.finish(served, map50=0.995, map50_95=0.970, precision=0.949, recall=1.0)
    runs.promote(served)
    # The refused run: same recall and mAP50, better precision, tighter-scoring
    # boxes marginally worse. The old gate compared only the last of those.
    better = runs.start(kind=KIND_DETECTOR, corpus_images=3, corpus_boxes=6)
    runs.finish(better, map50=0.995, map50_95=0.955, precision=0.999, recall=1.0)

    summary = ms.detector_summary(repo, runs)
    assert summary.declined_scored_higher
    assert summary.declined_fitness > summary.served_fitness
    assert summary.state == "stale"  # there is something better to be had
    assert summary.unseen_images == 0  # but not because of unseen labels


def test_labels_no_run_has_seen_are_still_stale(env, tmp_path):
    """The other half of the same rule: drift against the newest *finished* run
    must still fire when photos land after it."""
    _conn, repo, runs, _tmp = env
    _install_model()
    _corpus(repo, tmp_path, images=1)
    served = runs.start(kind=KIND_DETECTOR, corpus_images=1, corpus_boxes=2)
    runs.finish(served, map50=0.99, map50_95=0.97, precision=0.95, recall=1.0)
    runs.promote(served)

    _corpus(repo, tmp_path, images=ms.STALE_IMAGE_DRIFT, card="c-2")

    summary = ms.detector_summary(repo, runs)
    assert summary.state == "stale"
    assert summary.unseen_images == ms.STALE_IMAGE_DRIFT
    assert summary.declined is None


def test_no_declined_run_when_the_retrain_won(env, tmp_path):
    _conn, repo, runs, _tmp = env
    _install_model()
    _corpus(repo, tmp_path, images=5)
    first = runs.start(kind=KIND_DETECTOR, corpus_images=1, corpus_boxes=2)
    runs.finish(first, map50_95=0.90)
    runs.promote(first)
    better = runs.start(kind=KIND_DETECTOR, corpus_images=5, corpus_boxes=10)
    runs.finish(better, map50_95=0.96)
    runs.promote(better)

    assert ms.detector_summary(repo, runs).declined is None


# -- the result window -------------------------------------------------------


def test_a_promoted_detector_run_says_it_is_now_the_model(env, tmp_path):
    _conn, repo, runs, _tmp = env
    run = runs.start(kind=KIND_DETECTOR, corpus_images=39, corpus_boxes=152, epochs=100)
    runs.finish(run, map50=0.99, map50_95=0.97, precision=0.95, recall=1.0)
    runs.promote(run)

    result = ms.run_result(runs, KIND_DETECTOR)
    assert result.outcome == "Promoted as v1" and result.tone == "served"
    assert [m.label for m in result.headline] == [
        "fitness",
        "recall",
        "precision",
        "mAP50",
        "mAP50-95",
    ]
    assert "100 epochs on 39 images" in result.lines[0]
    assert "uses from now on" in result.lines[-1]


def test_a_losing_run_quotes_both_sides_on_the_same_validation_split(env):
    """The result window is where "not promoted" has to stop being cryptic — and
    the two numbers it quotes have to be measurements of the same thing.

    Comparing a candidate against the incumbent's *historical* score compared two
    different exams, because the validation split moved with the corpus. When the
    run re-scored the incumbent on its own split, that is the figure to show.
    """
    import json

    _conn, repo, runs, _tmp = env
    served = runs.start(kind=KIND_DETECTOR)
    runs.finish(served, map50=0.99, map50_95=0.97, precision=0.95, recall=1.0)
    runs.promote(served)
    loser = runs.start(kind=KIND_DETECTOR, corpus_images=39, corpus_boxes=152)
    runs.finish(
        loser,
        map50=0.99,
        map50_95=0.955,
        precision=0.90,
        recall=0.95,
        fitness=0.941,
        metrics_json=json.dumps(
            {
                "verdict": "it scored -0.020 on fitness, outside the ±0.004 band",
                "baseline_fitness": 0.961,
                "noise_floor": 0.004,
                "comparable": True,
            }
        ),
    )

    result = ms.run_result(runs, KIND_DETECTOR)
    assert result.outcome == "Not promoted" and result.tone == "stale"
    reason = " ".join(result.lines)
    assert "94.1%" in reason and "96.1%" in reason  # both sides, one exam
    assert "like-for-like" in reason


def test_a_losing_run_says_when_the_two_scores_were_never_comparable(env):
    """Runs from before the split was stabilised cannot be compared at all, and
    the window must say that rather than imply the gap is real."""
    import json

    _conn, repo, runs, _tmp = env
    served = runs.start(kind=KIND_DETECTOR)
    runs.finish(served, map50=0.99, map50_95=0.97, precision=0.95, recall=1.0)
    runs.promote(served)
    loser = runs.start(kind=KIND_DETECTOR, corpus_images=9, corpus_boxes=20)
    runs.finish(
        loser,
        map50=0.98,
        map50_95=0.95,
        fitness=0.93,
        metrics_json=json.dumps(
            {"verdict": "it saw less of the corpus", "comparable": False}
        ),
    )

    result = ms.run_result(runs, KIND_DETECTOR)
    assert "different validation split" in " ".join(result.lines)


def test_a_failed_run_is_reported_as_failed(env):
    _conn, repo, runs, _tmp = env
    run = runs.start(kind=KIND_DETECTOR)
    runs.finish(run, status="failed", notes="ultralytics is not installed")

    result = ms.run_result(runs, KIND_DETECTOR)
    assert result.outcome == "Failed" and result.tone == "blocked"
    assert not result.succeeded
    assert "ultralytics" in result.notes


def test_a_grader_that_loses_to_always_nm_says_so(env):
    """It can train successfully and still be worse than a constant guess. The
    window has to say that rather than call it a success."""
    _conn, repo, runs, _tmp = env
    run = runs.start(kind=KIND_CONDITION, corpus_cards=63)
    _finish_grader(runs, run, exact=0.14, baseline=0.61, distance=1.4)

    result = ms.run_result(runs, KIND_CONDITION)
    assert result.tone == "stale"
    assert "no better than guessing" in result.outcome
    assert "thin grades" in " ".join(result.lines)
    # Both figures, correctly sourced: exact accuracy and the real always-NM rate.
    assert [m.value for m in result.headline][:2] == ["14.0%", "61.0%"]


def test_a_grader_that_beats_the_baseline_is_worth_using(env):
    _conn, repo, runs, _tmp = env
    run = runs.start(kind=KIND_CONDITION, corpus_cards=120)
    _finish_grader(runs, run, exact=0.72, baseline=0.61, distance=0.5)

    result = ms.run_result(runs, KIND_CONDITION)
    assert result.outcome == "Trained" and result.tone == "served"
    assert "worth using" in " ".join(result.lines)


def test_a_grader_is_judged_on_the_baseline_not_on_macro_f1(env):
    """Regression, 2026-08-07: `precision` on a condition run holds macro-F1, and
    reading it as the always-NM baseline made this grader "beat guessing" — it
    was comparing 44% accuracy against an unrelated 12%."""
    _conn, repo, runs, _tmp = env
    run = runs.start(kind=KIND_CONDITION, corpus_cards=63)
    _finish_grader(runs, run, exact=0.44, baseline=0.61, macro_f1=0.12, distance=1.1)

    result = ms.run_result(runs, KIND_CONDITION)
    assert result.tone == "stale"
    assert "no better than guessing" in result.outcome


def test_the_benchmark_result_carries_the_layers_and_the_caveat(env):
    _conn, repo, runs, _tmp = env
    _identity_run(
        runs,
        {"encoder": 0.194, "scope": 0.672, "projection": 0.910, "band": 0.925},
        pool=49551,
        wrong_printing=2,
        wrong_card=3,
        in_sample_from="projection",
    )
    result = ms.run_result(runs, KIND_IDENTITY)

    assert result.outcome == "Benchmarked"
    assert [layer.label for layer in result.layers][0] == "DINOv2-small encoder"
    assert "+73.1 points" in " ".join(result.lines)
    assert "2 wrong printing and 3 wrong card" in " ".join(result.lines)
    assert result.caveat and "in-sample" in result.caveat


def test_a_clean_benchmark_carries_no_caveat(env):
    _conn, repo, runs, _tmp = env
    _identity_run(runs, {"encoder": 0.2, "band": 0.7})
    assert ms.run_result(runs, KIND_IDENTITY).caveat is None


def test_only_the_jobs_with_a_run_behind_them_open_the_window(app):
    """Indexing and building an environment report a sentence and nothing more,
    so they keep the plain message rather than an empty results window."""
    from tcginventory.ui.views import models as mv

    assert set(mv._RESULT_KINDS) == {
        "models.detector.train",
        "models.condition.train",
        "identity.benchmark",
    }
    for op_id in ("identity.index_catalog", "vision.reindex", "models.training_env"):
        assert op_id not in mv._RESULT_KINDS


def test_the_result_window_builds_for_every_kind(app, env):
    from tcginventory.ui.views.models import ResultDialog

    _conn, repo, runs, _tmp = env
    run = runs.start(kind=KIND_DETECTOR, corpus_images=3, corpus_boxes=6, epochs=10)
    runs.finish(run, map50=0.9, map50_95=0.8, precision=0.9, recall=0.9)
    runs.promote(run)
    _identity_run(runs, {"encoder": 0.2, "band": 0.7}, in_sample_from="projection")
    cond = runs.start(kind=KIND_CONDITION, corpus_cards=40)
    runs.finish(cond, map50=0.7, precision=0.6)

    for kind in (KIND_DETECTOR, KIND_CONDITION, KIND_IDENTITY):
        result = ms.run_result(runs, kind)
        dialog = ResultDialog(result)
        assert dialog.windowTitle().startswith(result.title)
        dialog.deleteLater()
