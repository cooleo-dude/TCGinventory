"""Size autofill completer popups to their content, not the input box.

A ``QCompleter`` popup defaults to the width of the field it completes, so a
fuller match label (e.g. ``"Pikachu  ·  Base Set  #58"``) gets clipped on a
narrow input — badly so in a table cell, where a Name column can be under 100px
while the labels need 600+. :func:`show_fitted` measures the popup's widest row
and opens it that wide, spilling past the field's right edge as needed, bounded
only by the screen.

**Why a persistent fitter and not just a resize.** ``QLineEdit::keyPressEvent``
changes the text (emitting ``textEdited``, where call sites resize the popup) and
*then* calls its own ``completer->complete()`` with no rect — which
``QCompleterPrivate::showPopup`` reads as "use ``widget->width()``" and applies
via ``setGeometry``. So a resize made from ``textEdited`` is overwritten a moment
later, on every keystroke, leaving the popup exactly as wide as the field. The
first :func:`show_fitted` call therefore installs a filter on the popup that
re-applies the fitted width on every ``Show``/``Resize`` — whoever sizes it,
content wins.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QCompleter, QWidget

_FITTER_NAME = "tcgPopupFitter"
_SCREEN_MARGIN = 40  # keep a popup off the very edge of the screen
_EXTRA = 6  # a little breathing room past the widest row


def _screen_limit(field: QWidget) -> int:
    screen = field.screen() or QGuiApplication.primaryScreen()
    if screen is None:  # pragma: no cover - no screen (never in a GUI session)
        return 1 << 24
    return max(120, screen.availableGeometry().width() - _SCREEN_MARGIN)


def _content_width(completer: QCompleter, popup: QWidget) -> int:
    """Pixels the widest row needs, including frame and scrollbar."""
    width = popup.sizeHintForColumn(0)
    scrollbar = popup.verticalScrollBar()
    if scrollbar is not None:
        model = popup.model()
        rows = model.rowCount() if model is not None else 0
        # Before the popup is shown the scrollbar reports hidden even when the
        # row count guarantees one — reserve its width either way, or the widest
        # row ends up elided by exactly the scrollbar.
        if scrollbar.isVisible() or rows > completer.maxVisibleItems():
            width += scrollbar.sizeHint().width()
    return width + 2 * popup.frameWidth() + _EXTRA


def fitted_width(
    completer: QCompleter, field: QWidget, max_width: int | None = None
) -> int:
    """The width ``field``'s popup should have: wide enough for its widest row,
    never narrower than the field, never wider than the screen (or
    ``max_width``)."""
    popup = completer.popup()
    limit = min(max_width, _screen_limit(field)) if max_width else _screen_limit(field)
    return max(field.width(), min(limit, _content_width(completer, popup)))


class _PopupFitter(QObject):
    """Re-applies the fitted width whenever the popup is shown or resized."""

    def __init__(
        self, completer: QCompleter, field: QWidget, max_width: int | None
    ) -> None:
        super().__init__(completer)
        self.setObjectName(_FITTER_NAME)
        self._completer = completer
        self._field = field
        self._max_width = max_width
        self._popup = completer.popup()
        self._busy = False
        self._popup.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if (
            obj is self._popup
            and not self._busy
            and event.type() in (QEvent.Show, QEvent.Resize)
        ):
            self._busy = True  # our own setGeometry re-enters here
            try:
                self.apply()
            finally:
                self._busy = False
        return super().eventFilter(obj, event)

    def apply(self) -> None:
        popup = self._popup
        width = fitted_width(self._completer, self._field, self._max_width)
        if width == popup.width():
            return
        # Widening can run the popup off the right edge — pull it back on screen.
        geom = popup.geometry()
        x = geom.x()
        screen = self._field.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = max(avail.left(), min(x, avail.right() - width + 1))
        popup.setGeometry(x, geom.y(), width, geom.height())


def _fitter(
    completer: QCompleter, field: QWidget, max_width: int | None
) -> _PopupFitter:
    """The completer's fitter, installed once and reused thereafter."""
    existing = completer.findChild(
        _PopupFitter, _FITTER_NAME, Qt.FindDirectChildrenOnly
    )
    return existing or _PopupFitter(completer, field, max_width)


def show_fitted(
    completer: QCompleter, field: QWidget, *, max_width: int | None = None
) -> None:
    """Open ``completer``'s popup anchored under ``field``, widened to fit its
    longest row (bounded by the screen, or ``max_width`` if given) but never
    narrower than the field.

    Drop-in for ``completer.complete()`` at a populated call site — the delegate
    already reports each row's true width via ``sizeHint``, so a fuller label is
    shown in full rather than truncated to the box. Also installs the persistent
    fitter (see the module docstring), so ``QLineEdit`` can't undo the width on
    the keystroke that follows.
    """
    fitter = _fitter(completer, field, max_width)
    rect = field.rect()
    rect.setWidth(fitted_width(completer, field, max_width))
    completer.complete(rect)
    fitter.apply()  # in case the popup was already open at a stale width
