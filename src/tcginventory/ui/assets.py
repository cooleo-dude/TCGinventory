"""Access to bundled UI assets (icon)."""

from __future__ import annotations

from importlib import resources

from PySide6.QtGui import QIcon, QPixmap


def app_icon() -> QIcon:
    """Return the application icon (works in dev and in a PyInstaller bundle)."""
    data = (resources.files("tcginventory.ui") / "assets" / "icon.png").read_bytes()
    pixmap = QPixmap()
    pixmap.loadFromData(data)
    return QIcon(pixmap)
