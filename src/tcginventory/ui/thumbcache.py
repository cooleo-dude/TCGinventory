"""Session-scoped cache of small, pre-scaled thumbnail pixmaps.

The Cards columns in the History log (catalog art) and the Recognition log
(region crops) paint a stack of tiny thumbnails per row. Decoding those PNGs
from disk inside ``paint()`` means every repaint — a scroll, a hover, or simply
navigating back to the page — re-reads and re-scales dozens of files, so the
thumbnails visibly re-load each time the view is shown.

This module keeps the *scaled* pixmaps (a few KB each) in Qt's global
:class:`QPixmapCache` for the life of the process, so a file is decoded once per
size and every later paint is a lookup. Files that aren't on disk yet are
remembered as misses so repaints don't re-stat them; when a background worker
lands new thumbnails it calls :func:`forget_misses` to let them be picked up.

GUI-thread only (``QPixmapCache`` and ``QPixmap`` are not thread-safe).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QPixmapCache

# Room for a few thousand card thumbnails (Qt's default is 10 MiB, in KiB).
_CACHE_LIMIT_KIB = 32 * 1024

# Cache keys whose source file is missing or unreadable — re-checked only after
# forget_misses(), so a normal repaint costs no filesystem calls.
_misses: set[str] = set()
_limit_set = False


def scaled(
    namespace: str, key: str, path: Path | None, width: int, height: int
) -> QPixmap | None:
    """The thumbnail for ``key``, scaled to cover ``width`` x ``height``.

    ``namespace`` separates key spaces (catalog card ids vs. region ids).
    Returns None when there's no cached source image on disk yet.
    """
    global _limit_set
    if not _limit_set:
        QPixmapCache.setCacheLimit(max(QPixmapCache.cacheLimit(), _CACHE_LIMIT_KIB))
        _limit_set = True

    cache_key = f"{namespace}:{key}:{width}x{height}"
    if cache_key in _misses:
        return None
    cached = QPixmapCache.find(cache_key)
    if cached is not None and not cached.isNull():
        return cached
    if path is None or not path.exists():
        _misses.add(cache_key)
        return None
    src = QPixmap(str(path))
    if src.isNull():
        _misses.add(cache_key)
        return None
    pixmap = src.scaled(
        width, height, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
    )
    QPixmapCache.insert(cache_key, pixmap)
    return pixmap


def forget_misses() -> None:
    """Retry previously-missing thumbnails (new files landed on disk)."""
    _misses.clear()
