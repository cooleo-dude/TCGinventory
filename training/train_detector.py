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

from tcginventory.data.vision import VisionRepository, open_vision  # noqa: E402
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

    data_yaml = args.data_yaml
    if data_yaml is None:
        conn = open_vision(args.vision_db) if args.vision_db else open_vision()
        repo = VisionRepository(conn)
        summary = export_yolo_dataset(repo, args.dataset)
        conn.close()
        msg = (
            f"Exported {summary.images} images "
            f"({summary.train} train / {summary.val} val), {summary.boxes} boxes"
        )
        if summary.skipped_missing:
            msg += f", skipped {summary.skipped_missing} missing"
        print(msg)
        if summary.images == 0:
            print("No labeled images found — annotate cards in the Recognition "
                  "window and save first.")
            return 1
        data_yaml = str(summary.data_yaml)

    try:
        from ultralytics import YOLO
    except ImportError:
        print(
            "ultralytics is not installed. Install the training stack with:\n"
            "    pip install ultralytics\n"
            "(this pulls torch, ~2 GB — intentionally kept out of the app).",
            file=sys.stderr,
        )
        return 2

    model = YOLO(args.weights)
    model.train(data=data_yaml, epochs=args.epochs, imgsz=args.imgsz, single_cls=True)
    exported = model.export(format="onnx", imgsz=args.imgsz, opset=12)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(exported, out)
    print(f"Wrote {out}  (track with Git LFS; it will bundle into the exe)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
