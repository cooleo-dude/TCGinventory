"""Train the single-class 'card' detector from the command line (spec 06, M-V1.5).

Thin wrapper over the *same* orchestration the Operations window uses
(``services.training``): export the labeled regions to a YOLO dataset, open a row
in the ``detector_run`` log, run :mod:`yolo_train_worker` in an interpreter that
has ultralytics, then record the metrics, hash the artifact and apply the
promotion gate. Keeping one path means the CLI exercises exactly what the UI does.

The heavy stack stays out of the app: this process doesn't import ultralytics
either — the worker subprocess does. Point ``--python`` at an interpreter that has
it (defaults to the saved preference, else the current one):

    pip install ultralytics          # in that interpreter (~2 GB, pulls torch)

Examples
--------
    python training/train_detector.py --epochs 100
    python training/train_detector.py --python .trainvenv/Scripts/python.exe
    python training/train_detector.py --out src/tcginventory/models/card_yolo.onnx
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tcginventory.data.vision import (  # noqa: E402
    DetectorRunRepository,
    VisionRepository,
    open_vision,
)
from tcginventory.services import detector, training  # noqa: E402


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dataset", default="build/yolo", help="where to write the YOLO dataset"
    )
    p.add_argument("--vision-db", default=None, help="override the vision.db path")
    p.add_argument(
        "--python",
        default=None,
        help="interpreter that has ultralytics (default: saved preference, "
        "else this one)",
    )
    p.add_argument("--epochs", type=int, default=100)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--weights", default="yolov8n.pt", help="base checkpoint")
    p.add_argument(
        "--out",
        default=None,
        help="destination for the exported ONNX (default: the app's data dir, "
        "where a UI-triggered run puts it)",
    )
    p.add_argument(
        "--source",
        default="cli",
        help="recorded in the run log: cli | ui | scheduled",
    )
    p.add_argument(
        "--promote-margin",
        type=float,
        default=0.0,
        help="mAP50-95 a candidate must beat the model in use by to replace it",
    )
    p.add_argument(
        "--force-promote",
        action="store_true",
        help="install the new model even if it scores worse than the current one",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    env = training.probe_training_env(args.python)
    print(f"Training interpreter: {env.python}\n  {env.detail}")

    conn = open_vision(args.vision_db) if args.vision_db else open_vision()
    repo = VisionRepository(conn)
    runs = DetectorRunRepository(conn)
    try:
        job = training.prepare_detector_training(
            repo,
            runs,
            env=env,
            dataset_dir=args.dataset,
            out_path=args.out or detector.trained_model_destination(),
            epochs=args.epochs,
            imgsz=args.imgsz,
            weights=args.weights,
            source=args.source,
        )
    except training.TrainingUnavailable as exc:
        print(f"Cannot train: {exc}", file=sys.stderr)
        conn.close()
        return 1

    print(
        f"Run #{job.run_id}: {job.train_images} train / {job.val_images} val images, "
        f"{job.boxes} boxes\n  " + " ".join(job.command)
    )
    try:
        proc = subprocess.run(job.command, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        training.fail_detector_training(job, runs, str(exc))
        conn.close()
        print(f"Could not start the worker: {exc}", file=sys.stderr)
        return 3

    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    if proc.returncode != 0:
        reason = (proc.stderr or "").strip().splitlines()[-1:] or ["worker failed"]
        training.fail_detector_training(job, runs, reason[0])
        conn.close()
        return proc.returncode

    outcome = training.complete_detector_training(
        job,
        runs,
        worker_output=proc.stdout or "",
        promote_margin=args.promote_margin,
        force_promote=args.force_promote,
    )
    print(f"\n{outcome.summary}  ({outcome.epochs} epochs recorded)")
    if outcome.promoted:
        print(f"Model: {job.out_path}")
        print(
            "To ship it with the exe, copy it to src/tcginventory/models/ and "
            "track it with Git LFS."
        )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
