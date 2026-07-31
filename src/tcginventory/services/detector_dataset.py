"""Export the labeled region corpus to a YOLO detection dataset (spec 06, M-V1.5
Phase B). Qt-free; used by the offline training script, not the app at runtime.

Each annotated image becomes one YOLO label file: a ``0 cx cy w h`` line per
region (single class ``card``; coordinates normalised 0..1, centre form). Images
are copied into ``images/{train,val}/`` and labels into ``labels/{train,val}/``,
with a ``data.yaml`` pointing at them — the layout ultralytics expects.
"""

from __future__ import annotations

import random
import shutil
from dataclasses import dataclass
from pathlib import Path

from tcginventory.data.vision import VisionRepository


@dataclass
class DatasetSummary:
    images: int
    train: int
    val: int
    boxes: int
    data_yaml: Path
    skipped_missing: int  # source image files that no longer exist on disk


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def export_yolo_dataset(
    repo: VisionRepository,
    out_dir: str | Path,
    *,
    val_frac: float = 0.2,
    seed: int = 0,
    copy_images: bool = True,
) -> DatasetSummary:
    """Write a YOLO dataset for the single ``card`` class under ``out_dir``.

    Deterministic given ``seed``. Images whose source file is gone are skipped
    (counted in the summary). Returns the counts and the ``data.yaml`` path.
    """
    out = Path(out_dir)
    for split in ("train", "val"):
        (out / "images" / split).mkdir(parents=True, exist_ok=True)
        (out / "labels" / split).mkdir(parents=True, exist_ok=True)

    images = list(repo.images_with_regions())
    random.Random(seed).shuffle(images)
    n_val = int(len(images) * val_frac)

    train = val = boxes = skipped = 0
    for i, img in enumerate(images):
        src = Path(img["path"])
        if copy_images and not src.exists():
            skipped += 1
            continue
        split = "val" if i < n_val else "train"
        regions = repo.regions_for_image(img["id"])
        lines = []
        for r in regions:
            cx = _clamp01(r["x"] + r["w"] / 2)
            cy = _clamp01(r["y"] + r["h"] / 2)
            w = _clamp01(r["w"])
            h = _clamp01(r["h"])
            if w <= 0 or h <= 0:
                continue
            lines.append(f"0 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
        if not lines:
            continue
        stem = f"{img['id']:06d}_{src.stem}"
        if copy_images:
            shutil.copy2(src, out / "images" / split / f"{stem}{src.suffix}")
        (out / "labels" / split / f"{stem}.txt").write_text("\n".join(lines))
        boxes += len(lines)
        if split == "val":
            val += 1
        else:
            train += 1

    data_yaml = out / "data.yaml"
    data_yaml.write_text(
        f"path: {out.resolve().as_posix()}\n"
        "train: images/train\n"
        "val: images/val\n"
        "names:\n"
        "  0: card\n"
    )
    return DatasetSummary(
        images=train + val,
        train=train,
        val=val,
        boxes=boxes,
        data_yaml=data_yaml,
        skipped_missing=skipped,
    )
