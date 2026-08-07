"""Detector training: the run log, corpus snapshots, and launching a run.

The launch tests drive the real orchestration through a **stub worker** — a
torch-free stand-in that writes the same ``results.csv`` / ONNX / ``TCGI_RESULT``
the trainer does — so the whole app-side path is covered on a machine that will
never have ultralytics installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tcginventory.data.vision import (
    CardLabel,
    ModelRunRepository,
    VisionRepository,
    open_vision,
)
from tcginventory.services import training


@pytest.fixture()
def repo():
    conn = open_vision(":memory:")
    yield VisionRepository(conn)
    conn.close()


@pytest.fixture()
def runs(repo):
    return ModelRunRepository(repo.conn)


def _capture(repo, boxes, *, card_ids=("cel-4",), now="2026-08-01T00:00:00+00:00"):
    """One capture, one image, ``boxes`` regions labeled round-robin from ids."""
    cap = repo.add_capture(source="manual", now=now)
    img = repo.add_image(cap, f"sha{cap}", f"/scene{cap}.png", "front", now=now)
    for i, box in enumerate(boxes):
        cid = card_ids[i % len(card_ids)]
        repo.add_region(
            img, i + 1, box, None, CardLabel(card_id=cid, name=cid), now=now
        )
    return cap, img


def _scene_capture(
    repo, tmp_path, boxes=((0.05, 0.1, 0.3, 0.7), (0.55, 0.1, 0.3, 0.7))
):
    """A capture whose image really exists on disk — the dataset exporter copies
    the file, and silently skips regions whose source photo is gone."""
    from PIL import Image

    scene = Path(tmp_path) / "scene.png"
    Image.new("RGB", (240, 160), color=(200, 200, 200)).save(scene)
    cap = repo.add_capture(source="manual", now="2026-08-01T00:00:00+00:00")
    img = repo.add_image(cap, "sha-scene", str(scene), "front")
    for i, box in enumerate(boxes):
        repo.add_region(img, i + 1, box, None, CardLabel(card_id=f"c{i}", name=f"C{i}"))
    return cap, img


# -- corpus snapshot + fingerprint ------------------------------------------


def test_snapshot_counts_what_can_be_trained_on(repo):
    _capture(repo, [(0.0, 0.0, 0.2, 0.3), (0.3, 0.0, 0.2, 0.3)], card_ids=("a", "b"))
    snap = training.corpus_snapshot(repo)
    assert (snap.images, snap.boxes, snap.cards) == (1, 2, 2)
    assert snap.trainable


def test_empty_corpus_is_not_trainable(repo):
    snap = training.corpus_snapshot(repo)
    assert (snap.images, snap.boxes) == (0, 0)
    assert not snap.trainable


def test_fingerprint_is_stable_and_changes_with_the_boxes(repo):
    _capture(repo, [(0.0, 0.0, 0.2, 0.3)])
    first = training.corpus_snapshot(repo).fingerprint
    assert training.corpus_snapshot(repo).fingerprint == first  # stable

    _capture(repo, [(0.5, 0.5, 0.2, 0.3)])  # more data → different corpus
    assert training.corpus_snapshot(repo).fingerprint != first


def test_fingerprint_ignores_relabeling(repo):
    """Renaming a card doesn't change what the *detector* trains on."""
    _capture(repo, [(0.1, 0.1, 0.2, 0.3)])
    before = training.corpus_snapshot(repo).fingerprint
    repo.conn.execute("UPDATE region SET card_id = 'other', name = 'Other'")
    repo.conn.commit()
    assert training.corpus_snapshot(repo).fingerprint == before


# -- the run log -------------------------------------------------------------


def test_a_run_is_recorded_before_training_so_a_crash_leaves_a_trace(repo, runs):
    _capture(repo, [(0.0, 0.0, 0.2, 0.3)])
    snap = training.corpus_snapshot(repo)
    run_id = runs.start(
        corpus_images=snap.images,
        corpus_boxes=snap.boxes,
        corpus_cards=snap.cards,
        corpus_fingerprint=snap.fingerprint,
    )
    row = runs.get(run_id)
    assert row["status"] == "running"
    assert row["version"] is None and row["promoted"] == 0
    assert row["corpus_fingerprint"] == snap.fingerprint


