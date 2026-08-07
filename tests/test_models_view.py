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
    vision = open_vision(":memory:")
    yield conn, VisionRepository(vision), ModelRunRepository(vision), tmp_path
    conn.close()
    vision.close()


def _install_model() -> None:
    """A promoted run only counts as *serving* if its file is actually there."""
    from tcginventory.services import detector

    path = detector.trained_model_destination()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a real onnx, but a real file")


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
    assert [m.value for m in summary.metrics][:2] == ["99.0%", "95.0%"]


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
    runs.finish(run, map50=0.61, precision=0.44)
    runs.promote(run)

    summary = ms.condition_summary(repo, runs)
    assert summary.trained and summary.state == "served"
    assert [m.value for m in summary.metrics] == ["61.0%", "44.0%"]


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
    vision = repo.conn
    monkeypatch.setattr("tcginventory.data.vision.open_vision", lambda *a, **k: vision)
    _install_fake_env(tmp_path)

    summary = ms.environment_summary()
    assert summary.ready
    assert summary.ultralytics == "8.4.115"
    assert summary.size_bytes > 0


def test_an_absent_environment_reads_as_not_set_up(app, env, monkeypatch):
    conn, repo, _runs, _tmp = env
    vision = repo.conn
    monkeypatch.setattr("tcginventory.data.vision.open_vision", lambda *a, **k: vision)

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
    vision = repo.conn
    monkeypatch.setattr("tcginventory.data.vision.open_vision", lambda *a, **k: vision)

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
