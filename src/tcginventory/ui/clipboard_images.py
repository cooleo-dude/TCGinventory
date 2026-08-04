"""Clipboard → image paths, shared by every paste-an-image entry point.

The vision pipeline (detect / crop / suggest / corpus write-back) reads scenes
**from disk**, so a raw bitmap on the clipboard — a screenshot, or an image
copied out of a browser — is spooled to a temp PNG; copied image *files* are
used where they already live.
"""

from __future__ import annotations

import os
import tempfile

from PySide6.QtGui import QGuiApplication, QPixmap

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


def spool_image(image) -> str:
    """Write a pasted ``QImage`` to a temp PNG and return its path."""
    fd, path = tempfile.mkstemp(prefix="tcgi-paste-", suffix=".png")
    os.close(fd)
    QPixmap.fromImage(image).save(path, "PNG")
    return path


def clipboard_image_paths() -> list[str]:
    """Image paths from the clipboard, or ``[]`` when it holds no image.

    Copied image files come back as their own paths (possibly several); a raw
    bitmap comes back as a single spooled temp PNG.
    """
    clip = QGuiApplication.clipboard()
    if clip is None:
        return []
    mime = clip.mimeData()
    paths: list[str] = []
    if mime is not None and mime.hasUrls():
        for url in mime.urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(IMAGE_EXTS):
                paths.append(url.toLocalFile())
    if paths:
        return paths
    image = clip.image()
    return [spool_image(image)] if not image.isNull() else []