def test_finish_failed_keeps_the_reason(runs):
    run_id = runs.start()
    runs.finish(run_id, status="failed", notes="ultralytics is not installed")
    row = runs.get(run_id)
    assert row["status"] == "failed"
    assert "ultralytics" in row["notes"]
    assert runs.current_model() is None  # a failure never becomes the model


def test_promotion_assigns_incrementing_versions(runs):
    first, second = runs.start(), runs.start()
    runs.finish(first, map50_95=0.20)
    runs.finish(second, map50_95=0.45)
    assert runs.promote(first) == 1
    assert runs.promote(second) == 2
    assert runs.current_model()["id"] == second  # highest version wins


def test_epoch_curve_and_best_epoch(runs):
    run_id = runs.start()
    rows = [
        {"epoch": 1, "map50": 0.10, "map50_95": 0.04, "box_loss": 2.5},
        {"epoch": 2, "map50": 0.55, "map50_95": 0.31, "box_loss": 1.2},
        {"epoch": 3, "map50": 0.50, "map50_95": 0.28, "box_loss": 1.1},
    ]
    assert runs.add_epochs(run_id, rows) == 3
    stored = runs.epochs(run_id)
    assert [r["epoch"] for r in stored] == [1, 2, 3]
    assert training.best_epoch(rows)["epoch"] == 2


def test_both_losses_survive_the_round_trip(runs):
    """The plot needs the training loss, so it has to reach the database."""
    run_id = runs.start()
    runs.add_epochs(
        run_id,
        [
            {
                "epoch": 1,
                "box_loss": 0.43,
                "cls_loss": 2.4,
                "train_box_loss": 1.22,
                "train_cls_loss": 2.67,
            }
        ],
    )
    row = runs.epochs(run_id)[0]
    assert row["box_loss"] == 0.43 and row["train_box_loss"] == 1.22
    assert row["cls_loss"] == 2.4 and row["train_cls_loss"] == 2.67


def test_an_older_database_gains_the_training_loss_columns(tmp_path):
    """The columns arrived after runs already existed, so opening an old vision
    DB has to add them rather than fail."""
    import sqlite3

    from tcginventory.data import vision as v

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE model_run (id INTEGER PRIMARY KEY, kind TEXT, status TEXT,"
        " source TEXT, started_at TEXT, created_at TEXT);"
        "CREATE TABLE model_epoch (run_id INTEGER, epoch INTEGER, map50 REAL,"
        " map50_95 REAL, precision REAL, recall REAL, box_loss REAL,"
        " cls_loss REAL, PRIMARY KEY (run_id, epoch));"
    )
    conn.commit()
    conn.close()

    reopened = v.open_vision(path)
    cols = {r[1] for r in reopened.execute("PRAGMA table_info(model_epoch)")}
    assert {"train_box_loss", "train_cls_loss"} <= cols
    reopened.close()  # not the last one


def test_accuracy_series_tracks_promoted_models_oldest_first(runs):
    """Charts ``fitness``, not ``map50_95``. That column is mean grade *distance*
    on a condition run — lower is better — so plotting it as an accuracy trend
    drew the grader's improvement upside down."""
    for score in (0.20, 0.31, 0.29):
        run_id = runs.start()
        runs.finish(run_id, fitness=score)
        runs.promote(run_id)
    assert runs.accuracy_series() == [(1, 0.20), (2, 0.31), (3, 0.29)]


# -- detector status (what Operations displays) ------------------------------


def test_status_with_nothing_trained_reports_waiting_data(repo, runs):
    _capture(repo, [(0.0, 0.0, 0.2, 0.3)])
    status = training.detector_status(repo, runs)
    assert status.never_trained
    assert status.version is None
    assert status.stale  # data is waiting to be trained on
    assert status.corpus.boxes == 1
    assert status.new_boxes == 1


