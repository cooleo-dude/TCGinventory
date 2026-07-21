"""Price Lookup view — chart the price history of any card, and overlay several.

Two side-by-side lists act as a shuttle: search results on the left, tagged
cards on the right. Move a card between them with the hover arrow (or a
double-click). Hovering a card shows it in the display; with nothing hovered the
display becomes a gallery of the tagged cards, each bordered in its graph-line
colour. Clicking a card charts it; tagged cards' graphs persist and overlay.
Clicking off a card drops its (untagged) preview line. Backed by the
catalog-wide ``pricehistory.db``.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from PySide6.QtCharts import QChart, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDateTime, QEvent, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.ui.card_gallery import CardGallery
from tcginventory.ui.charts import RangedChartView
from tcginventory.ui.formatting import money

_CARD_ID_ROLE = Qt.UserRole + 1
_DAY_MS = 86_400_000
_ARROW_W = 22
# Distinct line/border colours cycled by tag order; preview uses a neutral tone.
_PALETTE = [
    "#cc241d", "#458588", "#98971a", "#d79921",
    "#b16286", "#689d6a", "#d65d0e", "#8ec07c",
]
_PREVIEW_COLOR = "#a89984"


def _msecs(day: str) -> float:
    try:
        dt = datetime.fromisoformat(day)
    except ValueError:
        dt = datetime.fromisoformat(day[:10])
    return dt.timestamp() * 1000.0


def _label(card) -> str:
    tail = card.set_code or card.set_name or ""
    return f"{card.name} ({tail})" if tail else card.name


class _ArrowDelegate(QStyledItemDelegate):
    """Paint a move-arrow chip and move the card when it's clicked.

    ``align`` puts the chip on the right (search list — pinned to the visible
    right edge so long names can't push it off-screen) or the left (tagged list —
    adjacent to the card). ``always`` keeps it visible without hovering. The chip
    uses palette highlight colours so it reads in light and dark themes.
    """

    def __init__(
        self, glyph: str, view: QListWidget, on_move, *, align="right", always=False
    ) -> None:
        super().__init__(view)
        self._glyph = glyph
        self._view = view
        self._on_move = on_move  # (card_id) -> None
        self._align = align
        self._always = always

    def _arrow_rect(self, rect: QRect) -> QRect:
        if self._align == "left":
            return QRect(rect.left() + 2, rect.top(), _ARROW_W, rect.height())
        # Right: pin to the viewport's visible right edge, not the item rect.
        right = min(rect.right(), self._view.viewport().width() - 1)
        return QRect(right - _ARROW_W, rect.top(), _ARROW_W, rect.height())

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        option.textElideMode = Qt.ElideRight
        if self._align == "left":  # indent text past the left-hand arrow
            option.rect = option.rect.adjusted(_ARROW_W + 4, 0, 0, 0)
        else:  # reserve room on the right so text never runs under the arrow
            option.rect = option.rect.adjusted(0, 0, -_ARROW_W, 0)

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        if not (self._always or option.state & QStyle.State_MouseOver):
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        box = self._arrow_rect(option.rect).adjusted(1, 3, -1, -3)
        pal = option.palette
        painter.setBrush(pal.color(QPalette.Highlight))
        painter.setPen(pal.color(QPalette.Highlight).darker(130))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(pal.color(QPalette.HighlightedText))
        painter.drawText(box, Qt.AlignCenter, self._glyph)
        painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        if event.type() == QEvent.MouseButtonRelease and self._arrow_rect(
            option.rect
        ).contains(event.pos()):
            self._on_move(index.data(_CARD_ID_ROLE))
            return True
        return super().editorEvent(event, model, option, index)


def _configure_list(lst: QListWidget) -> None:
    lst.setMouseTracking(True)  # so itemEntered fires on hover
    lst.setUniformItemSizes(True)
    lst.setWordWrap(False)
    lst.setTextElideMode(Qt.ElideRight)  # long names don't widen the row
    lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)


class PriceLookupView(QWidget):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._catalog = CardCatalogRepository(conn)
        self._history_conn: sqlite3.Connection | None = None
        self._history = None  # PriceHistoryRepository, opened lazily
        self._cards: dict[str, object] = {}  # card_id -> CatalogCard (any seen)
        self._selected = None  # CatalogCard whose graph is previewed (click)
        self._tagged: dict[str, object] = {}  # ordered: card_id -> CatalogCard
        self._hover_card: str | None = None

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search a card — name, set, and/or number…")
        self._search.textChanged.connect(self._on_search)
        self._results = QListWidget()
        _configure_list(self._results)
        self._results.setItemDelegate(
            _ArrowDelegate("→", self._results, self._tag, align="right")
        )
        self._results.itemEntered.connect(self._on_hover)
        self._results.itemClicked.connect(self._on_click)
        self._results.itemDoubleClicked.connect(
            lambda it: self._tag(it.data(_CARD_ID_ROLE))
        )
        self._tagged_list = QListWidget()
        _configure_list(self._tagged_list)
        self._tagged_list.setItemDelegate(
            _ArrowDelegate(
                "←", self._tagged_list, self._untag, align="left", always=True
            )
        )
        self._tagged_list.itemEntered.connect(self._on_hover)
        self._tagged_list.itemClicked.connect(self._on_click)
        self._tagged_list.itemDoubleClicked.connect(
            lambda it: self._untag(it.data(_CARD_ID_ROLE))
        )

        results_panel = QWidget()
        rp = QVBoxLayout(results_panel)
        rp.setContentsMargins(0, 0, 0, 0)
        rp.addWidget(self._search)
        rp.addWidget(self._results, 1)
        tagged_panel = QWidget()
        tp = QVBoxLayout(tagged_panel)
        tp.setContentsMargins(0, 0, 0, 0)
        tp.addWidget(QLabel("Tagged cards"))
        tp.addWidget(self._tagged_list, 1)
        lists = QSplitter(Qt.Horizontal)
        lists.addWidget(results_panel)
        lists.addWidget(tagged_panel)
        lists.setStretchFactor(0, 1)
        lists.setStretchFactor(1, 1)

        self._display = CardGallery(
            empty_hint="Hover a card to preview it; tag cards to gather them here."
        )
        self._current = QLabel("Hover a card for its image; click for its graph.")
        self._current.setStyleSheet("color: #689d6a; font-weight: 600;")
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(lists, 2)  # shorter columns…
        left_layout.addWidget(self._current)
        left_layout.addWidget(self._display, 3)  # …above the larger card display

        self._chart = RangedChartView(default_days=90)  # 3 Months by default
        self._chart.setMinimumHeight(240)

        body = QSplitter(Qt.Horizontal)
        body.addWidget(left)
        body.addWidget(self._chart)
        body.setStretchFactor(0, 2)
        body.setStretchFactor(1, 3)  # graph takes the whole right side

        layout = QVBoxLayout(self)
        layout.addWidget(body)
        self._rebuild_chart()
        self._refresh_gallery()
        self._update_display()

        from PySide6.QtWidgets import QApplication

        QApplication.instance().installEventFilter(self)

    # -- app event filter: hover→gallery transitions + click-off clears preview

    def eventFilter(self, obj, event) -> bool:
        try:
            if not self.isVisible():
                return False
            etype = event.type()
            if etype == QEvent.MouseButtonPress:
                if self._selected is not None and not self._click_on_card(obj, event):
                    self._clear_preview()
            elif etype == QEvent.Leave and self._is_list_viewport(obj):
                self._set_hover(None)
            elif etype == QEvent.MouseMove and self._is_list_viewport(obj):
                lst = (
                    self._results
                    if obj is self._results.viewport()
                    else self._tagged_list
                )
                if not lst.indexAt(event.position().toPoint()).isValid():
                    self._set_hover(None)
        except RuntimeError:
            pass  # our C++ widget was deleted (teardown)
        return False

    def _is_list_viewport(self, obj) -> bool:
        return obj in (self._results.viewport(), self._tagged_list.viewport())

    def _click_on_card(self, obj, event) -> bool:
        for lst in (self._results, self._tagged_list):
            if obj is lst.viewport():
                return lst.indexAt(event.position().toPoint()).isValid()
        return False

    # -- history db (opened on first use) -----------------------------------

    def _hist(self):
        if self._history is None:
            try:
                from tcginventory.data.pricehistory import (
                    PriceHistoryRepository,
                    open_history,
                )

                self._history_conn = open_history()
                self._history = PriceHistoryRepository(self._history_conn)
            except Exception:
                self._history = None
        return self._history

    # -- search -------------------------------------------------------------

    def _on_search(self, text: str) -> None:
        self._results.clear()
        if len(text.strip()) < 2:
            return
        for card in self._catalog.search(text, limit=50):
            if card.card_id in self._tagged:
                continue
            self._cards[card.card_id] = card
            self._results.addItem(self._make_item(card))

    def _make_item(self, card) -> QListWidgetItem:
        label = f"{card.name}  ·  {card.set_name or '?'}  #{card.full_number or '?'}"
        item = QListWidgetItem(label)
        item.setData(_CARD_ID_ROLE, card.card_id)
        return item

    # -- hover (display) / click (graph) ------------------------------------

    def _on_hover(self, item: QListWidgetItem) -> None:
        self._set_hover(item.data(_CARD_ID_ROLE))

    def _set_hover(self, card_id: str | None) -> None:
        if card_id == self._hover_card:
            return
        self._hover_card = card_id
        self._update_display()

    def _update_display(self) -> None:
        """Flip between the hovered single card and the tagged gallery — cheap;
        the gallery contents are (re)built only by _refresh_gallery."""
        card = self._cards.get(self._hover_card) if self._hover_card else None
        if card is not None:
            self._display.show_single(card.card_id, getattr(card, "image_small", None))
        else:
            self._display.show_gallery()

    def _refresh_gallery(self) -> None:
        """Rebuild the tagged-card gallery (only when the tagged set changes, so
        thumbnail images load once and persist across hover toggles)."""
        entries = [
            (cid, getattr(c, "image_small", None), self._color_for(cid))
            for cid, c in self._tagged.items()
        ]
        self._display.set_cards(entries)

    def _on_click(self, item: QListWidgetItem) -> None:
        card = self._cards.get(item.data(_CARD_ID_ROLE))
        if card is None:
            return
        self._selected = card
        hist = self._hist()
        cents = hist.current_price(card.card_id) if hist else None
        self._current.setText(
            f"{_label(card)} — current market: {money(cents)}"
            if cents is not None
            else f"{_label(card)} — no market price recorded yet."
        )
        self._rebuild_chart()

    # -- colours ------------------------------------------------------------

    def _color_for(self, card_id: str) -> str:
        keys = list(self._tagged.keys())
        if card_id in keys:
            return _PALETTE[keys.index(card_id) % len(_PALETTE)]
        return _PREVIEW_COLOR

    # -- shuttle: tag / untag -----------------------------------------------

    @staticmethod
    def _take(lst: QListWidget, card_id: str) -> None:
        for i in range(lst.count()):
            if lst.item(i).data(_CARD_ID_ROLE) == card_id:
                lst.takeItem(i)
                return

    def _tag(self, card_id: str) -> None:
        card = self._cards.get(card_id)
        if card is None or card_id in self._tagged:
            return
        self._tagged[card_id] = card
        self._take(self._results, card_id)
        self._tagged_list.addItem(self._make_item(card))
        self._hover_card = None  # show the tagged gallery, not the just-tagged card
        self._rebuild_chart()
        self._refresh_gallery()
        self._update_display()

    def _untag(self, card_id: str) -> None:
        card = self._tagged.pop(card_id, None)
        if card is None:
            return
        self._take(self._tagged_list, card_id)
        self._results.insertItem(0, self._make_item(card))
        self._hover_card = None
        self._rebuild_chart()
        self._refresh_gallery()
        self._update_display()

    def _clear_preview(self) -> None:
        if self._selected is None:
            return
        self._selected = None
        self._results.clearSelection()
        self._tagged_list.clearSelection()
        self._current.setText("Hover a card for its image; click for its graph.")
        self._rebuild_chart()

    # -- chart --------------------------------------------------------------

    def _rebuild_chart(self) -> None:
        hist = self._hist()
        entries: list[tuple[str, list[tuple[str, int]], str]] = []
        for card_id, card in self._tagged.items():
            pts = hist.history_for_card(card_id) if hist else []
            if pts:
                entries.append((_label(card), pts, self._color_for(card_id)))
        if self._selected is not None and self._selected.card_id not in self._tagged:
            pts = hist.history_for_card(self._selected.card_id) if hist else []
            if pts:
                entries.append((_label(self._selected), pts, _PREVIEW_COLOR))
        self._chart.setChart(_overlay_chart(entries))


def _overlay_chart(entries: list[tuple[str, list[tuple[str, int]], str]]) -> QChart:
    chart = QChart()
    if not entries:
        chart.setTitle("Click a card for its graph; tag cards to compare.")
        return chart
    chart.setTitle("Price history")
    today_x = _msecs(date.today().isoformat())
    all_x: list[float] = []
    all_y: list[float] = []
    for label, points, color in entries:
        pts = [(_msecs(a), c / 100.0) for a, c in points]
        if pts and pts[-1][0] < today_x:  # carry last price to today
            pts.append((today_x, pts[-1][1]))
        if len(pts) == 1:  # a lone point needs a second vertex to draw a line
            x, y = pts[0]
            pts.insert(0, (x - _DAY_MS, y))
        series = QLineSeries()
        series.setName(label)
        series.setColor(QColor(color))
        for x, y in pts:
            series.append(x, y)
            all_x.append(x)
            all_y.append(y)
        chart.addSeries(series)

    axis_x = QDateTimeAxis()
    axis_x.setFormat("MMM d")
    axis_x.setTitleText("Date")
    chart.addAxis(axis_x, Qt.AlignBottom)
    axis_y = QValueAxis()
    axis_y.setTitleText("USD")
    chart.addAxis(axis_y, Qt.AlignLeft)
    for series in chart.series():
        series.attachAxis(axis_x)
        series.attachAxis(axis_y)
    axis_x.setRange(
        QDateTime.fromMSecsSinceEpoch(int(min(all_x))),
        QDateTime.fromMSecsSinceEpoch(int(max(all_x))),
    )
    low, high = min(all_y), max(all_y)
    pad = max(1.0, (high - low) * 0.1)
    axis_y.setRange(low - pad, high + pad)
    chart.legend().setVisible(True)
    return chart
