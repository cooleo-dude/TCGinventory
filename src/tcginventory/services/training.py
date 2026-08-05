"""Detector training: corpus snapshots, run status, and metric parsing.

The training itself runs **outside the app** (``training/train_detector.py`` needs
ultralytics/torch, deliberately not shipped). This module is the part both sides
share and that the Operations window reads:

* :func:`corpus_snapshot` — what the corpus currently holds, plus a
  **fingerprint** of the exact (image, box) set, so a model can be compared with
  the data as it stands now;
* :func:`detector_status` — is a model installed, which version, what it trained
  on, how it scored, and has the corpus moved on since (``stale``);
* :func:`parse_results_csv` — ultralytics' per-epoch metrics, so a run's curve and
  best epoch land in the run log;
* :func:`should_promote` — the gate that stops a worse model replacing a better
  one just because it trained later.

Qt-free and no SQL of its own (reads through the repositories), so it tests
headless.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from tcginventory import config
from tcginventory.data.vision import (
    KIND_DETECTOR,
    ModelRunRepository,
    VisionRepository,
)
from tcginventory.services import detector
from tcginventory.services.detector_dataset import export_yolo_dataset

# A box is fingerprinted at this precision — enough that nudging a box by a pixel
# counts as a change, loose enough that float noise doesn't.
_BOX_PLACES = 5

# The interpreter that has ultralytics installed. Stored as a preference because
# it is a property of the machine, not of the app: the frozen exe can't train in
# its own process (torch is unbundled by design), so it drives a
# training venv the user creates once.
TRAINING_PYTHON_PREF = "training_python"
# What probe_training_env() found when the environment was last validated, so
# the Operations window can report readiness without importing torch to ask.
TRAINING_VERSION_PREF = "training_env_version"

# What a training interpreter needs. onnx/onnxslim/onnxruntime are *not* part of
# a plain `pip install ultralytics` (they sit behind its export-base extra), and
# without them a run trains for minutes and then fails at the ONNX export.
TRAINING_REQUIREMENTS = ("ultralytics", "onnx", "onnxslim", "onnxruntime")
INSTALL_HINT = "pip install " + " ".join(TRAINING_REQUIREMENTS)

WORKER_FILENAME = "yolo_train_worker.py"
RESULT_PREFIX = "TCGI_RESULT "
DEFAULT_VAL_FRAC = 0.2
DEFAULT_SPLIT_SEED = 0


@dataclass(frozen=True)
class CorpusSnapshot:
    """What the detector could train on right now."""

    images: int  # images carrying at least one box
    boxes: int
    cards: int  # distinct identified cards (identifier reach, not detector)
    fingerprint: str  # hash of the (image, box) set

    @property
    def trainable(self) -> bool:
        """Whether there is anything at all to train on."""
        return self.images > 0 and self.boxes > 0


@dataclass(frozen=True)
class DetectorStatus:
    """The detector as the Operations window reports it."""

    installed: bool  # a model file is present and will be used
    model_path: str | None
    version: int | None  # promoted model version ("detector v3")
    trained_at: str | None
    trained_on: CorpusSnapshot | None  # the snapshot behind the current model
    corpus: CorpusSnapshot  # the corpus as it stands now
    map50: float | None
    map50_95: float | None
    precision: float | None
    recall: float | None
    last_status: str | None  # last attempt's outcome, even if not promoted
    last_notes: str | None

    @property
    def never_trained(self) -> bool:
        return self.version is None

    @property
    def age_days(self) -> float | None:
        """Days since the model in use finished training, if one ever did."""
        if not self.trained_at:
            return None
        from datetime import datetime, timezone

        try:
            stamp = datetime.fromisoformat(self.trained_at)
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds() / 86400

    @property
    def stale(self) -> bool:
        """True when the corpus has changed since the model was trained."""
        if self.trained_on is None:
            return self.corpus.trainable  # nothing trained yet, but data waiting
        return self.trained_on.fingerprint != self.corpus.fingerprint

    @property
    def new_images(self) -> int:
        base = self.trained_on.images if self.trained_on else 0
        return max(0, self.corpus.images - base)

    @property
    def new_boxes(self) -> int:
        base = self.trained_on.boxes if self.trained_on else 0
        return max(0, self.corpus.boxes - base)


def corpus_snapshot(repo: VisionRepository) -> CorpusSnapshot:
    """Count the trainable corpus and fingerprint its exact contents."""
    images = list(repo.images_with_regions())
    boxes = 0
    digest = hashlib.sha256()
    for img in sorted(images, key=lambda r: int(r["id"])):
        regions = repo.regions_for_image(img["id"])
        digest.update(f"i{int(img['id'])}:".encode())
        for r in regions:
            boxes += 1
            digest.update(
                (
                    f"{round(r['x'], _BOX_PLACES)},{round(r['y'], _BOX_PLACES)},"
                    f"{round(r['w'], _BOX_PLACES)},{round(r['h'], _BOX_PLACES)};"
                ).encode()
            )
    return CorpusSnapshot(
        images=len(images),
        boxes=boxes,
        cards=repo.labeled_card_count(),
        fingerprint=digest.hexdigest(),
    )


def detector_status(
    repo: VisionRepository, runs: ModelRunRepository | None = None
) -> DetectorStatus:
    """Assemble the detector's current state for display."""
    runs = runs or ModelRunRepository(repo.conn)
    now = corpus_snapshot(repo)
    current = runs.current_model(KIND_DETECTOR)
    last = runs.last_run(KIND_DETECTOR)
    model = detector.yolo_model_path()

    trained_on = None
    if current is not None:
        trained_on = CorpusSnapshot(
            images=int(current["corpus_images"] or 0),
            boxes=int(current["corpus_boxes"] or 0),
            cards=int(current["corpus_cards"] or 0),
            fingerprint=current["corpus_fingerprint"] or "",
        )
    return DetectorStatus(
        installed=model is not None,
        model_path=str(model) if model else None,
        version=int(current["version"]) if current and current["version"] else None,
        trained_at=current["finished_at"] if current else None,
        trained_on=trained_on,
        corpus=now,
        map50=current["map50"] if current else None,
        map50_95=current["map50_95"] if current else None,
        precision=current["precision"] if current else None,
        recall=current["recall"] if current else None,
        last_status=last["status"] if last else None,
        last_notes=last["notes"] if last else None,
    )


