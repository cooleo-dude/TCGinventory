"""Export the labeled region corpus to a YOLO detection dataset (spec 06, M-V1.5
Phase B). Qt-free; used by the offline training script, not the app at runtime.

Each annotated image becomes one YOLO label file: a ``0 cx cy w h`` line per
region (single class ``card``; coordinates normalised 0..1, centre form). Images
are copied into ``images/{train,val}/`` and labels into ``labels/{train,val}/``,
with a ``data.yaml`` pointing at them — the layout ultralytics expects.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from tcginventory.data.vision import VisionRepository
from tcginventory.services.fitness import SPLIT_HASHED_V1


@dataclass
class DatasetSummary:
    images: int
    train: int
    val: int
    boxes: int
    data_yaml: Path
    skipped_missing: int  # source image files that no longer exist on disk
    policy: str = SPLIT_HASHED_V1  # how val was chosen; recorded on the run


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _val_rank(image_id: int, seed: int) -> float:
    """A stable position in [0, 1) for one image, from its id alone.

    This is what makes two runs comparable. The exporter used to shuffle the
    whole corpus and slice off the first 20%, which sounds deterministic and is —
    for a corpus of a fixed size. Add eleven images and ``shuffle`` deals a
    different hand, so the validation set is replaced wholesale and the new
    model's score describes a different exam from the old model's. Comparing the
    two then rejected five consecutive better detectors (2026-08-07).

    Hashing the id instead pins each image to its own place: an image in val
    stays in val however much the corpus grows around it.
    """
    digest = hashlib.blake2b(f"{seed}:{image_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


def _clear_split_dirs(out: Path) -> None:
    """Empty the train/val directories before writing this export.

    They were only ever added to. An image that moved between splits — which the
    old shuffle did constantly — left its previous copy behind, so the same photo
    could sit in *both* train and val and the validation score was measured partly
    on images the model had trained on.
    """
    for kind in ("images", "labels"):
        for split in ("train", "val"):
            target = out / kind / split
            if not target.is_dir():
                continue
            for path in target.iterdir():
                if path.is_file():
                    path.unlink()


def export_yolo_dataset(
    repo: VisionRepository,
    out_dir: str | Path,
    *,
    val_frac: float = 0.2,
    seed: int = 0,
    copy_images: bool = True,
) -> DatasetSummary:
    """Write a YOLO dataset for the single ``card`` class under ``out_dir``.

    Deterministic given ``seed``, and **stable as the corpus grows**: each image
    is assigned to train or val by a hash of its own id, so adding photos never
    reshuffles the ones already there. Images whose source file is gone are
    skipped (counted in the summary). Returns the counts and the ``data.yaml``
    path.
    """
    out = Path(out_dir)
    for split in ("train", "val"):
        (out / "images" / split).mkdir(parents=True, exist_ok=True)
        (out / "labels" / split).mkdir(parents=True, exist_ok=True)
    _clear_split_dirs(out)

    images = sorted(repo.images_with_regions(), key=lambda r: int(r["id"]))
    ranks = {int(img["id"]): _val_rank(int(img["id"]), seed) for img in images}
    chosen = {i for i, rank in ranks.items() if rank < val_frac}
    # Hashing gives *about* val_frac, not exactly it, and on a small corpus about
    # can be none at all — which ultralytics cannot validate against. Fall back to
    # the single lowest-ranked image, still chosen by hash and so still stable.
    if not chosen and len(images) >= 2:
        chosen = {min(ranks, key=lambda i: ranks[i])}

    train = val = boxes = skipped = 0
    for img in images:
        src = Path(img["path"])
        if copy_images and not src.exists():
            skipped += 1
            continue
        split = "val" if int(img["id"]) in chosen else "train"
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
        policy=SPLIT_HASHED_V1,
    )
