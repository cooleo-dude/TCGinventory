"""Vision ingestion — store a transaction/auction's card photos as one **capture**
and record the cards they document. Qt-free.

Image files are **content-addressed**: each is hashed (SHA-256) and stored at
``<data_dir>/vision/images/<sha><ext>``, so identical files dedup on disk. A
capture groups all the photos supplied for a single entry with the set of cards
those photos collectively show (the labels the recognition engine learns from).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from tcginventory import config
from tcginventory.data.vision import CardLabel, VisionRepository


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


def ingest_capture(
    repo: VisionRepository,
    image_paths: Iterable[str | Path],
    source: str,
    cards: Iterable[CardLabel],
    *,
    base_dir: str | Path | None = None,
    now: str | None = None,
) -> int | None:
    """Record one capture: all the entry's photos + all its cards.

    Returns the new capture id, or ``None`` when there's nothing to store (no
    photos, or no identifiable cards).
    """
    paths = list(image_paths)
    cards = list(cards)
    if not paths or not cards:
        return None
    capture_id = repo.add_capture(source, now=now)
    for path in paths:
        dest, sha = store_file(path, base_dir)
        repo.add_image(capture_id, sha, str(dest), now=now)
    for card in cards:
        repo.add_capture_card(capture_id, card, now=now)
    return capture_id
