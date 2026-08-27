"""History view — full transaction log with detail drill-down (F-VW-3).

Styled to the Ember Duo theme: the log renders type/status as pills and money as
mono, sign-tinted; selecting a row opens a summary + a per-card table below.
Searchable by **card** or counterparty. Void/Edit act on the selected row.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QRectF,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QThread,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.auctions import (
    EVENT_ARCHIVED,
    EVENT_DELETED,
    EVENT_UNARCHIVED,
    EVENT_WON,
    AuctionEvent,
    AuctionEventRepository,
)
from tcginventory.data.db import connect
from tcginventory.data.repositories import (
    ItemRepository,
    LotConsumptionRepository,
    LotRepository,
    TransactionLineRepository,
    TransactionRepository,
)
from tcginventory.data.vision import VisionRepository, open_vision
from tcginventory.domain.enums import Direction, TransactionStatus
from tcginventory.domain.models import Item
from tcginventory.services import cardimages, corpus, regioncrops
from tcginventory.services.exceptions import ServiceError
from tcginventory.services.reporting import ReportingService
from tcginventory.services.transactions import TransactionService
from tcginventory.ui import icons, theme, thumbcache
from tcginventory.ui.card_gallery import CardGallery
from tcginventory.ui.formatting import money, percent
from tcginventory.ui.models.auction_history_model import (
    AuctionEventRow,
    AuctionHistoryModel,
)
from tcginventory.ui.models.history_model import (
    CARDS_COL,
    CARDS_ROLE,
    HistoryRow,
    HistoryTableModel,
)
from tcginventory.ui.models.recognition_history_model import (
    EXPORT_COL,
    EXPORT_ROLE,
    RecognitionHistoryModel,
    RecognitionRow,
)
from tcginventory.ui.views._worker import retire

_TYPES = ["All", "purchase", "sale", "trade", "adjustment"]
_TYPE_COL, _STATUS_COL = 1, 5

# Bottom-pane columns describing each card in the selected transaction.
_DETAIL_HEADERS = [
    "SKU",
    "Name",
    "Set",
    "#",
    "Purchase Price",
    "Notes",
    "Cond/Grade",
    "Language",
]


def _cond_grade(item: Item) -> str:
    if item.is_graded:
        company = item.grading_company.value if item.grading_company else "?"
        return f"{company} {item.grade or ''}".strip()
    return item.condition.value if item.condition else ""


def _alpha(hex_color: str, a: float) -> QColor:
    c = QColor(hex_color)
    c.setAlphaF(a)
    return c


def _type_color(value: str) -> str:
    t = theme.active()
    return {
        "purchase": t.info,
        "sale": t.pos,
        "trade": t.accent_ink,
        "adjustment": t.ink3,
    }.get(value, t.ink3)


class _PillDelegate(QStyledItemDelegate):
    """Paints the Type and Status columns as rounded, colour-coded pills."""

    def sizeHint(self, option, index) -> QSize:
        if index.column() not in (_TYPE_COL, _STATUS_COL):
            return super().sizeHint(option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        label = (opt.text or "").capitalize()
        # left margin 8 + pad 9·2 + dot 6 + gap 6 + text + right margin 8
        width = 46 + opt.fontMetrics.horizontalAdvance(label)
        return QSize(width, opt.fontMetrics.height() + 10)

    def paint(self, painter, option, index) -> None:
        col = index.column()
        if col not in (_TYPE_COL, _STATUS_COL):
            super().paint(painter, option, index)
            return

        # Draw the themed cell background (selection / zebra) with no text via the
        # style directly — calling super().paint() would re-run initStyleOption and
        # restore the label under our pill — then overlay the pill.
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)
        if not text:
            return

        fg, fill, border, dot = self._colors(col, text, index)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        fm = opt.fontMetrics
        label = text.capitalize()
        dot_d, pad_x, gap = 6, 9, 6
        text_w = fm.horizontalAdvance(label)
        pill_h = fm.height() + 4
        pill_w = pad_x * 2 + dot_d + gap + text_w
        x = option.rect.left() + 8
        y = option.rect.center().y() - pill_h / 2
        rect = QRectF(x, y, pill_w, pill_h)
        path = QPainterPath()
        path.addRoundedRect(rect, pill_h / 2, pill_h / 2)
        painter.fillPath(path, QColor(fill))
        if border is not None:
            painter.setPen(QPen(border, 1))
            painter.drawPath(path)
        cy = rect.center().y()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(dot))
        painter.drawEllipse(QRectF(x + pad_x, cy - dot_d / 2, dot_d, dot_d))
        painter.setPen(QColor(fg))
        painter.setFont(opt.font)
        painter.drawText(
            QRectF(x + pad_x + dot_d + gap, y, text_w + 2, pill_h),
            int(Qt.AlignVCenter | Qt.AlignLeft),
            label,
        )
        painter.restore()

    def _colors(self, col, text, index):
        """(fg, fill, border, dot) for the pill in ``col``."""
        t = theme.active()
        voided = index.siblingAtColumn(_STATUS_COL).data() == "voided"
        if col == _TYPE_COL:
            fg = t.ink3 if voided else _type_color(text)
            fill = _alpha(t.surface2 if voided else fg, 1.0 if voided else 0.15)
            return fg, fill, None, fg
        # status — a quieter ghost pill
        fg = t.ink3 if text == "voided" else t.ink2
        return (
            fg,
            QColor(t.surface),
            QColor(t.line if text == "voided" else t.line2),
            QColor(t.neg if text == "voided" else t.pos),
        )


def _source_color(value: str) -> str:
    t = theme.active()
    return {
        "transaction": t.info,
        "listing": t.accent_ink,
        "auction": t.warn,
        "manual": t.ink3,
    }.get(value, t.ink3)


def _state_color(value: str) -> str:
    t = theme.active()
    return {"labeled": t.pos, "partial": t.warn, "unlabeled": t.ink3}.get(value, t.ink3)


class _RecPillDelegate(_PillDelegate):
    """Recognition-log pills: Source (col 1) and State (col 5), both filled and
    tinted like the transaction Type pill."""

    def _colors(self, col, text, index):
        key = (text or "").lower()
        fg = _source_color(key) if col == _TYPE_COL else _state_color(key)
        return fg, _alpha(fg, 0.15), None, fg


class _ThumbDelegate(QStyledItemDelegate):
    """Paints an overlapping stack of up to 3 card thumbnails + a "+N" overflow
    chip in the Cards column — the same preview the auctions watchlist uses.

    Thumbnails come from :mod:`tcginventory.ui.thumbcache`, so each image is
    decoded once per session rather than on every repaint."""

    _W, _H, _STEP = 24, 32, 16
    _NAMESPACE = "card"  # catalog art, keyed by card id

    def sizeHint(self, option, index) -> QSize:
        count = (index.data(CARDS_ROLE) or ([], 0))[1]
        slots = max(1, min(3, count)) + (1 if count > 3 else 0)
        return QSize(16 + self._W + (slots - 1) * self._STEP, self._H + 8)

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)

        thumb_ids, count = index.data(CARDS_ROLE) or ([], 0)
        if count <= 0:
            return
        t = theme.active()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        x = option.rect.left() + 8
        y = option.rect.center().y() - self._H / 2
        for i in range(min(3, count)):
            self._card(painter, x, y, self._pixmap(thumb_ids, i), t)
            x += self._STEP
        if count > 3:
            self._overflow(painter, x, y, f"+{count - 3}", opt.font, t)
        painter.restore()

    def _pixmap(self, thumb_ids: list, i: int):
        key = thumb_ids[i] if i < len(thumb_ids) else None
        if not key:
            return None
        return thumbcache.scaled(
            self._NAMESPACE, str(key), self._thumb_path(key), self._W, self._H
        )

    def _thumb_path(self, key):
        return cardimages.cache_path(key)  # catalog art, keyed by card id

    def _card(self, painter, x, y, pm, t) -> None:
        painter.fillPath(
            _rounded(x - 1, y - 1, self._W + 2, self._H + 2, 5), QColor(t.surface)
        )
        face = _rounded(x, y, self._W, self._H, 4)
        if pm is not None:
            painter.save()
            painter.setClipPath(face)
            painter.drawPixmap(int(x), int(y), pm)  # already scaled by thumbcache
            painter.restore()
        else:
            grad = QLinearGradient(x, y, x, y + self._H)
            grad.setColorAt(0, QColor("#4a2f1c"))
            grad.setColorAt(1, QColor("#241a12"))
            painter.fillPath(face, QBrush(grad))

    def _overflow(self, painter, x, y, text, base_font, t) -> None:
        painter.fillPath(
            _rounded(x - 1, y - 1, self._W + 2, self._H + 2, 5), QColor(t.surface)
        )
        painter.fillPath(_rounded(x, y, self._W, self._H, 4), QColor(t.surface2))
        f = QFont(base_font)
        f.setBold(True)
        f.setPointSizeF(8.5)
        painter.setFont(f)
        painter.setPen(QColor(t.ink3))
        painter.drawText(QRectF(x, y, self._W, self._H), int(Qt.AlignCenter), text)


def _rounded(x: float, y: float, w: float, h: float, r: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), r, r)
    return path


# (name, set, number) -> (card id, image url), or None for no match, memoised for
# the session. Art resolution is a catalog query per card, so re-running it on
# every refresh is what made the Cards column repopulate from scratch on each
# visit; with the memo a refresh paints the thumbnails it already knows
# immediately. The *url* is kept as well as the id because the preview gallery
# needs it to fetch a card whose image isn't on disk yet.
# Written from the resolver thread, read on the UI thread: plain dict of
# immutable keys/values, so single assignments are enough — no lock needed.
_ART_MEMO: dict[tuple, tuple[str, str] | None] = {}


def _art_id(ident: tuple) -> str | None:
    """The memoised card id for ``ident`` — None if unknown or unmatched."""
    art = _ART_MEMO.get(ident)
    return art[0] if art else None


def _needs_resolve(idents: list, ids: list) -> bool:
    """True when a row still needs a catalog lookup or an image download; rows
    that are fully memoised and cached skip the resolver entirely."""
    for ident, card_id in zip(idents, ids, strict=False):
        if ident not in _ART_MEMO:
            return True
        if card_id is not None and not cardimages.is_cached(card_id):
            return True
    return False


class _ThumbResolver(QThread):
    """Resolves card art entirely off the UI thread: opens its own connection to
    the DB file, maps each (name, set, number) to a catalog card + image URL,
    caches the bytes, and emits the resolved ids per row as it goes. Keeping the
    catalog lookups off the main thread is what keeps History's refresh instant.
    """

    resolved = Signal(int, int, list)  # (generation, source_row, [card_id|None])

    def __init__(self, db_file: str, generation: int, jobs: list, parent=None) -> None:
        super().__init__(parent)
        self._db_file = db_file
        self._generation = generation
        self._jobs = jobs  # list[(source_row, [(name, set, number), ...])]

    def run(self) -> None:
        conn = connect(self._db_file)
        try:
            for source_row, idents in self._jobs:
                if self.isInterruptionRequested():
                    return
                ids: list[str | None] = []
                for ident in idents:
                    ids.append(self._resolve_one(conn, ident))
                if any(ids):
                    self.resolved.emit(self._generation, source_row, ids)
        finally:
            conn.close()

    @staticmethod
    def _resolve_one(conn, ident: tuple) -> str | None:
        """Art key for one (name, set, number), fetching its image if needed.
        Reuses the session memo unless the image still isn't on disk (a failed or
        never-attempted download), in which case we re-resolve and retry."""
        memoised = ident in _ART_MEMO
        card_id = _art_id(ident)
        if memoised and (card_id is None or cardimages.is_cached(card_id)):
            return card_id
        resolved = cardimages.art_for(conn, *ident)
        card_id = resolved[0] if resolved else None
        _ART_MEMO[ident] = resolved
        if resolved:
            url = resolved[1]
            if url and not cardimages.is_cached(card_id):
                cardimages.ensure_cached(card_id, url)
        return card_id


class _HistoryFilterProxy(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.setSortRole(Qt.UserRole)
        self._type = "All"
        self._text = ""

    def set_type(self, value: str) -> None:
        self._type = value
        self.invalidateFilter()

    def set_text(self, text: str) -> None:
        self._text = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        row = self.sourceModel().row_at(source_row)
        txn = row.transaction
        if self._type != "All" and txn.type.value != self._type:
            return False
        if self._text:
            # Match either the counterparty or any card in the transaction.
            haystack = f"{txn.counterparty or ''} {row.items_label}".lower()
            if self._text not in haystack:
                return False
        return True


class _VoidDialog(QDialog):
    """Confirm reversing a posted transaction — reverses effects, keeps history."""

    def __init__(self, txn, cash: str, margin: str, items: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Void transaction")
        t = theme.active()

        title = QLabel("Void this transaction?")
        title.setStyleSheet("font-size:15px; font-weight:700;")
        body = QLabel(
            "This reverses its effects — restoring the lots it drew down and "
            "backing out the cash — but keeps it visible in history."
        )
        body.setWordWrap(True)
        body.setStyleSheet(f"color:{t.ink2};")

        summary = QFrame()
        summary.setStyleSheet(
            f"background:{t.surface2}; border:1px solid {t.line}; border-radius:10px;"
        )
        s_layout = QVBoxLayout(summary)
        s_layout.setContentsMargins(11, 10, 11, 10)
        s_layout.setSpacing(6)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        chips.addWidget(_pill(txn.type.value.capitalize(), _type_color(txn.type.value)))
        chips.addWidget(_chip(txn.occurred_at, t.ink2))
        if cash:
            chips.addWidget(_chip(cash, _money_tone(txn.cash_delta_cents)))
        if margin:
            chips.addWidget(_chip(f"margin {margin}", t.ink2))
        chips.addStretch(1)
        s_layout.addLayout(chips)
        if items:
            item_lbl = QLabel(items)
            item_lbl.setStyleSheet(f"color:{t.ink2}; font-size:12px;")
            item_lbl.setWordWrap(True)
            s_layout.addWidget(item_lbl)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        confirm = QPushButton("Void transaction")
        confirm.clicked.connect(self.accept)
        confirm.setStyleSheet(
            f"background:{t.accent2}; color:#ffffff; border:none; "
            "border-radius:8px; padding:7px 15px; font-weight:600;"
        )
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)

        layout = QVBoxLayout(self)
        layout.setSpacing(13)
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addWidget(summary)
        layout.addLayout(buttons)
        self.setMinimumWidth(400)


def _money_tone(cents: int) -> str:
    t = theme.active()
    if cents > 0:
        return t.pos
    if cents < 0:
        return t.neg
    return t.ink3


def _pill(text: str, color: str) -> QLabel:
    """A filled, tinted category pill."""
    c = QColor(color)
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color:{color}; "
        f"background:rgba({c.red()},{c.green()},{c.blue()},0.15);"
        "border-radius:9px; padding:2px 9px; font-size:11px; font-weight:600;"
    )
    return lbl


def _chip(text: str, color: str) -> QLabel:
    """A ghost/outline chip (mono) for a value."""
    t = theme.active()
    lbl = QLabel(text)
    lbl.setFont(QFont(theme.mono_family()))
    lbl.setStyleSheet(
        f"color:{color}; background:{t.surface}; border:1px solid {t.line2};"
        "border-radius:9px; padding:2px 9px; font-size:11px;"
    )
    return lbl


def _empty_state(glyph: str, title: str, body: str, action: str) -> tuple:
    """A centred empty-state widget; returns (widget, body_label, action_button)."""
    t = theme.active()
    halo = QLabel()
    halo.setPixmap(icons.pixmap(glyph, t.accent_ink, 34))
    halo.setAlignment(Qt.AlignCenter)
    head = QLabel(title)
    head.setStyleSheet("font-size:16px; font-weight:700;")
    head.setAlignment(Qt.AlignCenter)
    text = QLabel(body)
    text.setStyleSheet(f"color:{t.ink2};")
    text.setWordWrap(True)
    text.setAlignment(Qt.AlignCenter)
    text.setMaximumWidth(420)
    button = QPushButton(action)
    button.setObjectName("primary")

    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setAlignment(Qt.AlignCenter)
    layout.setSpacing(11)
    layout.addWidget(halo)
    layout.addWidget(head)
    layout.addWidget(text, 0, Qt.AlignCenter)
    layout.addWidget(button, 0, Qt.AlignCenter)
    return box, text, button


def _kpi_tile(caption: str) -> tuple[QFrame, QLabel]:
    """A dashboard-style summary tile; returns (frame, value_label)."""
    t = theme.active()
    tile = QFrame()
    tile.setStyleSheet(
        f"QFrame {{ background:{t.surface2}; border:1px solid {t.line};"
        " border-radius:12px; }"
    )
    lay = QVBoxLayout(tile)
    lay.setContentsMargins(16, 10, 20, 11)
    lay.setSpacing(3)
    cap = QLabel(caption.upper())
    cap.setStyleSheet(
        f"color:{t.ink3}; font-size:10px; font-weight:700;"
        " letter-spacing:1px; border:none; background:transparent;"
    )
    value = QLabel("—")
    value.setFont(QFont(theme.mono_family()))
    value.setStyleSheet(
        f"color:{t.ink}; font-size:20px; font-weight:700;"
        " border:none; background:transparent;"
    )
    lay.addWidget(cap)
    lay.addWidget(value)
    return tile, value


def _set_kpi(label: QLabel, text: str, color: str) -> None:
    label.setStyleSheet(
        f"color:{color}; font-size:20px; font-weight:700;"
        " border:none; background:transparent;"
    )
    label.setText(text)


class _Sparkline(QWidget):
    """A compact cumulative-growth line (area fill + 2px accent line + end dot)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._values: list[float] = []
        self.setMinimumHeight(52)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def set_values(self, values) -> None:
        self._values = [float(v) for v in values]
        self.update()

    def paintEvent(self, event) -> None:
        t = theme.active()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h, pad = self.width(), self.height(), 5
        vals = self._values
        if len(vals) < 2:
            p.setPen(QPen(QColor(t.line2), 2))
            p.drawLine(pad, h - pad, w - pad, h - pad)
            return
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
        n = len(vals)

        def point(i: int) -> tuple[float, float]:
            x = pad + (w - 2 * pad) * (i / (n - 1))
            y = (h - pad) - (h - 2 * pad) * ((vals[i] - lo) / span)
            return x, y

        pts = [point(i) for i in range(n)]
        area = QPainterPath()
        area.moveTo(pts[0][0], h - pad)
        for x, y in pts:
            area.lineTo(x, y)
        area.lineTo(pts[-1][0], h - pad)
        area.closeSubpath()
        p.fillPath(area, _alpha(t.accent, 0.16))
        line = QPainterPath()
        line.moveTo(*pts[0])
        for x, y in pts[1:]:
            line.lineTo(x, y)
        p.setPen(QPen(QColor(t.accent), 2))
        p.drawPath(line)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(t.accent_ink))
        ex, ey = pts[-1]
        p.drawEllipse(QRectF(ex - 3, ey - 3, 6, 6))


