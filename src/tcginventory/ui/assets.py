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


def scrafty_movie_path() -> str:
    """Filesystem path to the bundled Scrafty animation (for a ``QMovie``)."""
    return str(resources.files("tcginventory.ui") / "assets" / "Scrafty.gif")
