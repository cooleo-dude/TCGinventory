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
    DetectorRunRepository,
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
    return DetectorRunRepository(repo.conn)


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
    assert training.best_epoch(rows)["epoch"] == 2  # not the last one


def test_accuracy_series_tracks_promoted_models_oldest_first(runs):
    for score in (0.20, 0.31, 0.29):
        run_id = runs.start()
        runs.finish(run_id, map50_95=score)
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
    assert rows[1]["box_loss"] == 1.15  # the val loss, not train
    assert training.best_epoch(rows)["epoch"] == 2


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
for flag in ("--data", "--out", "--weights", "--project", "--name", "--device"):
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
    """End to end through a real subprocess: prepare â†’ run â†’ complete."""
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


def test_a_worse_second_run_is_not_promoted(
    repo, runs, ready_env, stub_worker, tmp_path
):
    _scene_capture(repo, tmp_path)
    first = runs.start()
    runs.finish(first, map50_95=0.90)
    runs.promote(first)  # a strong incumbent

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
    # This venv deliberately has no ultralytics â€” that's the message users see.
    assert not env.ready
    assert "ultralytics" in env.detail


def test_training_python_preference_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    assert training.training_python() is None
    training.set_training_python(r"C:\trainvenv\Scripts\python.exe")
    assert training.training_python() == r"C:\trainvenv\Scripts\python.exe"
    training.set_training_python(None)
    assert training.training_python() is None