_REC_SOURCES = ["All", "transaction", "listing", "manual"]


class _RecFilterProxy(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.setSortRole(Qt.UserRole)
        self._source = "All"
        self._text = ""

    def set_source(self, value: str) -> None:
        self._source = value
        self.invalidateFilter()

    def set_text(self, text: str) -> None:
        self._text = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        row = self.sourceModel().row_at(source_row)
        if self._source != "All" and (row.source or "manual") != self._source:
            return False
        if self._text:
            hay = f"{row.note or ''} {row.items_label}".lower()
            if self._text not in hay:
                return False
        return True


class _ExportDelegate(QStyledItemDelegate):
    """Two small icon buttons in the Recognition log's Export column: send this
    capture's labeled cards to the Transaction window (⇆) or start an auction lot
    from them (gavel).

    Painted rather than embedded as widgets, so sorting/filtering can't strand a
    stale button on a row. Captures with nothing labeled show the pair dimmed and
    ignore clicks. Hover is tracked through an event filter on the viewport —
    ``editorEvent`` never sees plain mouse moves, and per-button feedback is what
    makes these read as buttons.
    """

    _W, _H, _GAP, _PAD = 23, 20, 4, 6
    _TARGETS = (  # (glyph, target, tooltip)
        ("arrow-left-right", "transaction", "Export to Transaction"),
        ("gavel", "auction", "Export to Auction"),
    )

    def __init__(self, view, on_export) -> None:
        super().__init__(view)
        self._view = view
        self._on_export = on_export  # (target, capture_id) -> None
        self._hover: tuple[int, int] | None = None  # (row, button index)
        view.setMouseTracking(True)  # so the viewport sends us mouse moves
        # Hold the viewport rather than re-reading view.viewport() in the filter:
        # teardown delivers events after the C++ view is gone, and dereferencing
        # it then raises.
        self._viewport = view.viewport()
        self._viewport.installEventFilter(self)

    def sizeHint(self, option, index) -> QSize:
        slots = len(self._TARGETS)
        return QSize(
            self._PAD * 2 + slots * self._W + (slots - 1) * self._GAP, self._H + 8
        )

    def _button_rects(self, cell) -> list[QRectF]:
        y = cell.center().y() - self._H / 2
        x = cell.left() + self._PAD
        rects = []
        for _ in self._TARGETS:
            rects.append(QRectF(x, y, self._W, self._H))
            x += self._W + self._GAP
        return rects

    def _hit(self, cell, pos) -> int | None:
        for i, rect in enumerate(self._button_rects(cell)):
            if rect.contains(pos):
                return i
        return None

    # -- painting -----------------------------------------------------------

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)

        _capture_id, count = index.data(EXPORT_ROLE) or (None, 0)
        t = theme.active()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        for i, rect in enumerate(self._button_rects(option.rect)):
            glyph = self._TARGETS[i][0]
            live = count > 0
            hovered = live and self._hover == (index.row(), i)
            path = QPainterPath()
            path.addRoundedRect(rect, 6, 6)
            if hovered:
                painter.fillPath(path, QColor(t.accent))
                ink = t.accent_on
            else:
                painter.fillPath(path, QColor(t.surface2))
                painter.setPen(QPen(_alpha(t.line2 if live else t.line, 1.0), 1))
                painter.drawPath(path)
                ink = t.ink2 if live else t.ink3
            pm = icons.pixmap(glyph, ink, 13)
            if not live:
                painter.setOpacity(0.45)
            painter.drawPixmap(
                int(rect.center().x() - 6.5), int(rect.center().y() - 6.5), pm
            )
            painter.setOpacity(1.0)
        painter.restore()

    # -- interaction --------------------------------------------------------

    def editorEvent(self, event, model, option, index) -> bool:
        if event.type() == QEvent.MouseButtonRelease:
            capture_id, count = index.data(EXPORT_ROLE) or (None, 0)
            which = self._hit(option.rect, event.position())
            if which is not None and count > 0 and capture_id is not None:
                self._on_export(self._TARGETS[which][1], capture_id)
                return True
            if which is not None:
                return True  # swallow clicks on a dimmed pair
        return super().editorEvent(event, model, option, index)

    def eventFilter(self, obj, event) -> bool:
        """Track which button the pointer is over, for hover feedback."""
        if obj is self._viewport:
            if event.type() == QEvent.MouseMove:
                self._set_hover(event.position().toPoint())
            elif event.type() == QEvent.Leave:
                self._set_hover(None)
        return super().eventFilter(obj, event)

    def _set_hover(self, pos) -> None:
        hover = None
        if pos is not None:
            index = self._view.indexAt(pos)
            if index.isValid() and index.column() == EXPORT_COL:
                cell = self._view.visualRect(index)
                which = self._hit(cell, pos)
                if which is not None and (index.data(EXPORT_ROLE) or (None, 0))[1] > 0:
                    hover = (index.row(), which)
        if hover != self._hover:
            self._hover = hover
            self._viewport.setCursor(Qt.PointingHandCursor if hover else Qt.ArrowCursor)
            self._viewport.update()

    def helpEvent(self, event, view, option, index) -> bool:
        """Tooltip per button, so the icons don't have to explain themselves."""
        which = self._hit(option.rect, event.pos())
        if which is not None:
            count = (index.data(EXPORT_ROLE) or (None, 0))[1]
            tip = (
                self._TARGETS[which][2]
                if count
                else "Nothing labeled in this capture to export"
            )
            QToolTip.showText(event.globalPos(), tip, view)
            return True
        return super().helpEvent(event, view, option, index)


