"""Cropped region thumbnails for the Recognition History Cards column.

Each labeled region is a box on a supplied photo; the corpus stores no catalog
``card_id`` for many of them, so there's no external art to show. Instead we crop
the region straight out of its source image and cache the small PNG on disk
(keyed by region id), mirroring :mod:`tcginventory.services.cardimages`. Qt-free
so it can run off the UI thread.
"""

from __future__ import annotations

from pathlib import Path

from tcginventory import config

_THUMB_W = 120  # cropped-card thumbnail width (px); the delegate scales it down


def crops_dir() -> Path:
    return config.data_dir() / "vision" / "crops"


def crop_path(region_id: int | str) -> Path:
    return crops_dir() / f"{region_id}.png"


def is_cropped(region_id: int | str) -> bool:
    return crop_path(region_id).exists()


def ensure_crop(
    region_id: int | str,
    image_path: str,
    box: tuple[float, float, float, float],
) -> Path | None:
    """Crop ``box`` (x, y, w, h normalised 0..1) from ``image_path`` and cache it.

    Returns the cached path, or None if the source is missing/unreadable or the
    box is degenerate. A no-op if already cached.
    """
    dest = crop_path(region_id)
    if dest.exists():
        return dest
    try:
        from PIL import Image

        with Image.open(image_path) as im:
            im = im.convert("RGB")
            width, height = im.size
            x, y, w, h = box
            left, top = max(0, int(x * width)), max(0, int(y * height))
            right = min(width, int((x + w) * width))
            bottom = min(height, int((y + h) * height))
            if right <= left or bottom <= top:
                return None
            crop = im.crop((left, top, right, bottom))
            thumb_h = max(1, round(crop.height * _THUMB_W / crop.width))
            crop = crop.resize((_THUMB_W, thumb_h))
            dest.parent.mkdir(parents=True, exist_ok=True)
            crop.save(dest, "PNG")
        return dest
    except (OSError, ValueError):
        return None
