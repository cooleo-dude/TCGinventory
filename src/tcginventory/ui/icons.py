"""Lucide icons rendered as themed QIcons.

Lucide (https://lucide.dev) is an ISC-licensed line-icon set. The bundled SVGs
stroke with ``currentColor``; we substitute the requested colour and render at
the requested size, so every icon picks up the active theme. Results are cached
by (name, colour, size).
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


def _svg_text(name: str) -> str | None:
    """The icon's markup, or None when no such icon is bundled."""
    try:
        return (
            resources.files("tcginventory.ui") / "assets" / "icons" / f"{name}.svg"
        ).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


@lru_cache(maxsize=256)
def pixmap(name: str, color: str, size: int = 18) -> QPixmap:
    """Render Lucide icon ``name`` in ``color`` at ``size``×``size`` px.

    An unknown name yields a blank pixmap rather than raising. A missing icon is
    a cosmetic problem, but an exception raised part-way through building a
    widget is not: it leaves Qt holding a half-constructed object, and the crash
    surfaces later somewhere unrelated. A tile with no glyph is recoverable; a
    window that cannot be built is not.
    """
    pm = QPixmap(QSize(size, size))
    pm.fill(Qt.transparent)
    svg = _svg_text(name)
    if svg is None:
        return pm
    renderer = QSvgRenderer(QByteArray(svg.replace("currentColor", color).encode()))
    painter = QPainter(pm)
    try:
        renderer.render(painter)
    finally:
        painter.end()  # an unended painter on a QPixmap is its own crash
    return pm


def icon(name: str, color: str, size: int = 18) -> QIcon:
    return QIcon(pixmap(name, color, size))
