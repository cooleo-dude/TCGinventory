"""Price Lookup view — chart the price history of any card, and overlay several.

Two side-by-side lists act as a shuttle: search results on the left, tagged
cards on the right. Move a card between them with the arrow that appears on the
selected row (or a double-click). Hovering a row shows its image in the (wide)
card display; clicking a row updates the graph. Tagged cards' graphs persist and
overlay for comparison. Backed by the catalog-wide ``pricehistory.db``.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from PySide6.QtCharts import QChart, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDateTime, QEvent, QRect, Qt
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
from tcginventory.ui.card_preview import CardImagePane
from tcginventory.ui.charts import HoverChartView
from tcginventory.ui.formatting import money

_CARD_ID_ROLE = Qt.UserRole + 1
_DAY_MS = 86_400_000
_ARROW_W = 22


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
    """Paint a move arrow on the selected row; clicking it moves that card."""

    def __init__(self, glyph: str, view: QListWidget, on_move) -> None:
        super().__init__(view)
        self._glyph = glyph
        self._view = view
        self._on_move = on_move  # (card_id) -> None

    @staticmethod
    def _arrow_rect(rect: QRect) -> QRect:
        return QRect(rect.right() - _ARROW_W, rect.top(), _ARROW_W, rect.height())

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        if option.state & QStyle.State_Selected:
            painter.save()
            painter.setPen(option.palette.highlightedText().color())
            painter.drawText(
                self._arrow_rect(option.rect), Qt.AlignCenter, self._glyph
            )
            painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        if (
            event.type() == QEvent.MouseButtonRelease
            and index == self._view.currentIndex()
            and self._arrow_rect(option.rect).contains(event.pos())
        ):
            self._on_move(index.data(_CARD_ID_ROLE))
            return True
        return super().editorEvent(event, model, option, index)


def _configure_list(lst: QListWidget) -> None:
    lst.setMouseTracking(True)  # so itemEntered fires on hover
    lst.setUniformItemSizes(True)


class PriceLookupView(QWidget):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._catalog = CardCatalogRepository(conn)
        self._history_conn: sqlite3.Connection | None = None
        self._history = None  # PriceHistoryRepository, opened lazily
        self._cards: dict[str, object] = {}  # card_id -> CatalogCard (any seen)
        self._selected = None  # CatalogCard whose graph is previewed (click)
        self._tagged: dict[str, object] = {}  # card_id -> CatalogCard

        # -- top: search list and tagged list, side by side --------------------
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search a card — name, set, and/or number…")
        self._search.textChanged.connect(self._on_search)
        self._results = QListWidget()
        _configure_list(self._results)
        self._results.setItemDelegate(
            _ArrowDelegate("→", self._results, self._tag)
        )
        self._results.itemEntered.connect(self._on_hover)
        self._results.itemClicked.connect(self._on_click)
        self._results.itemDoubleClicked.connect(
            lambda it: self._tag(it.data(_CARD_ID_ROLE))
        )

        self._tagged_list = QListWidget()
        _configure_list(self._tagged_list)
        self._tagged_list.setItemDelegate(
            _ArrowDelegate("←", self._tagged_list, self._untag)
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

        top = QSplitter(Qt.Horizontal)
        top.addWidget(results_panel)
        top.addWidget(tagged_panel)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 1)

        # -- bottom: (wide) card display beside the overlay chart --------------
        self._preview = CardImagePane()
        self._current = QLabel("Hover a card for its image; click for its graph.")
        self._current.setStyleSheet("color: #689d6a; font-weight: 600;")
        self._chart = HoverChartView()
        self._chart.setMinimumHeight(240)
        chart_panel = QWidget()
        cp = QVBoxLayout(chart_panel)
        cp.setContentsMargins(0, 0, 0, 0)
        cp.addWidget(self._current)
        cp.addWidget(self._chart, 1)

        bottom = QSplitter(Qt.Horizontal)
        bottom.addWidget(self._preview)
        bottom.addWidget(chart_panel)
        bottom.setStretchFactor(0, 2)  # wide card display
        bottom.setStretchFactor(1, 3)

        body = QSplitter(Qt.Vertical)
        body.addWidget(top)
        body.addWidget(bottom)
        body.setStretchFactor(0, 1)
        body.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addWidget(body)
        self._rebuild_chart()

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
                continue  # already on the right; don't list it twice
            self._cards[card.card_id] = card
            self._results.addItem(self._make_item(card))

    def _make_item(self, card) -> QListWidgetItem:
        label = f"{card.name}  ·  {card.set_name or '?'}  #{card.full_number or '?'}"
        item = QListWidgetItem(label)
        item.setData(_CARD_ID_ROLE, card.card_id)
        return item

    # -- hover (image) / click (graph) --------------------------------------

    def _on_hover(self, item: QListWidgetItem) -> None:
        card = self._cards.get(item.data(_CARD_ID_ROLE))
        if card is not None:
            self._preview.show_card(card.card_id, getattr(card, "image_small", None))

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
        self._rebuild_chart()

    def _untag(self, card_id: str) -> None:
        card = self._tagged.pop(card_id, None)
        if card is None:
            return
        self._take(self._tagged_list, card_id)
        # Return it to the search side so it's visibly "moved back".
        self._results.insertItem(0, self._make_item(card))
        self._rebuild_chart()

    # -- chart --------------------------------------------------------------

    def _rebuild_chart(self) -> None:
        hist = self._hist()
        entries: list[tuple[str, list[tuple[str, int]]]] = []
        for card in self._tagged.values():
            pts = hist.history_for_card(card.card_id) if hist else []
            if pts:
                entries.append((_label(card), pts))
        if self._selected is not None and self._selected.card_id not in self._tagged:
            pts = hist.history_for_card(self._selected.card_id) if hist else []
            if pts:
                entries.append((_label(self._selected), pts))
        self._chart.setChart(_overlay_chart(entries))


def _overlay_chart(entries: list[tuple[str, list[tuple[str, int]]]]) -> QChart:
    chart = QChart()
    if not entries:
        chart.setTitle("Click a card for its graph; tag cards to compare.")
        return chart
    chart.setTitle("Price history")
    today_x = _msecs(date.today().isoformat())
    all_x: list[float] = []
    all_y: list[float] = []
    for label, points in entries:
        pts = [(_msecs(a), c / 100.0) for a, c in points]
        if pts and pts[-1][0] < today_x:  # carry last price to today
            pts.append((today_x, pts[-1][1]))
        if len(pts) == 1:  # a lone point needs a second vertex to draw a line
            x, y = pts[0]
            pts.insert(0, (x - _DAY_MS, y))
        series = QLineSeries()
        series.setName(label)
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