def should_promote(
    candidate: float | None, incumbent: float | None, *, margin: float = 0.0
) -> bool:
    """Whether a new model should replace the one in use.

    Compares mAP50-95. A first model always wins (nothing to lose against); a
    candidate with no measured score never does. ``margin`` demands a real
    improvement rather than noise.
    """
    if candidate is None:
        return False
    if incumbent is None:
        return True
    return candidate >= incumbent + margin


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    """Content hash of a model artifact — the run log's identity for it."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


# ultralytics results.csv column → our field. Names carry a "metrics/" prefix and
# a "(B)" box-task suffix; the loss columns are per-split.
_METRIC_COLUMNS = {
    "metrics/mAP50(B)": "map50",
    "metrics/mAP50-95(B)": "map50_95",
    "metrics/precision(B)": "precision",
    "metrics/recall(B)": "recall",
    "val/box_loss": "box_loss",
    "val/cls_loss": "cls_loss",
}


def parse_results_csv(path: str | Path) -> list[dict]:
    """ultralytics' ``results.csv`` → one dict per epoch.

    Unknown or missing columns are simply absent, so a change in their CSV layout
    degrades to fewer metrics rather than a crash.
    """
    import csv

    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            clean = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
            epoch = clean.get("epoch")
            if not epoch:
                continue
            row: dict = {"epoch": int(float(epoch))}
            for column, field in _METRIC_COLUMNS.items():
                value = clean.get(column)
                if value not in (None, ""):
                    try:
                        row[field] = float(value)
                    except ValueError:
                        pass
            rows.append(row)
    return rows


def best_epoch(epochs: list[dict], key: str = "map50_95") -> dict | None:
    """The epoch with the highest ``key`` — what the exported weights represent
    (ultralytics exports ``best.pt``, not the last epoch)."""
    scored = [e for e in epochs if e.get(key) is not None]
    return max(scored, key=lambda e: e[key]) if scored else None


# ---------------------------------------------------------------------------
# running a training job: the app orchestrates, a separate interpreter trains
# ---------------------------------------------------------------------------


class TrainingUnavailable(RuntimeError):
    """Training can't start — no data, no interpreter, or no worker script."""


@dataclass(frozen=True)
class TrainingEnv:
    """A candidate training interpreter and what it can do."""

    python: str | None
    ok: bool  # ultralytics imported cleanly
    version: str | None  # ultralytics version, when known
    detail: str  # human-readable state for the Operations tile

    @property
    def ready(self) -> bool:
        return bool(self.python) and self.ok


