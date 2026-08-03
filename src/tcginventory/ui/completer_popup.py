"""Size autofill completer popups to their content, not the input box.

A ``QCompleter`` popup defaults to the width of the field it completes, so a
fuller match label (e.g. ``"Pikachu  ·  Base Set  #58"``) gets clipped on a
narrow input like the Transaction "Their Cards" Name box. :func:`show_fitted`
measures the popup's widest row and opens it that wide — spilling past the
field's right edge as needed — instead of clamping to the field.
"""

from __future__ import annotations

from PySide6.QtWidgets import QCompleter, QWidget

# Never run wider than this, so a very long label can't shoot off-screen.
_MAX_POPUP_W = 620


def show_fitted(
    completer: QCompleter, field: QWidget, *, max_width: int = _MAX_POPUP_W
) -> None:
    """Open ``completer``'s popup anchored under ``field``, widened to fit its
    longest row (clamped to ``max_width``) but never narrower than the field.

    Drop-in for ``completer.complete()`` at a populated call site — the delegate
    already reports each row's true width via ``sizeHint``, so a fuller label is
    shown in full rather than truncated to the box.
    """
    popup = completer.popup()
    content = popup.sizeHintForColumn(0)
    scrollbar = popup.verticalScrollBar()
    if scrollbar is not None and scrollbar.isVisible():
        content += scrollbar.sizeHint().width()
    content += 2 * popup.frameWidth() + 6  # frame + a little breathing room
    width = max(field.width(), min(max_width, content))
    rect = field.rect()
    rect.setWidth(width)
    completer.complete(rect)
