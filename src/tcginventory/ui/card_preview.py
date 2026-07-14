"""A single floating card-image preview shown while hovering a card anywhere.

One shared frameless popup is reused across the app (autofill matches, the
inventory grid, transaction rows). Callers hand it a catalog ``card_id`` + image
URL and a screen position; the image is fetched+cached lazily on first use
(:mod:`tcginventory.services.cardimages`) and served from disk after. A fetch
that finishes after the pointer has moved on is discarded (guarded by the
currently-requested id).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QThread, Signal
from PySide6.QtGui import QCursor, QPixmap
from PySide6.QtWidgets import QLabel, QWidget

from tcginventory.services import cardimages

_PREVIEW_WIDTH = 240  # px; small-image PNGs are ~245px wide natively


class _FetchWorker(QThread):
    done = Signal(str, str)  # card_id, local path ("" on failure)

    def __init__(self, card_id: str, url: str) -> None:
        super().__init__()
        self._card_id = card_id
        self._url = url

    def run(self) -> None:  # pragma: no cover - thread body exercised via UI
        path = cardimages.ensure_cached(self._card_id, self._url)
        self.done.emit(self._card_id, str(path) if path else "")


class CardImagePreview(QObject):
    def __init__(self) -> None:
        super().__init__()
        self._label = QLabel(None, Qt.WindowType.ToolTip)
        self._label.setStyleSheet(
            "background:#1d2021; border:1px solid #928374; padding:3px;"
        )
        self._label.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # Never capture the pointer — otherwise sliding toward the image would
        # trigger a leave/enter flicker on the widget underneath.
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._label.hide()
        self._current: str | None = None
        self._pos = QPoint()
        self._workers: list[_FetchWorker] = []

    def request(
        self, card_id: str | None, url: str | None, pos: QPoint | None = None
    ) -> None:
        """Show the image for ``card_id`` at ``pos`` (cursor if omitted)."""
        if not card_id or not url:
            self.clear()
            return
        self._current = card_id
        self._pos = pos or QCursor.pos()
        path = cardimages.cache_path(card_id)
        if path.exists():
            self._show(str(path))
            return
        worker = _FetchWorker(card_id, url)
        worker.done.connect(self._on_fetched)
        worker.finished.connect(lambda w=worker: self._retire(w))
        self._workers.append(worker)
        worker.start()

    def clear(self) -> None:
        self._current = None
        try:
            self._label.hide()
        except RuntimeError:
            # C++ QLabel already deleted (e.g. during app teardown) — nothing
            # left to hide.
            pass

    def _retire(self, worker: _FetchWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)

    def _on_fetched(self, card_id: str, path: str) -> None:
        # Only paint if the pointer is still on the same card.
        if path and card_id == self._current:
            self._show(path)

    def _show(self, path: str) -> None:
        pix = QPixmap(path)
        if pix.isNull():
            return
        try:
            self._label.setPixmap(
                pix.scaledToWidth(
                    _PREVIEW_WIDTH, Qt.TransformationMode.SmoothTransformation
                )
            )
            self._label.adjustSize()
            self._label.move(self._pos + QPoint(18, 18))
            self._label.show()
            self._label.raise_()
        except RuntimeError:
            pass  # C++ QLabel already deleted (app teardown)


_instance: CardImagePreview | None = None


def card_preview() -> CardImagePreview:
    """The shared preview singleton (created on first use)."""
    global _instance
    if _instance is None:
        _instance = CardImagePreview()
    return _instance


class _HoverFilter(QObject):
    """Show the preview while the pointer is over ``widget``.

    ``resolve()`` returns the ``(card_id, image_url)`` to preview, or ``None``
    for no image (the pointer entering a container with no single card).
    """

    def __init__(
        self, widget: QWidget, resolve: Callable[[], tuple[str, str] | None]
    ) -> None:
        super().__init__(widget)
        self._resolve = resolve
        widget.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        etype = event.type()
        if etype == QEvent.Type.Enter:
            res = self._resolve()
            if res:
                card_preview().request(res[0], res[1], QCursor.pos())
            else:
                card_preview().clear()
        elif etype in (QEvent.Type.Leave, QEvent.Type.Hide):
            card_preview().clear()
        return False


def install_hover_preview(
    widget: QWidget, resolve: Callable[[], tuple[str, str] | None]
) -> _HoverFilter:
    """Wire ``widget`` so hovering it shows the card image from ``resolve()``."""
    return _HoverFilter(widget, resolve)
