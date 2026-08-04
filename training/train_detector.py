"""Offline trainer for the single-class 'card' detector (spec 06, M-V1.5 Phase B).

NOT imported by the app — run it by hand in a training environment once you have
a labeled corpus in ``vision.db`` (draw + save boxes in the Recognition window).
It needs the heavy training stack, kept out of the app:

    pip install ultralytics          # pulls torch (~2 GB)

Pipeline: export the labeled regions to a YOLO dataset, fine-tune yolov8n on the
single ``card`` class, export to ONNX, and copy it where the app looks for it
(``src/tcginventory/models/card_yolo.onnx`` — track it with Git LFS like the
DINOv2 weights; the PyInstaller spec already bundles ``models/*.onnx``).

Examples
--------
    python training/train_detector.py --epochs 100
    python training/train_detector.py --data-yaml build/yolo/data.yaml
"""

from __future__ import annotations

import argparse
import shutil
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
from tcginventory.services import training  # noqa: E402
from tcginventory.services.detector import YOLO_MODEL_FILENAME  # noqa: E402
from tcginventory.services.detector_dataset import export_yolo_dataset  # noqa: E402


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dataset", default="build/yolo", help="where to write the YOLO dataset"
    )
    p.add_argument(
        "--data-yaml",
        default=None,
        help="use an existing data.yaml instead of exporting from vision.db",
    )
    p.add_argument("--vision-db", default=None, help="override the vision.db path")
    p.add_argument("--epochs", type=int, default=100)
    p.add_argument(
        "--source",
        default="manual",
        help="who asked for this run (recorded in the log): manual | scheduled",
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
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--weights", default="yolov8n.pt", help="base checkpoint")
    p.add_argument(
        "--out",
        default=str(
            REPO_ROOT / "src" / "tcginventory" / "models" / YOLO_MODEL_FILENAME
        ),
        help="destination for the exported ONNX model",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    # The run log lives in vision.db beside the corpus; open it for the whole run
    # so a crash still leaves a 'running' row behind rather than nothing.
    conn = open_vision(args.vision_db) if args.vision_db else open_vision()
    repo = VisionRepository(conn)
    runs = DetectorRunRepository(conn)
    snapshot = training.corpus_snapshot(repo)
    incumbent = runs.current_model()

    data_yaml = args.data_yaml
    summary = None
    if data_yaml is None:
        summary = export_yolo_dataset(repo, args.dataset)
        msg = (
            f"Exported {summary.images} images "
            f"({summary.train} train / {summary.val} val), {summary.boxes} boxes"
        )
        if summary.skipped_missing:
            msg += f", skipped {summary.skipped_missing} missing"
        print(msg)
        if summary.images == 0:
            print(
                "No labeled images found — annotate cards in the Recognition "
                "window and save first."
            )
            conn.close()
            return 1
        data_yaml = str(summary.data_yaml)

    run_id = runs.start(
        source=args.source,
        corpus_images=snapshot.images,
        corpus_boxes=snapshot.boxes,
        corpus_cards=snapshot.cards,
        corpus_fingerprint=snapshot.fingerprint,
        base_weights=args.weights,
        epochs=args.epochs,
        imgsz=args.imgsz,
        val_frac=None if summary is None else 0.2,
        split_seed=None if summary is None else 0,
    )
    print(
        f"Run #{run_id} recorded (corpus: {snapshot.images} images, "
        f"{snapshot.boxes} boxes, {snapshot.cards} cards)"
    )

    try:
        from ultralytics import YOLO
    except ImportError:
        runs.finish(run_id, status="failed", notes="ultralytics is not installed")
        conn.close()
        print(
            "ultralytics is not installed. Install the training stack with:\n"
            "    pip install ultralytics\n"
            "(this pulls torch, ~2 GB — intentionally kept out of the app).",
            file=sys.stderr,
        )
        return 2

    try:
        model = YOLO(args.weights)
        results = model.train(
            data=data_yaml, epochs=args.epochs, imgsz=args.imgsz, single_cls=True
        )
        exported = model.export(format="onnx", imgsz=args.imgsz, opset=12)
    except Exception as exc:  # a failed run is data too
        runs.finish(run_id, status="failed", notes=str(exc)[:500])
        conn.close()
        print(f"Training failed: {exc}", file=sys.stderr)
        return 3

    # Per-epoch curve + the epoch the exported weights actually represent.
    epochs = []
    csv_path = Path(getattr(results, "save_dir", ".")) / "results.csv"
    if csv_path.is_file():
        epochs = training.parse_results_csv(csv_path)
        runs.add_epochs(run_id, epochs)
    best = training.best_epoch(epochs) or {}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(exported, out)

    runs.finish(
        run_id,
        train_images=None if summary is None else summary.train,
        val_images=None if summary is None else summary.val,
        map50=best.get("map50"),
        map50_95=best.get("map50_95"),
        precision=best.get("precision"),
        recall=best.get("recall"),
        best_epoch=best.get("epoch"),
        artifact_path=str(out),
        artifact_sha256=training.sha256_file(out),
        artifact_bytes=out.stat().st_size,
        notes=f"{len(epochs)} epochs recorded",
    )

    # Promotion: don't let a worse model take over just because it trained later.
    incumbent_score = incumbent["map50_95"] if incumbent else None
    promote = args.force_promote or training.should_promote(
        best.get("map50_95"), incumbent_score, margin=args.promote_margin
    )
    if promote:
        version = runs.promote(run_id)
        print(f"Promoted as detector v{version}: {out}")
        print("(track with Git LFS; it will bundle into the exe)")
    else:
        print(
            f"Not promoted — mAP50-95 {best.get('map50_95')} does not beat the "
            f"model in use ({incumbent_score}). Re-run with --force-promote to "
            "install it anyway."
        )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
