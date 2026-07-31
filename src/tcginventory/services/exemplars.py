"""Crop-exemplar indexing for the identity matcher (spec 06, M-V2).

Every labeled region is a ``(crop, card_id)`` pair. Embedding those crops and
retrieving against them makes identification **crop-vs-crop** (same visual
domain) instead of crop-vs-catalog-render — accurate from the first handful of
labels and better with every ingested card, with **no model training**. The
same crops are the dataset the offline fine-tune (Part 2) learns from.

Qt-free. The embedder is any object with ``.version`` and ``.embed(image)`` (see
:mod:`tcginventory.services.recognizer`).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from tcginventory.data.vision import VisionRepository

Box = tuple[float, float, float, float]  # normalised (x, y, w, h) in 0..1


def crop_region(image_path: str | Path, box: Box) -> Image.Image:
    """Crop a normalised ``(x, y, w, h)`` region out of the full-resolution image."""
    x, y, w, h = box
    with Image.open(image_path) as im:
        img = im.convert("RGB")  # forces a load, so it survives the file close
    width, height = img.size
    left = max(0, min(int(round(x * width)), width - 1))
    top = max(0, min(int(round(y * height)), height - 1))
    right = max(left + 1, min(int(round((x + w) * width)), width))
    bottom = max(top + 1, min(int(round((y + h) * height)), height))
    return img.crop((left, top, right, bottom))


def index_regions(
    repo: VisionRepository,
    embedder,
    entries,
    *,
    skip_existing: bool = True,
) -> int:
    """Embed and store crop exemplars for ``entries``.

    ``entries`` is an iterable of ``(region_id, card_id, image_path, box)``. Only
    regions with a ``card_id`` are indexed; missing/unreadable source files are
    skipped. Returns the number of exemplars written.
    """
    version = embedder.version
    written = 0
    for region_id, card_id, image_path, box in entries:
        if not card_id:
            continue
        if skip_existing and repo.has_region_embedding(region_id, version):
            continue
        try:
            crop = crop_region(image_path, box)
        except (FileNotFoundError, OSError):
            continue
        vec = np.asarray(embedder.embed(crop), dtype=np.float32)
        repo.put_region_embedding(
            region_id, version, card_id, int(vec.shape[0]), vec.tobytes()
        )
        written += 1
    return written


def reindex_all(repo: VisionRepository, embedder, *, skip_existing: bool = True) -> int:
    """(Re)index every labeled region — used after a model/version change or to
    backfill a corpus captured before indexing existed."""
    entries = [
        (
            r["region_id"],
            r["card_id"],
            r["image_path"],
            (r["x"], r["y"], r["w"], r["h"]),
        )
        for r in repo.labeled_regions()
    ]
    return index_regions(repo, embedder, entries, skip_existing=skip_existing)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "unknown"


def export_identity_crops(repo: VisionRepository, out_dir: str | Path) -> int:
    """Write labeled crops to ``out_dir/<card_id>/<region_id>.png`` (training set
    for the offline fine-tune). Returns the number of crops written."""
    out = Path(out_dir)
    written = 0
    for r in repo.labeled_regions():
        try:
            crop = crop_region(r["image_path"], (r["x"], r["y"], r["w"], r["h"]))
        except (FileNotFoundError, OSError):
            continue
        folder = out / _safe(r["card_id"])
        folder.mkdir(parents=True, exist_ok=True)
        crop.save(folder / f"{r['region_id']:06d}.png")
        written += 1
    return written
