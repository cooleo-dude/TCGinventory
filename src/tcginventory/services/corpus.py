"""Write a labeled scene back into the vision corpus (spec 06, Part 2 feedback).

When a user confirms cards — annotating in the Recognition window, or accepting
image-autofilled cards in Transaction/Auction — that scene is ground truth. This
stores it: the image (content-addressed), a capture, a region per confirmed box
(detector training data), and a crop exemplar per **identified** box (identifier
training data, so the matcher improves). Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from tcginventory.data.vision import CardLabel, VisionRepository
from tcginventory.services import exemplars
from tcginventory.services import vision as vision_svc

# One confirmed box: normalised (x, y, w, h), its identity, and optional side.
Entry = tuple[tuple[float, float, float, float], CardLabel, "str | None"]


@dataclass
class IngestResult:
    capture_id: int
    regions: int
    exemplars: int


def ingest_labeled_scene(
    repo: VisionRepository,
    embedder,
    image_path: str | Path,
    source: str,
    entries,
    *,
    vision_dir=None,
) -> IngestResult:
    """Persist ``entries`` (``(box, CardLabel, side?)``) as a capture of one image.

    Every entry becomes a region (detector data); entries with a ``card_id`` also
    get a crop exemplar embedded via ``embedder`` (skipped when ``embedder`` is
    None). Returns the counts.
    """
    dest, sha = vision_svc.store_file(image_path, vision_dir)
    try:
        with Image.open(image_path) as im:
            width, height = im.size
    except (FileNotFoundError, OSError):
        width = height = None

    capture_id = repo.add_capture(source=source)
    image_id = repo.add_image(
        capture_id, sha, str(dest), width=width, height=height
    )

    exemplar_entries = []
    regions = 0
    for number, entry in enumerate(entries, start=1):
        box, card = entry[0], entry[1]
        side = entry[2] if len(entry) > 2 else None
        region_id = repo.add_region(image_id, number, box, side, card)
        regions += 1
        if card.card_id:
            exemplar_entries.append((region_id, card.card_id, str(dest), box))

    indexed = 0
    if embedder is not None and exemplar_entries:
        indexed = exemplars.index_regions(repo, embedder, exemplar_entries)
    return IngestResult(capture_id=capture_id, regions=regions, exemplars=indexed)
