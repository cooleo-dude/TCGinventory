"""Offline trainer for the card **condition** classifier (spec 06, M-V3).

NOT imported by the app. Run it by hand in a training environment once you've
graded a batch of card **backs** in the Recognition window (tick "Card backs
(condition)", assess/confirm, save). It needs the heavy stack, kept out of the
app:

    pip install torch torchvision

Pipeline: export the graded back crops (grouped by condition), train a small
image classifier over the NM/LP/MP/HP/DMG scale, and export an ONNX that maps a
preprocessed ``pixel_values`` tensor to class logits — the I/O contract
``condition.OnnxConditionModel`` consumes. Copy the result to
``src/tcginventory/models/condition_model.onnx`` (track with Git LFS); the app
then prefers it over the whitening heuristic.

The **usable** part of M-V3 (the whitening heuristic) needs none of this — it
grades from day one. This is the higher-ceiling upgrade for when a corpus exists.

Example:
    python training/train_condition.py --epochs 30 --min-per-class 8
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tcginventory.data.vision import VisionRepository, open_vision  # noqa: E402
from tcginventory.services.condition import (  # noqa: E402
    CONDITION_MODEL_FILENAME,
    CONDITION_SCALE,
    export_condition_crops,
)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--crops", default="build/condition_crops", help="crops export dir")
    p.add_argument("--vision-db", default=None, help="override the vision.db path")
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--imgsz", type=int, default=224)
    p.add_argument(
        "--min-per-class",
        type=int,
        default=8,
        help="skip grades with fewer than this many labeled crops",
    )
    p.add_argument(
        "--out",
        default=str(
            REPO_ROOT / "src" / "tcginventory" / "models" / CONDITION_MODEL_FILENAME
        ),
        help="destination for the exported ONNX model",
    )
    return p.parse_args(argv)


def _summarise(crops_dir: Path, min_per_class: int) -> Counter:
    counts: Counter = Counter()
    for grade in CONDITION_SCALE:
        folder = crops_dir / grade.value
        if folder.is_dir():
            counts[grade.value] = len(list(folder.glob("*.png")))
    usable = Counter({k: v for k, v in counts.items() if v >= min_per_class})
    print(
        f"{sum(counts.values())} crops across {len([c for c in counts if counts[c]])} "
        f"grades; {len(usable)} grades have >= {min_per_class} "
        f"({sum(usable.values())} usable for training)."
    )
    return usable


def main(argv=None) -> int:
    args = _parse_args(argv)

    conn = open_vision(args.vision_db) if args.vision_db else open_vision()
    written = export_condition_crops(VisionRepository(conn), args.crops)
    conn.close()
    print(f"Exported {written} graded back crops to {args.crops}")
    if written == 0:
        print(
            "No graded back crops — in the Recognition window tick 'Card backs "
            "(condition)', assess/confirm, and save first."
        )
        return 1

    usable = _summarise(Path(args.crops), args.min_per_class)
    if len(usable) < 2:
        print(
            "Need at least two grades with enough crops to train a classifier. "
            "Keep grading; the whitening heuristic works in the meantime."
        )
        return 1

    try:
        import torch  # noqa: F401
    except ImportError:
        print(
            "The training stack is not installed. Install it with:\n"
            "    pip install torch torchvision\n"
            "(kept out of the app on purpose).",
            file=sys.stderr,
        )
        return 2

    # --- training happens here (dev-only) --------------------------------
    # Sketch of the intended routine, left explicit rather than hidden:
    #   1. ImageFolder over `args.crops`, ImageNet normalise + resize to imgsz
    #      (must match condition.OnnxConditionModel's preprocessing exactly).
    #   2. A small backbone (e.g. torchvision resnet18, or a frozen DINOv2 +
    #      linear head) with a 5-way head over CONDITION_SCALE; class-weighted
    #      cross-entropy for the (usually) imbalanced grades.
    #   3. Train args.epochs; hold out a validation split.
    #   4. torch.onnx.export (pixel_values -> logits[1, 5]) to args.out.
    # Kept as a documented skeleton: the exact backbone/schedule is tuned against
    # the real corpus, and this never runs inside the shipped app.
    print(
        "Training stack present. Implement/run the classifier per the steps in "
        f"this file, exporting logits over {[c.value for c in CONDITION_SCALE]} "
        f"to {args.out}. Then commit the ONNX with Git LFS — the app auto-prefers it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
