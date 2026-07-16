"""Card-image previews shown while hovering a card.

Two sinks share the same lazy fetch+cache (:mod:`tcginventory.services.cardimages`):

* :class:`CardImagePreview` — a shared frameless popup that follows the cursor
  (used by the inventory grid).
* :class:`CardImagePane` — an embeddable fixed region (the Transaction window
  parks one in its lower-left corner).

Both expose ``show_card(card_id, url)`` / ``clear()``; :func:`install_hover_preview`
wires a widget's hover to whichever sink you pass (default: the popup). A fetch
that finishes after the pointer has moved on is discarded (guarded by the
currently-requested id).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QThread, Signal
from PySide6.QtGui import QCursor, QPixmap, QResizeEvent
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

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

    def show_card(self, card_id: str | None, url: str | None) -> None:
        """Sink interface: show at the current cursor position."""
        self.request(card_id, url)

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


class CardImagePane(QLabel):
    """An embeddable card preview region (e.g. Transaction lower-left corner).

    Same lazy fetch+cache as the popup. Every card is shown at the *same* width
    (no zoom-to-fill): the display width tracks the pane's width but is clamped
    to a sensible band, so it grows modestly with the window and never upscales
    past the source resolution. Shows a placeholder when empty.
    """

    _PLACEHOLDER = "Hover a card\nto preview"
    _MAX_W = 460  # px — cap so the card tracks its column (≈ the give panel)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Set state first so a resize during construction finds it (resizeEvent
        # -> _rescale reads self._pixmap).
        self._current: str | None = None
        self._pixmap: QPixmap | None = None
        self._workers: list[_FetchWorker] = []
        self.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        )
        self.setMinimumSize(110, 155)
        # Take whatever space the layout gives (don't let the pixmap's size hint
        # drive/inflate the layout) — the image is scaled to fit that space.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.set_border()
        self.setText(self._PLACEHOLDER)

    def set_border(self, color: str = "#504945", width: int = 1) -> None:
        """Recolour the frame — used to tint a thumbnail to match its graph line."""
        self.setStyleSheet(
            f"border:{width}px solid {color}; border-radius:4px; "
            "background:#1d2021; color:#a89984;"
        )

    def show_card(self, card_id: str | None, url: str | None) -> None:
        # No valid card (e.g. hovering a row with no catalog match, or a leave):
        # keep the most-recently-viewed image rather than blanking the pane.
        if not card_id or not url:
            return
        self._current = card_id
        path = cardimages.cache_path(card_id)
        if path.exists():
            self._set_image(card_id, str(path))
            return
        worker = _FetchWorker(card_id, url)
        worker.done.connect(self._on_fetched)
        worker.finished.connect(lambda w=worker: self._retire(w))
        self._workers.append(worker)
        worker.start()

    def clear(self) -> None:
        # Leaving a card keeps the last one shown (persistent preview). Nothing
        # to do — a subsequent show_card replaces it.
        pass

    def _retire(self, worker: _FetchWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)

    def _on_fetched(self, card_id: str, path: str) -> None:
        if path and card_id == self._current:
            self._set_image(card_id, path)

    def _set_image(self, card_id: str, path: str) -> None:
        pix = QPixmap(path)
        if pix.isNull() or card_id != self._current:
            return
        self._pixmap = pix
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        # Fit within the pane's width AND height (aspect kept), width capped —
        # so the card never overflows/clips the bottom of a short pane, and
        # grows with the pane up to the cap.
        avail_w = min(self.width() - 8, self._MAX_W)
        avail_h = self.height() - 8
        if avail_w < 1 or avail_h < 1:
            return
        try:
            self.setPixmap(
                self._pixmap.scaled(
                    avail_w,
                    avail_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        except RuntimeError:
            pass  # C++ QLabel already deleted (app teardown)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._rescale()


_instance: CardImagePreview | None = None


def card_preview() -> CardImagePreview:
    """The shared floating-popup singleton (created on first use)."""
    global _instance
    if _instance is None:
        _instance = CardImagePreview()
    return _instance


class _HoverFilter(QObject):
    """Show the preview while the pointer is over ``widget``.

    ``resolve()`` returns the ``(card_id, image_url)`` to preview, or ``None``
    for no image (the pointer entering a container with no single card).
    ``sink`` is the preview target; defaults to the shared floating popup.
    """

    def __init__(
        self,
        widget: QWidget,
        resolve: Callable[[], tuple[str, str] | None],
        sink: object | None = None,
    ) -> None:
        super().__init__(widget)
        self._resolve = resolve
        self._sink = sink
        widget.installEventFilter(self)

    def _target(self):
        return self._sink or card_preview()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        etype = event.type()
        try:
            if etype == QEvent.Type.Enter:
                res = self._resolve()
                self._target().show_card(*(res if res else (None, None)))
            elif etype in (QEvent.Type.Leave, QEvent.Type.Hide):
                self._target().clear()
        except RuntimeError:
            pass  # sink's C++ widget already deleted (app teardown)
        return False


def install_hover_preview(
    widget: QWidget,
    resolve: Callable[[], tuple[str, str] | None],
    sink: object | None = None,
) -> _HoverFilter:
    """Wire ``widget`` so hovering shows the card from ``resolve()`` in ``sink``
    (default: the shared floating popup)."""
    return _HoverFilter(widget, resolve, sink)
