"""Vision image store — content-addressed file storage for card photos. Qt-free.

Image files are **content-addressed**: each is hashed (SHA-256) and stored at
``<data_dir>/vision/images/<sha><ext>``, so identical files dedup on disk. The
capture/image/region records that reference these files are written directly via
:class:`tcginventory.data.vision.VisionRepository` (see ``services/corpus.py``
and the Recognition window).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tcginventory import config


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