def test_status_after_a_promoted_run_is_current_then_goes_stale(repo, runs):
    _capture(repo, [(0.0, 0.0, 0.2, 0.3)])
    snap = training.corpus_snapshot(repo)
    run_id = runs.start(
        corpus_images=snap.images,
        corpus_boxes=snap.boxes,
        corpus_cards=snap.cards,
        corpus_fingerprint=snap.fingerprint,
    )
    runs.finish(run_id, map50=0.6, map50_95=0.3)
    runs.promote(run_id)

    status = training.detector_status(repo, runs)
    assert status.version == 1
    assert not status.stale  # trained on exactly what the corpus holds
    assert (status.map50, status.map50_95) == (0.6, 0.3)
    assert status.trained_on.boxes == 1

    _capture(repo, [(0.5, 0.0, 0.2, 0.3), (0.7, 0.0, 0.2, 0.3)])  # new labels land
    later = training.detector_status(repo, runs)
    assert later.stale
    assert (later.new_images, later.new_boxes) == (1, 2)


def test_status_reports_whether_the_model_file_is_installed(repo, runs, monkeypatch):
    from tcginventory.services import detector

    monkeypatch.setattr(detector, "yolo_model_path", lambda: None)
    assert training.detector_status(repo, runs).installed is False

    monkeypatch.setattr(detector, "yolo_model_path", lambda: "card_yolo.onnx")
    status = training.detector_status(repo, runs)
    assert status.installed is True and status.model_path == "card_yolo.onnx"


# -- promotion gate ----------------------------------------------------------


def test_promotion_gate():
    assert training.should_promote(0.31, None)  # first model always wins
    assert training.should_promote(0.31, 0.30)
    assert not training.should_promote(0.29, 0.30)  # worse never replaces better
    assert not training.should_promote(None, 0.30)  # unmeasured never replaces
    assert not training.should_promote(0.305, 0.30, margin=0.02)  # noise, not gain
    assert training.should_promote(0.33, 0.30, margin=0.02)


def test_an_unmeasured_first_model_still_wins():
    """Regression: it lost to nothing at all, so a working detector stayed
    unpromoted and Operations reported it as never trained."""
    assert training.should_promote(None, None)


# -- ultralytics results.csv parsing ----------------------------------------


def test_parse_results_csv(tmp_path):
    csv_path = tmp_path / "results.csv"
    csv_path.write_text(
        "epoch,train/box_loss,metrics/precision(B),metrics/recall(B),"
        "metrics/mAP50(B),metrics/mAP50-95(B),val/box_loss\n"
        "1,2.5,0.40,0.35,0.10,0.04,2.40\n"
        "2,1.2,0.72,0.61,0.55,0.31,1.15\n",
        encoding="utf-8",
    )
    rows = training.parse_results_csv(csv_path)
    assert [r["epoch"] for r in rows] == [1, 2]
    assert rows[1]["map50_95"] == 0.31
    assert rows[1]["precision"] == 0.72
    assert rows[1]["box_loss"] == 1.15  # the val loss...
    assert rows[1]["train_box_loss"] == 1.2  # ...kept apart from the train loss
    assert training.best_epoch(rows)["epoch"] == 2


def test_the_training_loss_is_recorded_because_the_val_loss_stair_steps():
    """ultralytics does not validate on every epoch — it repeats the previous
    row's val figures on the ones it skips, so a curve drawn from `val/*` is a
    stair of duplicate pairs. `train/*` moves every epoch, so both are stored and
    the honest one is available to plot."""
    import csv
    import io

    # Shaped exactly like a real results.csv: val repeats, train does not.
    text = (
        "epoch,train/box_loss,train/cls_loss,metrics/mAP50(B),val/box_loss\n"
        "1,1.22,2.67,0.47,0.43\n"
        "2,1.13,2.61,0.45,0.48\n"
        "3,1.08,2.58,0.45,0.48\n"
        "4,1.17,2.65,0.37,0.52\n"
        "5,1.09,2.71,0.37,0.52\n"
    )
    assert len(list(csv.DictReader(io.StringIO(text)))) == 5

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "results.csv"
        path.write_text(text, encoding="utf-8")
        rows = training.parse_results_csv(path)

    val = [r["box_loss"] for r in rows]
    train = [r["train_box_loss"] for r in rows]
    assert len(set(val)) == 3, "the val column really does repeat"
    assert len(set(train)) == 5, "the train column moves every epoch"
    assert [r["train_cls_loss"] for r in rows][0] == 2.67