def worker_script() -> Path | None:
    """Locate the training worker — bundled beside the package when frozen, in the
    repo's ``training/`` directory when running from source."""
    try:
        res = resources.files("tcginventory") / "training" / WORKER_FILENAME
        bundled = Path(str(res))
        if bundled.is_file():
            return bundled
    except Exception:  # pragma: no cover - resource lookup edge cases
        pass
    # src/tcginventory/services/training.py -> repo root -> training/
    repo = Path(__file__).resolve().parents[3]
    candidate = repo / "training" / WORKER_FILENAME
    return candidate if candidate.is_file() else None


def training_python() -> str | None:
    """The configured training interpreter, if the user has set one."""
    value = config.get_pref(TRAINING_PYTHON_PREF)
    return str(value) if value else None


def set_training_python(path: str | None) -> None:
    """Remember (or clear) the interpreter used for training."""
    config.set_pref(TRAINING_PYTHON_PREF, str(path) if path else None)


def remember_training_env(env: TrainingEnv) -> None:
    """Record a validated environment so later reads are free."""
    set_training_python(env.python)
    config.set_pref(TRAINING_VERSION_PREF, env.version if env.ok else None)


def forget_training_env() -> None:
    """Drop the recorded interpreter and version — the environment is gone."""
    set_training_python(None)
    config.set_pref(TRAINING_VERSION_PREF, None)


def training_env_summary() -> TrainingEnv:
    """The saved environment, read from preferences only — **no subprocess**.

    :func:`probe_training_env` is the truth, but it imports ultralytics in the
    candidate interpreter (seconds, once torch is installed), which is far too slow
    for a status refresh. This trusts what setup recorded and only checks that the
    interpreter is still on disk.
    """
    path = training_python()
    if not path:
        return TrainingEnv(
            None, False, None, "not set up — no training interpreter recorded"
        )
    if not Path(path).is_file():
        return TrainingEnv(
            path, False, None, f"the recorded interpreter is gone: {path}"
        )
    version = config.get_pref(TRAINING_VERSION_PREF)
    if not version:
        return TrainingEnv(path, False, None, "recorded, but never validated")
    return TrainingEnv(path, True, str(version), f"ultralytics {version}")


