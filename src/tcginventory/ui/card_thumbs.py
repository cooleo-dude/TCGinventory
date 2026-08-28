"""Small, bounded card thumbnails for grids that show many cards at once.

``card_preview.CardImagePane`` decodes the **full-size** PNG from the disk cache
synchronously, which is right for one hovered card and wrong for a wall of them:
a decoded 245x342 pixmap is ~335 KB, so a 2,072-slot master collection would hold
about **694 MB** of them. Here the image is decoded straight to the size it will
be drawn at and the results are kept behind an LRU bound, so a screen of cards
costs kilobytes and the total can never grow without limit.

Reads only what ``services/cardimages`` already wrote to disk — it never fetches.
A card whose art is not cached yet returns ``None``, and the caller draws its
placeholder; warming the cache is the caller's job (see the Personal Collection
window's background warm), because a network fetch has no place on a paint path.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from tcginventory.services import cardimages

# Cards are 245x342 (5:7). Callers ask by width; the height follows so every
# thumbnail in a grid is the same shape whatever the source image was.
ASPECT = 342 / 245


def height_for(width: int) -> int:
    """The matching height for a thumbnail ``width``."""
    return round(width * ASPECT)


@lru_cache(maxsize=512)
def _decode(card_id: str, width: int) -> QPixmap | None:
    """Decode one card at one width, or ``None`` when it is not on disk.

    512 entries is roughly 20 screens of a paginated grid — enough that paging
    back and forth is instant, small enough to be a rounding error in memory
    (a 150 px-wide thumbnail is ~126 KB decoded, so the bound is ~64 MB worst
    case and far less in practice because most pages share no cards).
    """
    path = cardimages.cache_path(card_id)
    if not path.exists():
        return None
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        return None
    return pixmap.scaled(
        width,
        height_for(width),
        Qt.KeepAspectRatio,
        Qt.SmoothTransformation,
    )


def thumbnail(card_id: str | None, width: int) -> QPixmap | None:
    """A cached thumbnail for ``card_id`` at ``width``, or ``None``.

    ``None`` means "no art available", never "wait" — nothing here blocks and
    nothing here downloads.
    """
    if not card_id or width <= 0:
        return None
    return _decode(card_id, width)


def is_available(card_id: str | None) -> bool:
    """Whether art for this card is on disk, without decoding it."""
    return bool(card_id) and cardimages.cache_path(card_id).exists()


def clear() -> None:
    """Drop the decoded cache. For tests, and for a theme change if one ever
    tints thumbnails."""
    _decode.cache_clear()