def test_parse_results_csv_tolerates_unknown_columns(tmp_path):
    csv_path = tmp_path / "results.csv"
    csv_path.write_text(
        "epoch,something/new,metrics/mAP50(B)\n1,9.9,0.42\n", encoding="utf-8"
    )
    rows = training.parse_results_csv(csv_path)
    assert rows == [{"epoch": 1, "map50": 0.42}]


def test_sha256_file_matches_the_whole_file_digest(tmp_path):
    """Chunked reading must give the same digest as hashing the bytes outright —
    the artifact hash is how a run identifies the model it produced."""
    import hashlib

    blob = b"onnx-weights" * 100_000  # spans several read chunks
    p = tmp_path / "model.onnx"
    p.write_bytes(blob)
    assert training.sha256_file(p) == hashlib.sha256(blob).hexdigest()


# -- launching a run: app orchestrates, another interpreter trains -----------


_STUB_WORKER = '''"""Stand-in for the trainer: no torch, writes what it would write."""
import argparse, json, sys
from pathlib import Path

p = argparse.ArgumentParser()
for flag in ("--data", "--out", "--weights", "--project", "--name", "--device",
             "--baseline"):
    p.add_argument(flag)
for flag in ("--epochs", "--imgsz", "--batch"):
    p.add_argument(flag, type=int)
a = p.parse_args()

save_dir = Path(a.project or ".") / (a.name or "run")
save_dir.mkdir(parents=True, exist_ok=True)
(save_dir / "results.csv").write_text(
    "epoch,metrics/precision(B),metrics/recall(B),metrics/mAP50(B),"
    "metrics/mAP50-95(B),val/box_loss\\n"
    "1,0.40,0.30,0.12,0.05,2.40\\n"
    "2,0.71,0.62,0.58,0.34,1.10\\n",
    encoding="utf-8",
)
out = Path(a.out)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(b"onnx-model-bytes")
print("stub epochs done")
print("TCGI_RESULT " + json.dumps({"save_dir": str(save_dir), "onnx": str(out)}))
'''


@pytest.fixture()
def stub_worker(tmp_path, monkeypatch):
    """Point the orchestration at a torch-free stand-in for the trainer."""
    path = tmp_path / "stub_worker.py"
    path.write_text(_STUB_WORKER, encoding="utf-8")
    monkeypatch.setattr(training, "worker_script", lambda: path)
    return path


@pytest.fixture()
def ready_env():
    import sys

    return training.TrainingEnv(sys.executable, True, "stub", "stub env")


def test_worker_script_is_found_in_the_repo():
    """From source it resolves in training/; frozen it comes from the bundle."""
    found = training.worker_script()
    assert found is not None and found.name == training.WORKER_FILENAME


def test_prepare_refuses_without_an_interpreter(repo, runs):
    _capture(repo, [(0.0, 0.0, 0.2, 0.3)])
    env = training.TrainingEnv(None, False, None, "No training interpreter set")
    with pytest.raises(training.TrainingUnavailable, match="No training interpreter"):
        training.prepare_detector_training(
            repo, runs, env=env, dataset_dir="unused", out_path="unused.onnx"
        )
    assert runs.recent() == []  # nothing logged: the run never started