def probe_training_env(python: str | None = None) -> TrainingEnv:
    """Ask an interpreter whether it can train, without importing torch here.

    Order: the argument, then the saved preference, then this very interpreter
    (which works from source once ultralytics is installed, and never in the
    frozen app). The check is a subprocess so a broken or very heavy install
    can't take the UI down with it.
    """
    candidate = python or training_python()
    if candidate is None and not getattr(sys, "frozen", False):
        candidate = sys.executable
    if not candidate:
        return TrainingEnv(
            None, False, None, "No training interpreter set — choose one to train."
        )
    if not Path(candidate).is_file():
        return TrainingEnv(candidate, False, None, f"Not found: {candidate}")
    try:
        proc = subprocess.run(
            [candidate, "-c", "import ultralytics;print(ultralytics.__version__)"],
            capture_output=True,
            text=True,
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return TrainingEnv(candidate, False, None, f"Could not run it: {exc}")
    if proc.returncode != 0:
        return TrainingEnv(
            candidate,
            False,
            None,
            "ultralytics is not installed in this interpreter — "
            f"{INSTALL_HINT} (~280 MB, one time).",
        )
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    version = lines[-1] if lines else ""
    return TrainingEnv(candidate, True, version, f"ultralytics {version}")


@dataclass(frozen=True)
class TrainingJob:
    """A prepared run: the log row is open and the command is ready to launch."""

    run_id: int
    command: list[str]
    dataset_dir: Path
    data_yaml: Path
    train_images: int
    val_images: int
    boxes: int
    out_path: Path


def prepare_detector_training(
    repo: VisionRepository,
    runs: ModelRunRepository,
    *,
    env: TrainingEnv,
    dataset_dir: str | Path,
    out_path: str | Path,
    epochs: int = 100,
    imgsz: int = 640,
    weights: str = "yolov8n.pt",
    source: str = "ui",
    val_frac: float = DEFAULT_VAL_FRAC,
    seed: int = DEFAULT_SPLIT_SEED,
) -> TrainingJob:
    """Export the dataset, open a run row, and build the worker command.

    Everything here is app-side and torch-free; the caller launches
    ``job.command`` (``QProcess`` in the UI, ``subprocess`` in the CLI) and then
    calls :func:`complete_detector_training` or :func:`fail_detector_training`.

    Raises :class:`TrainingUnavailable` when there's nothing to train on or
    nothing to train with — checked *before* a run is logged, so the history
    doesn't fill up with attempts that never started.
    """
    if not env.ready:
        raise TrainingUnavailable(env.detail)
    worker = worker_script()
    if worker is None:
        raise TrainingUnavailable(f"{WORKER_FILENAME} is missing from this install.")

    snapshot = corpus_snapshot(repo)
    if not snapshot.trainable:
        raise TrainingUnavailable(
            "No labeled boxes yet — draw and save boxes in the Recognition window."
        )

    summary = export_yolo_dataset(repo, dataset_dir, val_frac=val_frac, seed=seed)
    if summary.images == 0:
        raise TrainingUnavailable(
            "The corpus has labels but no readable images "
            f"({summary.skipped_missing} source files are missing)."
        )

    out = Path(out_path)
    run_id = runs.start(
        kind=KIND_DETECTOR,
        source=source,
        corpus_images=snapshot.images,
        corpus_boxes=snapshot.boxes,
        corpus_cards=snapshot.cards,
        corpus_fingerprint=snapshot.fingerprint,
        base_weights=weights,
        epochs=epochs,
        imgsz=imgsz,
        val_frac=val_frac,
        split_seed=seed,
    )
    command = [
        str(env.python),
        str(worker),
        "--data",
        str(summary.data_yaml),
        "--out",
        str(out),
        "--weights",
        weights,
        "--epochs",
        str(epochs),
        "--imgsz",
        str(imgsz),
        "--project",
        str(Path(dataset_dir) / "runs"),
        "--name",
        f"run{run_id}",
    ]
    return TrainingJob(
        run_id=run_id,
        command=command,
        dataset_dir=Path(dataset_dir),
        data_yaml=Path(summary.data_yaml),
        train_images=summary.train,
        val_images=summary.val,
        boxes=summary.boxes,
        out_path=out,
    )


def parse_worker_result(output: str) -> dict:
    """Pull the worker's ``TCGI_RESULT {...}`` line out of its output."""
    for line in reversed((output or "").splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                return json.loads(line[len(RESULT_PREFIX) :])
            except json.JSONDecodeError:  # pragma: no cover - malformed line
                return {}
    return {}


@dataclass(frozen=True)
class TrainingOutcome:
    """What a finished run produced, as the caller should report it."""

    run_id: int
    promoted: bool
    version: int | None
    map50: float | None
    map50_95: float | None
    epochs: int
    summary: str


def complete_detector_training(
    job: TrainingJob,
    runs: ModelRunRepository,
    *,
    worker_output: str = "",
    promote_margin: float = 0.0,
    force_promote: bool = False,
) -> TrainingOutcome:
    """Record a finished run: metrics, artifact, and the promotion decision."""
    result = parse_worker_result(worker_output)
    onnx = Path(result.get("onnx") or job.out_path)
    save_dir = result.get("save_dir") or ""

    epochs: list[dict] = []
    csv_path = Path(save_dir) / "results.csv" if save_dir else None
    if csv_path is not None and csv_path.is_file():
        epochs = parse_results_csv(csv_path)
        runs.add_epochs(job.run_id, epochs)
    best = best_epoch(epochs) or {}

    incumbent = runs.current_model(KIND_DETECTOR)
    incumbent_score = incumbent["map50_95"] if incumbent else None

    runs.finish(
        job.run_id,
        status="ok",
        train_images=job.train_images,
        val_images=job.val_images,
        map50=best.get("map50"),
        map50_95=best.get("map50_95"),
        precision=best.get("precision"),
        recall=best.get("recall"),
        best_epoch=best.get("epoch"),
        artifact_path=str(onnx),
        artifact_sha256=sha256_file(onnx) if onnx.is_file() else None,
        artifact_bytes=onnx.stat().st_size if onnx.is_file() else None,
        notes=f"{len(epochs)} epochs recorded",
    )

    promote = force_promote or should_promote(
        best.get("map50_95"), incumbent_score, margin=promote_margin
    )
    version = runs.promote(job.run_id) if promote else None
    if promote:
        summary = f"Trained detector v{version}"
        if best.get("map50_95") is not None:
            summary += f" — mAP50-95 {best['map50_95']:.3f}"
    else:
        summary = (
            f"Not promoted — mAP50-95 {best.get('map50_95')} does not beat the "
            f"model in use ({incumbent_score})"
        )
    return TrainingOutcome(
        run_id=job.run_id,
        promoted=promote,
        version=version,
        map50=best.get("map50"),
        map50_95=best.get("map50_95"),
        epochs=len(epochs),
        summary=summary,
    )


def fail_detector_training(
    job: TrainingJob, runs: ModelRunRepository, reason: str
) -> None:
    """Close a run that died, was cancelled, or never produced a model."""
    runs.finish(job.run_id, status="failed", notes=(reason or "")[:500])
