"""Offline fine-tune of the card **identifier** embedder (spec 06, M-V2 Part 2).

NOT imported by the app. Run it by hand in a training environment once you have a
labeled corpus in ``vision.db`` (the Recognition window's saved regions). It
needs the heavy stack, kept out of the app:

    pip install torch torchvision transformers pytorch-metric-learning

Pipeline: export the labeled crops (grouped by ``card_id``), metric-learn on top
of DINOv2-small so same-card crops embed close together and different cards far
apart (ArcFace over L2-normalised embeddings), and export an ONNX that maps a
preprocessed ``pixel_values`` tensor to a single pooled embedding — the same I/O
contract ``recognizer.OnnxEmbedder`` already consumes. Copy the result to
``src/tcginventory/models/identifier_finetuned.onnx`` (track with Git LFS); the
app then prefers it over the frozen base model and re-embeds the exemplar cache
under the new model version automatically.

The **usable** part of M-V2 (crop-exemplar retrieval) needs none of this — it
works the moment you label cards. This fine-tune is the higher-ceiling upgrade
for when the corpus is large; until then the export step below is still useful
to inspect the dataset.

Example:
    python training/train_identifier.py --epochs 30 --min-per-class 4
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
from tcginventory.services.exemplars import export_identity_crops  # noqa: E402
from tcginventory.services.recognizer import (  # noqa: E402
    FINETUNED_FILENAME,
    MODEL_FILENAME,
)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--crops", default="build/identity_crops", help="crops export dir")
    p.add_argument("--vision-db", default=None, help="override the vision.db path")
    p.add_argument("--base", default=None, help="base ONNX/HF model to fine-tune")
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--imgsz", type=int, default=224)
    p.add_argument(
        "--min-per-class",
        type=int,
        default=4,
        help="skip cards with fewer than this many labeled crops",
    )
    p.add_argument(
        "--out",
        default=str(REPO_ROOT / "src" / "tcginventory" / "models" / FINETUNED_FILENAME),
        help="destination for the exported ONNX model",
    )
    return p.parse_args(argv)


def _summarise_crops(crops_dir: Path, min_per_class: int) -> Counter:
    counts: Counter = Counter()
    for card_dir in crops_dir.iterdir():
        if card_dir.is_dir():
            counts[card_dir.name] = len(list(card_dir.glob("*.png")))
    trainable = Counter({k: v for k, v in counts.items() if v >= min_per_class})
    print(
        f"{len(counts)} cards / {sum(counts.values())} crops; "
        f"{len(trainable)} cards have >= {min_per_class} crops "
        f"({sum(trainable.values())} crops usable for metric learning)."
    )
    return trainable


def main(argv=None) -> int:
    args = _parse_args(argv)

    conn = open_vision(args.vision_db) if args.vision_db else open_vision()
    written = export_identity_crops(VisionRepository(conn), args.crops)
    conn.close()
    print(f"Exported {written} labeled crops to {args.crops}")
    if written == 0:
        print("No labeled crops — annotate cards in the Recognition window first.")
        return 1

    trainable = _summarise_crops(Path(args.crops), args.min_per_class)
    if not trainable:
        print(
            "Not enough crops per card yet for a stable fine-tune. Keep ingesting; "
            "crop-exemplar retrieval already works in the meantime."
        )
        return 1

    try:
        import torch  # noqa: F401
        from pytorch_metric_learning import losses  # noqa: F401
    except ImportError:
        print(
            "The training stack is not installed. Install it with:\n"
            "    pip install torch torchvision transformers pytorch-metric-learning\n"
            "(kept out of the app on purpose).",
            file=sys.stderr,
        )
        return 2

    # --- training happens here (dev-only) --------------------------------
    # Sketch of the intended routine, left explicit rather than hidden:
    #   1. ImageFolder over `args.crops`, ImageNet normalise + resize to imgsz
    #      (must match recognizer.OnnxEmbedder's preprocessing exactly).
    #   2. Load DINOv2-small (base = args.base or the bundled MODEL_FILENAME),
    #      take the CLS token, L2-normalise → embedding.
    #   3. ArcFace (losses.ArcFaceLoss) over the embeddings with an
    #      MPerClassSampler; fine-tune for args.epochs.
    #   4. torch.onnx.export a wrapper (pixel_values -> normalised embedding,
    #      output shape [1, D]) to args.out with a dynamic batch axis.
    # Kept as a documented skeleton: the exact backbone/opt schedule is tuned
    # against the real corpus, and this never runs inside the shipped app.
    base = args.base or MODEL_FILENAME
    print(
        "Training stack present. Implement/run the ArcFace fine-tune per the "
        f"steps in this file, exporting to {args.out} (base: {base}).\n"
        "Then commit the ONNX with Git LFS — the app auto-prefers it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