class _CropThumbDelegate(_ThumbDelegate):
    """Cards column for the Recognition log: thumbnails are the regions cropped
    from their source photos (keyed by region id), not catalog art."""

    _NAMESPACE = "crop"

    def _thumb_path(self, region_id):
        return regioncrops.crop_path(region_id)


class _CropWorker(QThread):
    """Crops labeled regions out of their source photos off the UI thread and
    caches the thumbnails, pinging the view to repaint as they land."""

    progress = Signal(int)  # generation

    def __init__(self, generation: int, jobs: list, parent=None) -> None:
        super().__init__(parent)
        self._generation = generation
        self._jobs = jobs  # list[(region_id, image_path, (x, y, w, h))]

    def run(self) -> None:
        for region_id, path, box in self._jobs:
            if self.isInterruptionRequested():
                return
            if not regioncrops.is_cropped(region_id) and regioncrops.ensure_crop(
                region_id, path, box
            ):
                self.progress.emit(self._generation)


class _AnnotatedImage(QWidget):
    """A source photo scaled to fit, with each region's box drawn on top (accent
    for labeled boxes, muted for the rest)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._regions: list = []  # (x, y, w, h, labeled_bool)
        self.setMinimumSize(240, 220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_image(self, path: str | None, regions: list) -> None:
        self._pixmap = QPixmap(path) if path else None
        self._regions = regions
        self.update()

    def clear(self) -> None:
        self._pixmap = None
        self._regions = []
        self.update()

    def paintEvent(self, event) -> None:
        t = theme.active()
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(t.surface))
        if self._pixmap is None or self._pixmap.isNull():
            p.setPen(QColor(t.ink3))
            p.drawText(
                self.rect(),
                int(Qt.AlignCenter),
                "Select a capture to preview its image",
            )
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        avail = self.rect().adjusted(8, 8, -8, -8)
        scaled = self._pixmap.scaled(
            avail.width(), avail.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        ox = avail.left() + (avail.width() - scaled.width()) // 2
        oy = avail.top() + (avail.height() - scaled.height()) // 2
        p.drawPixmap(ox, oy, scaled)
        p.setRenderHint(QPainter.Antialiasing)
        sw, sh = scaled.width(), scaled.height()
        for x, y, w, h, labeled in self._regions:
            p.setPen(QPen(QColor(t.accent if labeled else t.ink3), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(ox + x * sw, oy + y * sh, w * sw, h * sh), 3, 3)


class _RecHistoryPanel(QWidget):
    """Recognition History — a corpus log in the exact format of the transaction
    log: one row per capture (annotation session), with KPI tiles + a
    cumulative-images sparkline reading corpus growth at a glance. The bottom
    splits table (left) | annotated-image preview with a filmstrip (right)."""

    export_to_transaction = Signal(object)  # [ExportCard] → Transaction window
    export_to_auction = Signal(object)  # [ExportCard] → Auction window

    def __init__(self, conn: sqlite3.Connection, vision_repo=None) -> None:
        super().__init__()
        self._conn = conn
        self._vision = vision_repo  # None → opened lazily on first refresh
        self._cropper: _CropWorker | None = None
        self._parked: list = []  # workers stopping but not yet exited
        self._thumb_gen = 0

        self._model = RecognitionHistoryModel()
        self._proxy = _RecFilterProxy()
        self._proxy.setSourceModel(self._model)

        self._subtitle = QLabel("")
        self._subtitle.setStyleSheet(f"color:{theme.active().ink3}; font-size:12px;")

        # KPI strip: three tiles + the growth sparkline.
        self._img_tile, self._stat_images = _kpi_tile("Images")
        self._lab_tile, self._stat_labeled = _kpi_tile("Labeled crops")
        self._card_tile, self._stat_cards = _kpi_tile("Cards covered")
        spark_frame = self._build_spark_frame()
        stats = QHBoxLayout()
        stats.setSpacing(12)
        stats.addWidget(self._img_tile)
        stats.addWidget(self._lab_tile)
        stats.addWidget(self._card_tile)
        stats.addWidget(spark_frame, 1)

        self._source = QComboBox()
        self._source.addItems(_REC_SOURCES)
        self._source.currentTextChanged.connect(self._on_source_changed)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search cards or note…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_changed)
        filter_bar = QHBoxLayout()
        filter_bar.addWidget(QLabel("Source:"))
        filter_bar.addWidget(self._source)
        filter_bar.addWidget(self._search, 1)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(0, Qt.DescendingOrder)  # newest first
        self._table.setItemDelegate(_RecPillDelegate(self._table))
        self._table.setItemDelegateForColumn(CARDS_COL, _CropThumbDelegate(self._table))
        # Export buttons live in the row itself (where the Note column used to be),
        # so any capture is one click from a Transaction or an auction lot.
        self._table.setItemDelegateForColumn(
            EXPORT_COL, _ExportDelegate(self._table, self._export)
        )
        self._table.verticalHeader().setDefaultSectionSize(40)
        self._table.horizontalHeader().setResizeContentsPrecision(25)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.selectionModel().selectionChanged.connect(self._show_detail)

        self._empty, self._empty_body, empty_btn = _empty_state(
            "scan-line",
            "No captures yet",
            "Supply and label photos in the Recognition window — each saved "
            "session lands here, growing the corpus your detector trains on.",
            "",
        )
        empty_btn.setVisible(False)  # no CTA on this state
        self._no_match, self._no_match_body, clear_btn = _empty_state(
            "search", "No matching captures", "Nothing matches these filters.", "Clear"
        )
        clear_btn.clicked.connect(self._clear_filters)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._table)
        self._stack.addWidget(self._empty)
        self._stack.addWidget(self._no_match)

        # Right half: an annotated-image preview, with a scrollable filmstrip to
        # its left when the selected capture has more than one image.
        self._preview_caption = QLabel("PREVIEW")
        self._preview_caption.setStyleSheet(
            f"color:{theme.active().ink3}; font-size:10px; font-weight:700;"
            " letter-spacing:.6px;"
        )
        self._filmstrip = QListWidget()
        self._filmstrip.setIconSize(QSize(84, 108))
        self._filmstrip.setFixedWidth(112)
        self._filmstrip.setSpacing(4)
        self._filmstrip.setUniformItemSizes(True)
        self._filmstrip.currentRowChanged.connect(self._on_filmstrip_changed)
        self._filmstrip.setVisible(False)
        self._annotated = _AnnotatedImage()
        preview_row = QHBoxLayout()
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_row.setSpacing(8)
        preview_row.addWidget(self._filmstrip)
        preview_row.addWidget(self._annotated, 1)
        preview_box = QWidget()
        pv = QVBoxLayout(preview_box)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(6)
        pv.addWidget(self._preview_caption)
        pv.addLayout(preview_row, 1)
        self._cap_images: list = []  # images of the selected capture (filmstrip)
        self._regions_by_image: dict = {}  # image_id -> [(x, y, w, h, labeled)]

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._stack)  # the capture log, left half
        splitter.addWidget(preview_box)  # the annotated-image preview, right half
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._subtitle)
        layout.addLayout(stats)
        layout.addLayout(filter_bar)
        layout.addWidget(splitter, 1)

    def _build_spark_frame(self) -> QFrame:
        t = theme.active()
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background:{t.surface2}; border:1px solid {t.line};"
            " border-radius:12px; }"
        )
        v = QVBoxLayout(frame)
        v.setContentsMargins(16, 10, 16, 10)
        v.setSpacing(3)
        cap = QLabel("IMAGES OVER TIME")
        cap.setStyleSheet(
            f"color:{t.ink3}; font-size:10px; font-weight:700;"
            " letter-spacing:1px; border:none; background:transparent;"
        )
        self._spark = _Sparkline()
        v.addWidget(cap)
        v.addWidget(self._spark, 1)
        return frame

    # -- export a capture (row buttons in the Export column) -----------------

    def _export(self, target: str, capture_id: int) -> None:
        cards = corpus.capture_export_cards(self._ensure_vision(), capture_id)
        if not cards:
            QMessageBox.information(
                self,
                "Nothing to export",
                "This capture has no labeled cards — open it in the Recognition "
                "window and name its boxes first.",
            )
            return
        if target == "transaction":
            self.export_to_transaction.emit(cards)
        else:
            self.export_to_auction.emit(cards)

    # -- data ---------------------------------------------------------------

    def _ensure_vision(self) -> VisionRepository:
        if self._vision is None:
            self._vision = VisionRepository(open_vision())
        return self._vision

    def refresh(self) -> None:
        vision = self._ensure_vision()
        cards_by_cap: dict[int, list] = {}
        for r in vision.capture_cards():
            cards_by_cap.setdefault(r["capture_id"], []).append(r)

        rows: list[RecognitionRow] = []
        jobs: list[tuple] = []  # (region_id, image_path, (x, y, w, h)) to crop
        for cap in vision.capture_rows():
            cards = cards_by_cap.get(cap["id"], [])
            labels = [
                f"{c['name'] or ''} {c['card_number'] or ''}".strip() for c in cards
            ]
            # Thumbnails are the labeled regions cropped from their photos.
            thumb_ids = [str(c["region_id"]) for c in cards[:3]]
            for c in cards[:3]:
                jobs.append(
                    (c["region_id"], c["image_path"], (c["x"], c["y"], c["w"], c["h"]))
                )
            rows.append(
                RecognitionRow(
                    capture_id=cap["id"],
                    source=cap["source"] or "manual",
                    note=cap["note"] or "",
                    occurred_at=(cap["created_at"] or "")[:10],
                    images=cap["images"],
                    regions=cap["regions"],
                    labeled=cap["labeled"],
                    items_label=", ".join(n for n in labels if n),
                    thumb_ids=thumb_ids,
                    card_count=len(cards),
                )
            )

        self._model.set_rows(rows)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setStretchLastSection(True)
        self._clear_preview()
        self._update_stats(vision)
        self._update_state()
        self._start_cropper(jobs)

    def _update_stats(self, vision: VisionRepository) -> None:
        t = theme.active()
        _set_kpi(self._stat_images, str(vision.image_count()), t.ink)
        _set_kpi(self._stat_labeled, str(vision.labeled_region_count()), t.accent_ink)
        _set_kpi(self._stat_cards, str(vision.labeled_card_count()), t.ink)
        self._spark.set_values([n for _, n in vision.image_growth_series()])

    def _update_subtitle(self) -> None:
        total, shown = self._model.rowCount(), self._proxy.rowCount()
        if total == 0:
            self._subtitle.setText("")
        elif shown == total:
            self._subtitle.setText(f"{total} capture{'s' if total != 1 else ''}")
        else:
            self._subtitle.setText(f"{shown} of {total} shown")

    def _update_state(self) -> None:
        self._update_subtitle()
        if self._model.rowCount() == 0:
            self._stack.setCurrentWidget(self._empty)
        elif self._proxy.rowCount() == 0:
            self._stack.setCurrentWidget(self._no_match)
        else:
            self._stack.setCurrentWidget(self._table)

    # -- filters ------------------------------------------------------------

    def _on_source_changed(self, value: str) -> None:
        self._proxy.set_source(value)
        self._update_state()

    def _on_search_changed(self, text: str) -> None:
        self._proxy.set_text(text)
        self._update_state()

    def _clear_filters(self) -> None:
        self._source.setCurrentText("All")
        self._search.clear()

    # -- Cards thumbnails (region crops made off-thread) ---------------------

    def _start_cropper(self, jobs: list) -> None:
        self._stop_cropper()
        self._thumb_gen += 1
        if not jobs:
            return
        self._cropper = _CropWorker(self._thumb_gen, jobs)
        self._cropper.progress.connect(self._on_crop_done)
        self._cropper.start()

    def _stop_cropper(self) -> None:
        # Parked, not dropped — see _stop_resolver.
        retire(self._cropper, self._parked)
        self._cropper = None

    def _on_crop_done(self, generation: int) -> None:
        if generation == self._thumb_gen:
            thumbcache.forget_misses()  # new crops are on disk now
            self._table.viewport().update()

    # -- preview (annotated image + filmstrip) ------------------------------

    def _selected_capture(self) -> RecognitionRow | None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self._model.row_at(self._proxy.mapToSource(indexes[0]).row())

    def _clear_preview(self) -> None:
        self._cap_images = []
        self._filmstrip.blockSignals(True)
        self._filmstrip.clear()
        self._filmstrip.blockSignals(False)
        self._filmstrip.setVisible(False)
        self._annotated.clear()

    def _show_detail(self) -> None:
        row = self._selected_capture()
        if row is None:
            self._clear_preview()
            return
        vision = self._ensure_vision()
        self._cap_images = vision.capture_images(row.capture_id)
        # Group the capture's regions by image, so each photo shows its own boxes.
        self._regions_by_image: dict[int, list] = {}
        for reg in vision.capture_regions(row.capture_id):
            labeled = bool(reg["card_id"] or (reg["name"] or "").strip())
            self._regions_by_image.setdefault(reg["image_id"], []).append(
                (reg["x"], reg["y"], reg["w"], reg["h"], labeled)
            )

        self._filmstrip.blockSignals(True)
        self._filmstrip.clear()
        for img in self._cap_images:
            item = QListWidgetItem()
            pix = QPixmap(img["path"])
            if not pix.isNull():
                item.setIcon(QIcon(pix))
            self._filmstrip.addItem(item)
        self._filmstrip.setVisible(len(self._cap_images) > 1)
        self._filmstrip.setCurrentRow(0 if self._cap_images else -1)
        self._filmstrip.blockSignals(False)
        self._display_image(0)

    def _on_filmstrip_changed(self, row: int) -> None:
        self._display_image(row)

    def _display_image(self, index: int) -> None:
        if not (0 <= index < len(self._cap_images)):
            self._annotated.clear()
            return
        img = self._cap_images[index]
        regions = self._regions_by_image.get(img["id"], [])
        self._annotated.set_image(img["path"], regions)


_AUC_KINDS = ["All", EVENT_DELETED, EVENT_ARCHIVED, EVENT_UNARCHIVED, EVENT_WON]
_AUC_DETAIL_HEADERS = ["Card", "Set", "Qty", "Market"]


def _event_color(value: str) -> str:
    t = theme.active()
    return {
        EVENT_DELETED: t.neg,
        EVENT_ARCHIVED: t.ink3,
        EVENT_UNARCHIVED: t.info,
        EVENT_WON: t.pos,
    }.get(value, t.ink3)


def _auc_state_color(value: str) -> str:
    t = theme.active()
    return {"restorable": t.accent_ink, "restored": t.info, "logged": t.ink3}.get(
        value, t.ink3
    )


class _AucPillDelegate(_PillDelegate):
    """Auction-log pills: Event (col 1) and State (col 5)."""

    def _colors(self, col, text, index):
        key = (text or "").lower()
        fg = _event_color(key) if col == _TYPE_COL else _auc_state_color(key)
        return fg, _alpha(fg, 0.15), None, fg


class _AucFilterProxy(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self._kind = "All"
        self._text = ""
        self.setSortRole(Qt.UserRole)

    def set_kind(self, value: str) -> None:
        self._kind = value
        self.invalidateFilter()

    def set_text(self, text: str) -> None:
        self._text = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        model = self.sourceModel()
        row = model.row_at(source_row)
        if self._kind != "All" and row.event.kind != self._kind:
            return False
        if self._text:
            haystack = f"{row.event.title} {row.result}".lower()
            if self._text not in haystack:
                return False
        return True


class _AucHistoryPanel(QWidget):
    """Auction History — the lifecycle log of watched eBay auctions, in the same
    format as the transaction log: one row per thing that happened (deleted,
    archived, unarchived, won), with KPI tiles above and, below, what the
    selected event was carrying.

    A ``deleted`` row is restorable: the delete snapshotted the whole bundle
    before ``ON DELETE CASCADE`` took the cards with it, so putting it back is a
    re-insert rather than a re-type.
    """

    changed = Signal()  # a restore put an auction back on the watchlist

    def __init__(self, auctions_conn: sqlite3.Connection | None = None) -> None:
        super().__init__()
        # None (the default) means "no watchlist database in this session" — the
        # pane shows its empty state rather than opening auctions.db behind the
        # caller's back, which is what keeps the bare HistoryView(conn) used
        # across the suite from writing to the user's real file.
        self._conn = auctions_conn
        self._events = (
            AuctionEventRepository(auctions_conn) if auctions_conn is not None else None
        )

        self._model = AuctionHistoryModel()
        self._proxy = _AucFilterProxy()
        self._proxy.setSourceModel(self._model)

        self._subtitle = QLabel("")
        self._subtitle.setStyleSheet(f"color:{theme.active().ink3}; font-size:12px;")

        self._del_tile, self._stat_deleted = _kpi_tile("Deleted")
        self._res_tile, self._stat_restorable = _kpi_tile("Restorable")
        self._won_tile, self._stat_won = _kpi_tile("Won")
        stats = QHBoxLayout()
        stats.setSpacing(12)
        for tile in (self._del_tile, self._res_tile, self._won_tile):
            stats.addWidget(tile)
        stats.addStretch(1)

        self._kind = QComboBox()
        self._kind.addItems(_AUC_KINDS)
        self._kind.currentTextChanged.connect(self._on_kind_changed)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search auction titles…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_changed)
        self._restore_btn = QPushButton("Restore…")
        self._restore_btn.setIcon(icons.icon("rotate-ccw", theme.active().ink2, 15))
        self._restore_btn.setToolTip(
            "Put the selected deleted auction back on the watchlist"
        )
        self._restore_btn.clicked.connect(self._restore_selected)
        filter_bar = QHBoxLayout()
        filter_bar.addWidget(QLabel("Event:"))
        filter_bar.addWidget(self._kind)
        filter_bar.addWidget(self._search, 1)
        filter_bar.addWidget(self._restore_btn)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(0, Qt.DescendingOrder)  # newest first
        self._table.setItemDelegate(_AucPillDelegate(self._table))
        self._table.horizontalHeader().setResizeContentsPrecision(25)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.selectionModel().selectionChanged.connect(self._show_detail)

        self._empty, self._empty_body, empty_btn = _empty_state(
            "gavel",
            "No auction activity yet",
            "Delete, archive, or win a watched auction in the eBay Auctions "
            "window and it lands here — a deleted one stays restorable.",
            "",
        )
        empty_btn.setVisible(False)  # no CTA on this state
        self._no_match, self._no_match_body, clear_btn = _empty_state(
            "search", "No matching events", "Nothing matches these filters.", "Clear"
        )
        clear_btn.clicked.connect(self._clear_filters)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._table)  # 0
        self._stack.addWidget(self._empty)  # 1
        self._stack.addWidget(self._no_match)  # 2

        # Below: what the selected event was carrying, so a restore is an
        # informed click rather than a guess at which bundle this was.
        self._detail_header = QLabel("Select an event to see the cards it covered.")
        self._detail_header.setTextFormat(Qt.RichText)
        self._detail_header.setStyleSheet("border:none; background:transparent;")
        self._detail_band = QFrame()
        band = QHBoxLayout(self._detail_band)
        band.setContentsMargins(14, 9, 14, 9)
        band.addWidget(self._detail_header)
        band.addStretch(1)
        self._detail_table = QTableWidget(0, len(_AUC_DETAIL_HEADERS))
        self._detail_table.setHorizontalHeaderLabels(_AUC_DETAIL_HEADERS)
        self._detail_table.verticalHeader().setVisible(False)
        self._detail_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._detail_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._detail_table.setAlternatingRowColors(True)
        # Card name takes the slack; Qty and Market are narrow numeric columns
        # and look absurd stretched across the pane.
        detail_header = self._detail_table.horizontalHeader()
        detail_header.setStretchLastSection(False)
        detail_header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, len(_AUC_DETAIL_HEADERS)):
            detail_header.setSectionResizeMode(col, QHeaderView.ResizeToContents)

        detail_box = QWidget()
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)
        detail_layout.addWidget(self._detail_band)
        detail_layout.addWidget(self._detail_table, 1)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._stack)
        splitter.addWidget(detail_box)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._subtitle)
        layout.addLayout(stats)
        layout.addLayout(filter_bar)
        layout.addWidget(splitter, 1)

        self._update_state()

    # -- data ---------------------------------------------------------------

    def refresh(self) -> None:
        rows = (
            [AuctionEventRow(e) for e in self._events.list_all()]
            if self._events is not None
            else []
        )
        self._model.set_rows(rows)
        self._table.resizeColumnsToContents()
        self._update_stats(rows)
        self._update_subtitle()
        self._update_state()
        self._clear_detail()

    def _update_stats(self, rows: list[AuctionEventRow]) -> None:
        t = theme.active()
        deleted = sum(1 for r in rows if r.event.kind == EVENT_DELETED)
        restorable = sum(1 for r in rows if r.event.restorable)
        won = sum(1 for r in rows if r.event.kind == EVENT_WON)
        _set_kpi(self._stat_deleted, str(deleted), t.neg if deleted else t.ink)
        _set_kpi(
            self._stat_restorable,
            str(restorable),
            t.accent_ink if restorable else t.ink,
        )
        _set_kpi(self._stat_won, str(won), t.pos if won else t.ink)

    def _update_subtitle(self) -> None:
        total = self._model.rowCount()
        shown = self._proxy.rowCount()
        if total and shown != total:
            self._subtitle.setText(f"{shown} of {total} events")
        else:
            self._subtitle.setText(f"{total} event{'' if total == 1 else 's'}")

    def _update_state(self) -> None:
        if self._model.rowCount() == 0:
            self._stack.setCurrentIndex(1)
        elif self._proxy.rowCount() == 0:
            self._stack.setCurrentIndex(2)
        else:
            self._stack.setCurrentIndex(0)
        event = self._selected_event()
        self._restore_btn.setEnabled(event is not None and event.restorable)

    # -- filters ------------------------------------------------------------

    def _on_kind_changed(self, value: str) -> None:
        self._proxy.set_kind(value)
        self._after_filter()

    def _on_search_changed(self, text: str) -> None:
        self._proxy.set_text(text)
        self._after_filter()

    def _after_filter(self) -> None:
        self._update_subtitle()
        self._update_state()

    def _clear_filters(self) -> None:
        self._kind.setCurrentIndex(0)
        self._search.clear()

    # -- selection / detail -------------------------------------------------

    def _selected_event(self) -> AuctionEvent | None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        source = self._proxy.mapToSource(rows[0])
        return self._model.row_at(source.row()).event

    def _clear_detail(self) -> None:
        self._detail_table.setRowCount(0)
        self._detail_header.setText("Select an event to see the cards it covered.")

    def _show_detail(self) -> None:
        self._update_state()
        event = self._selected_event()
        if event is None:
            self._clear_detail()
            return
        t = theme.active()
        title = event.title or "(untitled)"
        self._detail_header.setText(
            f"<b>{title}</b> &nbsp;·&nbsp; <span style='color:{t.ink2}'>"
            f"{event.kind} · {event.card_count} card"
            f"{'' if event.card_count == 1 else 's'} · {money(event.value_cents)}"
            "</span>"
        )
        cards = event.snapshot_cards()
        self._detail_table.setRowCount(len(cards))
        for i, card in enumerate(cards):
            values = [
                card.name,
                card.set_name or "",
                str(card.quantity),
                money(card.market_value_cents),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col >= 2:
                    item.setTextAlignment(int(Qt.AlignRight | Qt.AlignVCenter))
                    item.setFont(QFont(theme.mono_family()))
                self._detail_table.setItem(i, col, item)
        if not cards:
            # Only a delete carries a payload; say so rather than looking broken.
            self._detail_header.setText(
                self._detail_header.text() + f" &nbsp;<span style='color:{t.ink3}'>"
                "— no card snapshot for this event</span>"
            )

    # -- restore ------------------------------------------------------------

    def _restore_selected(self) -> None:
        event = self._selected_event()
        if event is None or not event.restorable or self._events is None:
            return
        title = event.title or "(untitled)"
        confirm = QMessageBox.question(
            self,
            "Restore auction",
            f"Put “{title}” back on the watchlist?\n\n"
            f"{event.card_count} card{'' if event.card_count == 1 else 's'} "
            f"({money(event.value_cents)}) return as a new watchlist entry — the "
            "original numbering is not reused.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            restored = self._events.restore(event.id)
        except ValueError as exc:
            QMessageBox.warning(self, "Could not restore", str(exc))
            self.refresh()
            return
        self.refresh()
        QMessageBox.information(
            self,
            "Auction restored",
            f"“{restored.title or '(untitled)'}” is back on the watchlist"
            f"{' (archived)' if restored.archived else ''}.",
        )
        self.changed.emit()


class HistoryView(QWidget):
    edit_requested = Signal(int)  # transaction id to edit
    changed = Signal()  # a void changed the ledger
    new_transaction_requested = Signal()  # empty-state CTA → Transaction window
    # Recognition History re-exporting a stored capture (relayed from its panel).
    export_to_transaction = Signal(object)
    export_to_auction = Signal(object)

    def __init__(
        self,
        conn: sqlite3.Connection,
        vision_repo=None,
        auctions_conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__()
        self._conn = conn
        self._vision_repo = vision_repo  # for the Recognition History page
        # Passed in, never opened here: MainWindow hands both this view and
        # AuctionsView the *same* auctions.db connection, and a bare
        # HistoryView(conn) gets an empty Auction History rather than touching
        # the user's real watchlist file.
        self._auctions_conn = auctions_conn
        self._txns = TransactionRepository(conn)
        self._lines = TransactionLineRepository(conn)
        self._consumptions = LotConsumptionRepository(conn)
        self._lots = LotRepository(conn)
        self._items = ItemRepository(conn)
        self._reporting = ReportingService(conn)
        self._service = TransactionService(conn)
        self._db_file = self._resolve_db_file(conn)
        self._resolver: _ThumbResolver | None = None
        self._parked: list = []  # workers stopping but not yet exited
        self._thumb_gen = 0  # bumps each refresh so stale resolver emits are ignored
        # Per-page freshness (0 Transaction, 1 Recognition, 2 Auction): a page is
        # rebuilt on its first show and after a data change, never on a plain
        # mode toggle.
        self._stale = {0: True, 1: True, 2: True}
        # Preview state: one (card_id, image_url) per *detail* row (None when the
        # card has no art), plus which row the pointer is currently over.
        self._detail_art: list[tuple[str, str] | None] = []
        self._hover_row: int | None = None

        self._model = HistoryTableModel()
        self._proxy = _HistoryFilterProxy()
        self._proxy.setSourceModel(self._model)

        # The mode toggle above labels the page, so no separate title here —
        # just the count subtitle (mirrors the Recognition page).
        self._subtitle = QLabel("")
        self._subtitle.setStyleSheet(f"color:{theme.active().ink3}; font-size:12px;")
        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(self._subtitle)
        header.addStretch(1)

        # KPI strip — a summary read before the detail (dashboard tile language).
        self._stat_count_tile, self._stat_count = self._stat_tile("Transactions")
        self._stat_cash_tile, self._stat_cash = self._stat_tile("Net cash")
        self._stat_pl_tile, self._stat_pl = self._stat_tile("Realized P/L")
        stats = QHBoxLayout()
        stats.setSpacing(12)
        for tile in (self._stat_count_tile, self._stat_cash_tile, self._stat_pl_tile):
            stats.addWidget(tile)
        stats.addStretch(1)

        self._type = QComboBox()
        self._type.addItems(_TYPES)
        self._type.currentTextChanged.connect(self._on_filter_changed)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search cards or counterparty…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_changed)

        self._edit_btn = QPushButton("Edit…")
        self._edit_btn.clicked.connect(self._edit_selected)
        self._void_btn = QPushButton("Void…")
        self._void_btn.clicked.connect(self._void_selected)

        filter_bar = QHBoxLayout()
        filter_bar.addWidget(QLabel("Type:"))
        filter_bar.addWidget(self._type)
        filter_bar.addWidget(self._search, 1)
        filter_bar.addWidget(self._edit_btn)
        filter_bar.addWidget(self._void_btn)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(0, Qt.DescendingOrder)  # newest first (today → back)
        self._table.setItemDelegate(_PillDelegate(self._table))
        self._table.setItemDelegateForColumn(CARDS_COL, _ThumbDelegate(self._table))
        self._table.verticalHeader().setDefaultSectionSize(40)  # room for thumbnails
        # Size columns from a small sample of rows, not all of them — resizing to
        # contents over the whole log (with the pill/thumbnail delegates' size
        # hints) is what made refresh slow; 25 rows is ample for these widths.
        self._table.horizontalHeader().setResizeContentsPrecision(25)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.selectionModel().selectionChanged.connect(self._show_detail)

        # Empty states swap in for the table (no transactions / no matches).
        self._empty, self._empty_body, empty_btn = _empty_state(
            "scroll-text",
            "No transactions yet",
            "Record a purchase, sale, or trade and it lands here — with inferred "
            "cost basis, realized margin, and a full per-card drill-down.",
            "Record a transaction",
        )
        empty_btn.clicked.connect(self.new_transaction_requested)
        self._no_match, self._no_match_body, clear_btn = _empty_state(
            "search",
            "No matching transactions",
            "Nothing matches these filters.",
            "Clear filters",
        )
        clear_btn.setObjectName("")
        clear_btn.clicked.connect(self._clear_filters)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._table)  # 0
        self._stack.addWidget(self._empty)  # 1
        self._stack.addWidget(self._no_match)  # 2

        # Bottom pane: a summary band (accent rail) + a table of its cards.
        self._detail_header = QLabel("Select a transaction to see its items.")
        self._detail_header.setTextFormat(Qt.RichText)
        self._detail_header.setStyleSheet("border:none; background:transparent;")
        self._detail_band = QFrame()
        band_layout = QHBoxLayout(self._detail_band)
        band_layout.setContentsMargins(14, 9, 14, 9)
        band_layout.addWidget(self._detail_header)
        band_layout.addStretch(1)
        self._detail_table = QTableWidget(0, len(_DETAIL_HEADERS))
        self._detail_table.setHorizontalHeaderLabels(_DETAIL_HEADERS)
        self._detail_table.verticalHeader().setVisible(False)
        self._detail_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._detail_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._detail_table.setAlternatingRowColors(True)
        self._detail_table.horizontalHeader().setStretchLastSection(True)
        # Hovering a row focuses that card in the gallery; the viewport is held
        # rather than re-read in the filter, because teardown delivers events
        # after the C++ widget is gone (the _ExportDelegate rule).
        self._detail_table.setMouseTracking(True)
        self._detail_viewport = self._detail_table.viewport()
        self._detail_viewport.installEventFilter(self)
        self._detail_table.itemSelectionChanged.connect(self._apply_focus)

        # …and to its right, every card in the transaction, the same CardGallery
        # the auction editor and Price Lookup use.
        gallery_caption = QLabel("CARDS IN THIS TRANSACTION")
        gallery_caption.setStyleSheet(
            f"color:{theme.active().ink3}; font-size:10px; font-weight:700;"
            " letter-spacing:.6px;"
        )
        self._gallery = CardGallery(empty_hint="Select a transaction to see its cards.")
        # Enough for a whole 150×210 thumbnail row, so the default split never
        # opens on a rank of cards sliced off at the chin.
        self._gallery.setMinimumHeight(226)
        gallery_box = QWidget()
        gallery_layout = QVBoxLayout(gallery_box)
        gallery_layout.setContentsMargins(0, 0, 0, 0)
        gallery_layout.setSpacing(8)
        gallery_layout.addWidget(gallery_caption)
        gallery_layout.addWidget(self._gallery, 1)
        gallery_box.setMinimumWidth(330)  # two 150px thumbnails per row

        cards_split = QSplitter(Qt.Horizontal)
        cards_split.addWidget(self._detail_table)  # the lots, left
        cards_split.addWidget(gallery_box)  # their card images, right
        cards_split.setStretchFactor(0, 2)  # 8 columns need the room
        cards_split.setStretchFactor(1, 1)

        detail_box = QWidget()
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)
        detail_layout.addWidget(self._detail_band)  # spans both, it's the summary
        detail_layout.addWidget(cards_split, 1)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._stack)
        splitter.addWidget(detail_box)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        txn_page = QWidget()
        txn_layout = QVBoxLayout(txn_page)
        txn_layout.setContentsMargins(0, 0, 0, 0)
        txn_layout.setSpacing(12)
        txn_layout.addLayout(header)
        txn_layout.addLayout(stats)
        txn_layout.addLayout(filter_bar)
        txn_layout.addWidget(splitter, 1)

        self._rec = _RecHistoryPanel(self._conn, vision_repo=self._vision_repo)
        self._rec.export_to_transaction.connect(self.export_to_transaction)
        self._rec.export_to_auction.connect(self.export_to_auction)
        self._auc = _AucHistoryPanel(auctions_conn=self._auctions_conn)
        # A restore puts an auction back on the watchlist, so the eBay Auctions
        # window is out of date — the same `changed` every other mutation uses.
        self._auc.changed.connect(self.changed)
        self._pages = QStackedWidget()
        self._pages.addWidget(txn_page)  # 0 Transaction
        self._pages.addWidget(self._rec)  # 1 Recognition
        self._pages.addWidget(self._auc)  # 2 Auction

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addLayout(self._build_mode_toggle())
        layout.addWidget(self._pages, 1)

        self._set_mode(0)  # default: Transaction History (refreshes it)

    # -- mode toggle (Transaction / Recognition / Auction) ------------------

    def _mode_buttons(self):
        """(page index, button) for every page — the one place the toggle's
        membership is written down, so a fourth page is one line."""
        return ((0, self._mode_txn), (1, self._mode_rec), (2, self._mode_auc))

    def _build_mode_toggle(self):
        self._mode_txn = QPushButton("Transaction History")
        self._mode_rec = QPushButton("Recognition History")
        self._mode_auc = QPushButton("Auction History")
        bar = QHBoxLayout()
        bar.setSpacing(8)
        for i, btn in self._mode_buttons():
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, idx=i: self._set_mode(idx))
            bar.addWidget(btn)
        bar.addStretch(1)
        return bar

    def _set_mode(self, index: int) -> None:
        t = theme.active()
        self._pages.setCurrentIndex(index)
        for i, btn in self._mode_buttons():
            active = i == index
            btn.setChecked(active)
            if active:
                btn.setStyleSheet(
                    f"background:{t.accent}; color:{t.accent_on}; border:none;"
                    " border-radius:8px; padding:7px 15px; font-weight:700;"
                )
            else:
                btn.setStyleSheet(
                    f"background:{t.surface2}; color:{t.ink2};"
                    f" border:1px solid {t.line}; border-radius:8px;"
                    " padding:7px 15px; font-weight:600;"
                )
        self._load_page(index)

    # -- KPI strip ----------------------------------------------------------

    def _stat_tile(self, caption: str) -> tuple[QFrame, QLabel]:
        """A dashboard-style summary tile; returns (frame, value_label)."""
        t = theme.active()
        tile = QFrame()
        tile.setStyleSheet(
            f"QFrame {{ background:{t.surface2}; border:1px solid {t.line};"
            " border-radius:12px; }"
        )
        lay = QVBoxLayout(tile)
        lay.setContentsMargins(16, 10, 20, 11)
        lay.setSpacing(3)
        cap = QLabel(caption.upper())
        cap.setStyleSheet(
            f"color:{t.ink3}; font-size:10px; font-weight:700;"
            " letter-spacing:1px; border:none; background:transparent;"
        )
        value = QLabel("—")
        value.setFont(QFont(theme.mono_family()))
        value.setStyleSheet(
            f"color:{t.ink}; font-size:20px; font-weight:700;"
            " border:none; background:transparent;"
        )
        lay.addWidget(cap)
        lay.addWidget(value)
        return tile, value

    @staticmethod
    def _set_stat(label: QLabel, text: str, color: str) -> None:
        label.setStyleSheet(
            f"color:{color}; font-size:20px; font-weight:700;"
            " border:none; background:transparent;"
        )
        label.setText(text)

    def _update_stats(self) -> None:
        t = theme.active()
        self._set_stat(self._stat_count, str(self._model.rowCount()), t.ink)
        net = self._reporting.net_cash_cents()
        self._set_stat(self._stat_cash, money(net), _money_tone(net))
        pl = self._reporting.realized_pl_cents()
        self._set_stat(self._stat_pl, money(pl), _money_tone(pl))

    def _update_subtitle(self) -> None:
        total = self._model.rowCount()
        shown = self._proxy.rowCount()
        if total == 0:
            self._subtitle.setText("")
        elif shown == total:
            self._subtitle.setText(f"{total} transaction{'s' if total != 1 else ''}")
        else:
            self._subtitle.setText(f"{shown} of {total} shown")

    # -- filters ------------------------------------------------------------

    def _on_filter_changed(self, value: str) -> None:
        self._proxy.set_type(value)
        self._update_state()

    def _on_search_changed(self, text: str) -> None:
        self._proxy.set_text(text)
        self._update_state()

    def _clear_filters(self) -> None:
        self._type.setCurrentText("All")
        self._search.clear()

    def refresh(self) -> None:
        """The ledger changed (or this is the first show): both pages are stale —
        reload the visible one now, the other on its next visit."""
        self._stale = dict.fromkeys(self._stale, True)
        self._load_page(self._pages.currentIndex())

    def _load_page(self, index: int) -> None:
        """Rebuild a history page only if it's stale; toggling the mode buttons
        back and forth re-shows the page (thumbnails and all) instantly."""
        if not self._stale.get(index, True):
            return
        if index == 0:
            self._refresh_txn()
        elif index == 1:
            self._rec.refresh()
        else:
            self._auc.refresh()
        self._stale[index] = False

    def _refresh_txn(self) -> None:
        # Build the rows with only cheap data (card *count* for the thumbnail
        # count) plus whatever art this session has already resolved, so revisits
        # paint their thumbnails immediately; anything still unknown (or not yet
        # downloaded) is resolved + fetched off-thread.
        jobs: list[tuple[int, list]] = []
        rows: list[HistoryRow] = []
        for source_row, txn in enumerate(self._txns.list_all()):
            items = self._txn_items(txn.id)
            labels = [f"{it.name} {it.card_number or ''}".strip() for it in items]
            idents = [(it.name, it.set_name, it.card_number) for it in items[:3]]
            thumb_ids = [_art_id(ident) for ident in idents]
            rows.append(
                HistoryRow(
                    transaction=txn,
                    margin_cents=self._reporting.transaction_margin_cents(txn.id),
                    proceeds_cents=self._reporting.transaction_proceeds_cents(txn.id),
                    items_label=", ".join(labels),
                    thumb_ids=thumb_ids,
                    card_count=len(items),
                )
            )
            if idents and _needs_resolve(idents, thumb_ids):
                jobs.append((source_row, idents))
        self._model.set_rows(rows)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setStretchLastSection(True)
        self._clear_detail()
        self._update_stats()
        self._update_state()
        self._start_resolver(jobs)

    def _txn_items(self, transaction_id: int) -> list[Item]:
        items = []
        for line in self._lines.list_for_transaction(transaction_id):
            item = self._items.get(line.item_id)
            if item is not None:
                items.append(item)
        return items

    @staticmethod
    def _resolve_db_file(conn: sqlite3.Connection) -> str | None:
        """Path of the main DB file, or None for an in-memory DB (tests)."""
        try:
            row = conn.execute("PRAGMA database_list").fetchone()
        except sqlite3.Error:
            return None
        path = row[2] if row else ""  # (seq, name, file); '' for :memory:
        return path or None

    def _start_resolver(self, jobs: list) -> None:
        self._stop_resolver()
        self._thumb_gen += 1
        if not jobs:
            return
        if self._db_file is None:
            # In-memory DB (tests) can't be reopened on another thread; resolve
            # inline — the catalog is empty there, so it's cheap and offline.
            self._resolve_inline(jobs)
            return
        self._resolver = _ThumbResolver(self._db_file, self._thumb_gen, jobs)
        self._resolver.resolved.connect(self._on_thumbs_resolved)
        self._resolver.start()

    def _resolve_inline(self, jobs: list) -> None:
        for source_row, idents in jobs:
            ids = [_ThumbResolver._resolve_one(self._conn, ident) for ident in idents]
            if any(ids):
                self._on_thumbs_resolved(self._thumb_gen, source_row, ids)

    def _stop_resolver(self) -> None:
        # Ask the old resolver to exit at its next row boundary; don't block the
        # UI waiting for it (a mid-flight fetch would stall). Stale emits are
        # ignored via the generation guard. It stays parked until it has really
        # exited — dropping it mid-run frees a live QThread and aborts the app.
        retire(self._resolver, self._parked)
        self._resolver = None

    def _on_thumbs_resolved(self, generation: int, source_row: int, ids: list) -> None:
        if generation != self._thumb_gen:
            return  # a newer refresh superseded this resolver
        if 0 <= source_row < self._model.rowCount():
            self._model.row_at(source_row).thumb_ids[:] = ids
            thumbcache.forget_misses()  # freshly downloaded art is on disk now
            self._table.viewport().update()

    def _update_state(self) -> None:
        """Show the table, or an empty state (no transactions / no matches)."""
        self._update_subtitle()
        if self._model.rowCount() == 0:
            self._stack.setCurrentWidget(self._empty)
        elif self._proxy.rowCount() == 0:
            self._no_match_body.setText(
                f"Nothing matches these filters. Clear them to see all "
                f"{self._model.rowCount()} transactions."
            )
            self._stack.setCurrentWidget(self._no_match)
        else:
            self._stack.setCurrentWidget(self._table)

    def _item_labels(self, transaction_id: int) -> list[str]:
        """['Articuno 161/159', 'Charizard 004/102', …] for a transaction."""
        labels = []
        for line in self._lines.list_for_transaction(transaction_id):
            item = self._items.get(line.item_id)
            name = item.name if item else f"item {line.item_id}"
            number = (item.card_number if item else None) or ""
            labels.append(f"{name} {number}".strip())
        return labels

    def _selected_transaction(self):
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        source = self._proxy.mapToSource(indexes[0])
        return self._model.row_at(source.row()).transaction

    def _edit_selected(self) -> None:
        txn = self._selected_transaction()
        if txn is None:
            QMessageBox.information(self, "Edit", "Select a transaction first.")
            return
        if txn.status is TransactionStatus.VOIDED:
            QMessageBox.information(self, "Edit", "That transaction is voided.")
            return
        self.edit_requested.emit(txn.id)

    def _void_selected(self) -> None:
        txn = self._selected_transaction()
        if txn is None:
            QMessageBox.information(self, "Void", "Select a transaction first.")
            return
        if txn.status is TransactionStatus.VOIDED:
            QMessageBox.information(self, "Void", "That transaction is already voided.")
            return
        row = self._selected_row()
        margin = ""
        if row and row.proceeds_cents:
            pct = percent(self._reporting.gross_margin_pct(txn.id))
            margin = f"{money(row.margin_cents)} ({pct})"
        dialog = _VoidDialog(
            txn,
            money(txn.cash_delta_cents) if txn.cash_delta_cents else "",
            margin,
            ", ".join(self._item_labels(txn.id)),
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self._service.void(txn.id)
        except ServiceError as exc:
            QMessageBox.warning(self, "Could not void", str(exc))
            return
        self.refresh()
        self.changed.emit()

    def _selected_row(self) -> HistoryRow | None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self._model.row_at(self._proxy.mapToSource(indexes[0]).row())

    def _clear_detail(self) -> None:
        t = theme.active()
        self._detail_band.setStyleSheet(
            f"QFrame {{ background:{t.surface}; border:1px solid {t.line};"
            " border-radius:10px; }"
        )
        self._detail_header.setStyleSheet(
            f"color:{t.ink3}; border:none; background:transparent;"
        )
        self._detail_header.setText("Select a transaction to see its items.")
        # Drop the art before emptying the table: clearing it emits a selection
        # change, and _apply_focus must not read the departing transaction's rows.
        self._detail_art = []
        self._hover_row = None
        self._detail_table.setRowCount(0)
        self._gallery.set_cards([])
        self._gallery.show_gallery()

    def _show_detail(self) -> None:
        row = self._selected_row()
        if row is None:
            self._clear_detail()
            return
        txn = row.transaction
        t = theme.active()

        def span(text: str, color: str, bold: bool = False) -> str:
            weight = "font-weight:600;" if bold else ""
            return f'<span style="color:{color};{weight}">{text}</span>'

        parts = [
            span(txn.type.value.title(), _type_color(txn.type.value), bold=True),
            span(txn.occurred_at, t.ink2),
            span(
                txn.status.value.title(),
                t.ink3 if txn.status is TransactionStatus.VOIDED else t.ink2,
            ),
            "Cash "
            + span(money(txn.cash_delta_cents), _money_tone(txn.cash_delta_cents)),
        ]
        if txn.counterparty:
            parts.append(span(txn.counterparty, t.ink))
        if row.proceeds_cents:
            parts.append(
                "Margin "
                + span(
                    f"{money(row.margin_cents)} "
                    f"({percent(self._reporting.gross_margin_pct(txn.id))})",
                    _money_tone(row.margin_cents),
                )
            )
        sep = f'  <span style="color:{t.line2}">·</span>  '
        rail = _type_color(txn.type.value)
        self._detail_band.setStyleSheet(
            f"QFrame {{ background:{t.surface2}; border:1px solid {t.line};"
            f" border-left:3px solid {rail}; border-radius:10px; }}"
        )
        self._detail_header.setStyleSheet(
            f"color:{t.ink2}; border:none; background:transparent;"
        )
        self._detail_header.setText(sep.join(parts))

        detail = self._detail_rows(txn.id)
        self._detail_art = [art for _cells, art in detail]
        self._hover_row = None
        self._detail_table.setRowCount(len(detail))
        mono = QFont(theme.mono_family())
        for r, (values, _art) in enumerate(detail):
            for c, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if c in (0, 4):  # SKU + price → mono
                    cell.setFont(mono)
                if c == 4:  # price → right-aligned
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 0 and text:  # SKU tinted like a link
                    cell.setForeground(QColor(t.accent_ink))
                self._detail_table.setItem(r, c, cell)
        self._detail_table.resizeColumnsToContents()
        self._detail_table.horizontalHeader().setStretchLastSection(True)
        self._rebuild_gallery()

    def _detail_rows(self, transaction_id: int) -> list[tuple[list[str], tuple | None]]:
        """One entry per lot in the transaction: its cells, and the card's
        ``(card_id, image_url)`` art for the preview (None when unmatched)."""
        rows: list[tuple[list[str], tuple | None]] = []
        for line in self._lines.list_for_transaction(transaction_id):
            item = self._items.get(line.item_id)
            if item is None:
                continue
            if line.direction is Direction.IN:
                lots = self._lots.list_for_acquired_line(line.id)
                pairs = [(lot.sku or "", lot.unit_cost_cents) for lot in lots]
            else:  # OUT — draw price basis from the lots it consumed
                pairs = []
                for cons in self._consumptions.list_for_line(line.id):
                    lot = self._lots.get(cons.lot_id)
                    sku = (lot.sku if lot else "") or ""
                    pairs.append((sku, cons.unit_cost_cents))
            if not pairs:  # fallback so the card still shows
                pairs = [("", line.unit_price_cents)]
            art = self._art_for(item)
            for sku, price_cents in pairs:
                rows.append(
                    (
                        [
                            sku,
                            item.name,
                            item.set_name or "",
                            item.card_number or "",
                            money(price_cents),
                            item.notes or "",
                            _cond_grade(item),
                            item.language.value,
                        ],
                        art,
                    )
                )
        return rows

    def _art_for(self, item: Item) -> tuple[str, str] | None:
        """``(card_id, image_url)`` for a card's printed identity, memoised.

        Resolved on demand for the *selected* transaction only — the log itself
        resolves just the three cards its thumbnail stack shows, off-thread. This
        runs on the UI thread because it shares the session memo (so anything the
        thumbnail resolver already found is free) and `_show_detail` is already a
        few repo queries per line; the image *bytes* are still fetched off-thread
        by the gallery's panes.
        """
        ident = (item.name, item.set_name, item.card_number)
        if ident not in _ART_MEMO:
            _ART_MEMO[ident] = cardimages.art_for(self._conn, *ident)
        return _ART_MEMO[ident]

    # -- card preview (gallery of the transaction; one card on hover) --------

    def _rebuild_gallery(self) -> None:
        """Rebuild the preview from the selected transaction's cards.

        Only called when the *selection* changes — hover merely flips the gallery
        between its two modes, so each thumbnail's image loads once and persists
        (the Price Lookup / auction-editor rule).
        """
        border = theme.active().line2  # neutral thumbnail frame
        entries, seen = [], set()
        for art in self._detail_art:
            if not self._usable(art) or art[0] in seen:
                continue  # one thumbnail per card, however many lots it has
            seen.add(art[0])
            entries.append((art[0], art[1], border))
        self._gallery.set_cards(entries)
        self._gallery.show_gallery()

    @staticmethod
    def _usable(art) -> bool:
        """A card is previewable only with both an id and a URL to fetch by."""
        return bool(art and art[0] and art[1])

    def eventFilter(self, obj, event) -> bool:
        if obj is self._detail_viewport:
            try:
                if event.type() == QEvent.MouseMove:
                    index = self._detail_table.indexAt(event.position().toPoint())
                    self._on_detail_hover(index.row() if index.isValid() else None)
                elif event.type() == QEvent.Leave:
                    self._on_detail_hover(None)
            except RuntimeError:
                pass  # our C++ widget is already gone (app teardown)
        return super().eventFilter(obj, event)

    def _on_detail_hover(self, row: int | None) -> None:
        if row != self._hover_row:
            self._hover_row = row
            self._apply_focus()

    def _apply_focus(self) -> None:
        """The hovered row wins; on leave fall back to the selected row, and with
        neither, back to the whole transaction's gallery."""
        row = self._hover_row
        if row is None:
            selected = self._detail_table.selectionModel().selectedRows()
            row = selected[0].row() if selected else None
        art = None
        if row is not None and 0 <= row < len(self._detail_art):
            art = self._detail_art[row]
        if self._usable(art):
            self._gallery.show_single(art[0], art[1])
        else:
            self._gallery.show_gallery()
