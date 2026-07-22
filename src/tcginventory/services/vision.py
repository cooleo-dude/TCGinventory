"""Vision ingestion — copy a supplied card photo into the training store and
record its label. Qt-free.

Images are **content-addressed**: the file is hashed (SHA-256) and stored at
``<data_dir>/vision/images/<sha><ext>``, so re-adding the same photo dedups to
one file and one ``image`` row. The label (the identity/condition the user
assigned) is recorded per ingest, building the ``(image, label)`` corpus the
recognition engine will learn from.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tcginventory import config
from tcginventory.data.vision import ImageLabel, VisionRepository


def images_dir(base_dir: str | Path | None = None) -> Path:
    """Directory holding the content-addressed image files."""
    base = Path(base_dir) if base_dir is not None else config.data_dir()
    return base / "vision" / "images"


def store_file(
    src_path: str | Path, base_dir: str | Path | None = None
) -> tuple[Path, str]:
    """Copy ``src_path`` into the content-addressed store; return (dest, sha256).

    Idempotent: an identical file already present is not rewritten.
    """
    data = Path(src_path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    ext = Path(src_path).suffix.lower() or ".img"
    dest_dir = images_dir(base_dir)
    dest = dest_dir / f"{sha}{ext}"
    if not dest.exists():
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return dest, sha


def ingest(
    repo: VisionRepository,
    src_path: str | Path,
    source: str,
    label: ImageLabel,
    *,
    width: int | None = None,
    height: int | None = None,
    base_dir: str | Path | None = None,
    now: str | None = None,
) -> int:
    """Store the photo file and record one labeled example; returns the image id."""
    dest, sha = store_file(src_path, base_dir)
    image_id = repo.add_image(
        sha256=sha,
        path=str(dest),
        source=source,
        width=width,
        height=height,
        now=now,
    )
    repo.add_label(image_id, label, now=now)
    return image_id