def test_prepare_refuses_with_an_empty_corpus(
    repo, runs, ready_env, stub_worker, tmp_path
):
    with pytest.raises(training.TrainingUnavailable, match="No labeled boxes"):
        training.prepare_detector_training(
            repo,
            runs,
            env=ready_env,
            dataset_dir=tmp_path / "ds",
            out_path=tmp_path / "m.onnx",
        )
    assert runs.recent() == []


def test_prepare_logs_the_run_and_builds_the_command(
    repo, runs, ready_env, stub_worker, tmp_path
):
    _scene_capture(repo, tmp_path)
    job = training.prepare_detector_training(
        repo,
        runs,
        env=ready_env,
        dataset_dir=tmp_path / "ds",
        out_path=tmp_path / "card_yolo.onnx",
        epochs=2,
        source="ui",
    )
    row = runs.get(job.run_id)
    assert row["status"] == "running" and row["source"] == "ui"
    assert row["corpus_boxes"] == 2
    assert str(stub_worker) in job.command
    assert "--epochs" in job.command and "2" in job.command
    assert job.train_images + job.val_images == 1


def test_a_full_run_records_metrics_and_promotes(
    repo, runs, ready_env, stub_worker, tmp_path
):
    """End to end through a real subprocess: prepare → run → complete."""
    import subprocess

    _scene_capture(repo, tmp_path)
    job = training.prepare_detector_training(
        repo,
        runs,
        env=ready_env,
        dataset_dir=tmp_path / "ds",
        out_path=tmp_path / "card_yolo.onnx",
        epochs=2,
    )
    proc = subprocess.run(job.command, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    outcome = training.complete_detector_training(job, runs, worker_output=proc.stdout)
    assert outcome.promoted and outcome.version == 1
    assert outcome.map50_95 == 0.34  # the best epoch, not the last
    assert outcome.epochs == 2
    assert "v1" in outcome.summary

    row = runs.current_model()
    assert row["status"] == "ok"
    assert row["best_epoch"] == 2
    assert row["artifact_sha256"] and row["artifact_bytes"] == 16
    assert len(runs.epochs(job.run_id)) == 2

    # The model the app will load is exactly the artifact that was recorded.
    assert Path(row["artifact_path"]).read_bytes() == b"onnx-model-bytes"


def test_metrics_are_found_when_the_worker_reports_no_save_dir(
    repo, runs, ready_env, stub_worker, tmp_path
):
    """The 2026-08-06 regression: ultralytics 8.4 dropped ``save_dir`` from what
    ``model.train()`` returns, the worker reported an empty one, and the app read
    no metrics at all — so a 0.970 mAP50-95 detector went unpromoted. The app
    chose the run directory itself, so it must fall back to looking there."""
    import json
    import subprocess

    _scene_capture(repo, tmp_path)
    job = training.prepare_detector_training(
        repo,
        runs,
        env=ready_env,
        dataset_dir=tmp_path / "ds",
        out_path=tmp_path / "card_yolo.onnx",
        epochs=2,
    )
    proc = subprocess.run(job.command, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    blinded = "TCGI_RESULT " + json.dumps(
        {"save_dir": "", "onnx": str(job.out_path)}  # what 8.4 produced
    )

    outcome = training.complete_detector_training(job, runs, worker_output=blinded)
    assert outcome.epochs == 2  # read from where we told the worker to write
    assert outcome.map50_95 == 0.34
    assert outcome.promoted and outcome.version == 1


def test_a_run_that_produced_no_model_file_is_not_promoted(
    repo, runs, ready_env, stub_worker, tmp_path
):
    """Promotion means "this is the model in use". With no artifact there is
    nothing to use, however the run reports itself."""
    import subprocess

    _scene_capture(repo, tmp_path)
    job = training.prepare_detector_training(
        repo,
        runs,
        env=ready_env,
        dataset_dir=tmp_path / "ds",
        out_path=tmp_path / "card_yolo.onnx",
        epochs=2,
    )
    proc = subprocess.run(job.command, capture_output=True, text=True)
    job.out_path.unlink()  # the export vanished / was never written

    outcome = training.complete_detector_training(job, runs, worker_output=proc.stdout)
    assert not outcome.promoted and outcome.version is None
    assert "no model file" in outcome.summary.lower()
    assert runs.current_model() is None


def test_worker_takes_save_dir_from_the_trainer_not_the_metrics():
    """The trainer has always carried ``save_dir``; the metrics object stopped
    doing so in ultralytics 8.4. Reading the wrong one loses every metric, and
    silently — a missing attribute defaults to "" and the caller looks nowhere."""
    import importlib.util

    path = training.worker_script()
    spec = importlib.util.spec_from_file_location("yolo_train_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class _Model:  # ultralytics 8.4: metrics carry no save_dir at all
        class trainer:
            save_dir = "/runs/run7"

    class _Legacy:  # older ultralytics: the result object knows
        save_dir = "/runs/old"

    assert module._save_dir(_Model(), object()) == "/runs/run7"
    assert module._save_dir(object(), _Legacy()) == "/runs/old"
    assert module._save_dir(object(), object()) == ""  # nothing to report


def test_a_worse_second_run_is_not_promoted(
    repo, runs, ready_env, stub_worker, tmp_path
):
    from tcginventory.services import fitness as fit

    _scene_capture(repo, tmp_path)
    # A strong incumbent, recorded the way a real run now records itself: scored
    # on the same split policy the candidate will use, so the two are genuinely
    # comparable. Without a policy they are held incomparable and the corpus
    # decides instead — which is correct, but a different rule from this one.
    first = runs.start(split_policy=fit.SPLIT_HASHED_V1)
    runs.finish(
        first, map50=0.99, map50_95=0.90, precision=0.98, recall=1.0, fitness=0.90
    )
    runs.promote(first)

    import subprocess

    job = training.prepare_detector_training(
        repo,
        runs,
        env=ready_env,
        dataset_dir=tmp_path / "ds",
        out_path=tmp_path / "card_yolo.onnx",
        epochs=2,
    )
    proc = subprocess.run(job.command, capture_output=True, text=True)
    outcome = training.complete_detector_training(job, runs, worker_output=proc.stdout)

    assert not outcome.promoted and outcome.version is None
    assert "Not promoted" in outcome.summary
    assert runs.current_model()["id"] == first  # the good model stays in use
    assert runs.get(job.run_id)["status"] == "ok"  # the run itself succeeded


def test_a_dead_worker_is_recorded_as_failed(
    repo, runs, ready_env, stub_worker, tmp_path
):
    _scene_capture(repo, tmp_path)
    job = training.prepare_detector_training(
        repo,
        runs,
        env=ready_env,
        dataset_dir=tmp_path / "ds",
        out_path=tmp_path / "m.onnx",
        epochs=2,
    )
    training.fail_detector_training(job, runs, "cancelled by the user")
    row = runs.get(job.run_id)
    assert row["status"] == "failed" and "cancelled" in row["notes"]
    assert runs.current_model() is None


def test_parse_worker_result_ignores_ordinary_output():
    assert training.parse_worker_result("noise\nmore noise") == {}
    out = 'epoch 1/2\nTCGI_RESULT {"onnx": "m.onnx", "save_dir": "d"}\ndone'
    assert training.parse_worker_result(out)["onnx"] == "m.onnx"


def test_probe_reports_a_missing_interpreter(tmp_path):
    env = training.probe_training_env(str(tmp_path / "nope.exe"))
    assert not env.ready and "Not found" in env.detail


def test_probe_reports_an_interpreter_without_ultralytics():
    import sys

    env = training.probe_training_env(sys.executable)
    # This venv deliberately has no ultralytics — that's the message users see.
    assert not env.ready
    assert "ultralytics" in env.detail


def test_training_python_preference_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    assert training.training_python() is None
    training.set_training_python(r"C:\trainvenv\Scripts\python.exe")
    assert training.training_python() == r"C:\trainvenv\Scripts\python.exe"
    training.set_training_python(None)
    assert training.training_python() is None
