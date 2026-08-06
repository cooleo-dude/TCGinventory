"""Train a single-class YOLO card detector. Runs in a **separate** interpreter.

This is the only piece that needs the training stack (``ultralytics`` + torch),
so it is deliberately **standalone**: it imports nothing from ``tcginventory`` and
takes everything by argument. That lets the app drive it from a training venv the
app itself knows nothing about — including from the frozen exe, which ships this
file as bundled data and has no source tree to import from.

The app owns the bookkeeping (dataset export, run log, metric parsing, artifact
hashing, promotion); this owns only the training. It reports back on stdout with a
single machine-readable line:

    TCGI_RESULT {"save_dir": "...", "onnx": "...", "epochs": 100}

Everything else on stdout/stderr is ultralytics' own progress output, which the
caller can show verbatim as a live log.

Run directly for a one-off:

    python training/yolo_train_worker.py --data build/yolo/data.yaml
        --out card_yolo.onnx
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

RESULT_PREFIX = "TCGI_RESULT "


def _save_dir(model, results) -> str:
    """Where ultralytics wrote this run — its ``results.csv``, weights and plots.

    Ask the *trainer*, not the metrics object. ``model.train()`` used to return
    something carrying ``save_dir``, and ultralytics 8.4 dropped it (``DetMetrics``
    takes only ``names`` now). A ``getattr`` default turned that into an empty
    string, the caller then looked for the per-epoch metrics nowhere, and two
    perfectly good detectors went unpromoted and were reported as never trained.
    The trainer has held this attribute throughout, so it leads; the result object
    stays as a fallback for older versions.
    """
    for holder in (getattr(model, "trainer", None), results):
        value = getattr(holder, "save_dir", None)
        if value:
            return str(value)
    return ""


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Train the single-class card detector.")
    p.add_argument("--data", required=True, help="path to the YOLO data.yaml")
    p.add_argument("--out", required=True, help="where to write the exported .onnx")
    p.add_argument("--weights", default="yolov8n.pt", help="base checkpoint")
    p.add_argument("--epochs", type=int, default=100)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--batch", type=int, default=-1, help="-1 = ultralytics auto")
    p.add_argument("--project", default=None, help="ultralytics runs directory")
    p.add_argument("--name", default=None, help="ultralytics run name")
    p.add_argument("--device", default=None, help="e.g. cpu, 0 (default: auto)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        from ultralytics import YOLO
    except ImportError:
        print(
            "ultralytics is not installed in this interpreter "
            f"({sys.executable}).\n    pip install ultralytics",
            file=sys.stderr,
        )
        return 2

    kwargs = dict(
        data=args.data,
        epochs=args.epochs,
        imgsz=args.imgsz,
        single_cls=True,  # one class: "card". Identity is the identifier's job.
    )
    if args.batch and args.batch > 0:
        kwargs["batch"] = args.batch
    if args.project:
        kwargs["project"] = args.project
    if args.name:
        kwargs["name"] = args.name
    if args.device:
        kwargs["device"] = args.device

    model = YOLO(args.weights)
    results = model.train(**kwargs)
    exported = model.export(format="onnx", imgsz=args.imgsz, opset=12)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(exported, out)

    # The caller reads results.csv out of save_dir for the per-epoch metrics.
    payload = {
        "save_dir": _save_dir(model, results),
        "onnx": str(out),
        "epochs": args.epochs,
    }
    print(RESULT_PREFIX + json.dumps(payload), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
