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


def _svg_text(name: str) -> str:
    return (
        resources.files("tcginventory.ui") / "assets" / "icons" / f"{name}.svg"
    ).read_text(encoding="utf-8")


@lru_cache(maxsize=256)
def pixmap(name: str, color: str, size: int = 18) -> QPixmap:
    """Render Lucide icon ``name`` in ``color`` at ``size``×``size`` px."""
    svg = _svg_text(name).replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pm = QPixmap(QSize(size, size))
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    renderer.render(painter)
    painter.end()
    return pm


def icon(name: str, color: str, size: int = 18) -> QIcon:
    return QIcon(pixmap(name, color, size))
