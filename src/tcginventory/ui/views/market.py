"""Market view — look up *any* card and see its price-history graph.

Backed by the catalog-wide ``pricehistory.db`` (built by the daily sweep), so
it works for every card in the catalog, not just owned ones. Search by name /
set / number, pick a result, and see the card image, current market price, and
its recorded price path.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from PySide6.QtCharts import QChart, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.ui.card_preview import CardImagePane
from tcginventory.ui.charts import HoverChartView
from tcginventory.ui.formatting import money

_CARD_ID_ROLE = Qt.UserRole + 1


def _msecs(day: str) -> float:
    try:
        dt = datetime.fromisoformat(day)
    except ValueError:
        dt = datetime.fromisoformat(day[:10])
    return dt.timestamp() * 1000.0


class MarketView(QWidget):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._catalog = CardCatalogRepository(conn)
        self._history_conn: sqlite3.Connection | None = None
        self._history = None  # PriceHistoryRepository, opened lazily
        self._matches: dict[str, object] = {}

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search any card — name, set, and/or number…")
        self._search.textChanged.connect(self._on_search)
        self._results = QListWidget()
        self._results.currentItemChanged.connect(self._on_select)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self._search)
        left_layout.addWidget(self._results, 1)

        self._title = QLabel("Search for a card to see its price history.")
        self._title.setStyleSheet("font-size: 15px; font-weight: 600;")
        self._title.setWordWrap(True)
        self._current = QLabel("")
        self._current.setStyleSheet("color: #689d6a; font-weight: 600;")
        self._preview = CardImagePane()
        self._chart = HoverChartView()
        self._chart.setMinimumHeight(240)
        self._show_message("Search for a card to see its price history.")

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.addWidget(self._title)
        detail_layout.addWidget(self._current)
        body = QSplitter(Qt.Horizontal)
        body.addWidget(self._preview)
        body.addWidget(self._chart)
        body.setStretchFactor(0, 1)
        body.setStretchFactor(1, 3)
        detail_layout.addWidget(body, 1)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(detail)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)

        layout = QVBoxLayout(self)
        layout.addWidget(split)

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

    # -- search / select ----------------------------------------------------

    def _on_search(self, text: str) -> None:
        self._results.clear()
        self._matches = {}
        if len(text.strip()) < 2:
            return
        for card in self._catalog.search(text, limit=50):
            self._matches[card.card_id] = card
            label = (
                f"{card.name}  ·  {card.set_name or '?'}  #{card.full_number or '?'}"
            )
            item = QListWidgetItem(label)
            item.setData(_CARD_ID_ROLE, card.card_id)
            self._results.addItem(item)

    def _on_select(self, current: QListWidgetItem | None, _prev=None) -> None:
        if current is None:
            return
        card = self._matches.get(current.data(_CARD_ID_ROLE))
        if card is None:
            return
        self._title.setText(
            f"{card.name}  ·  {card.set_name or ''}  "
            f"#{card.full_number or card.number or ''}  ({card.language})"
        )
        self._preview.show_card(card.card_id, getattr(card, "image_small", None))

        hist = self._hist()
        points = hist.history_for_card(card.card_id) if hist else []
        current_cents = hist.current_price(card.card_id) if hist else None
        self._current.setText(
            f"Current market: {money(current_cents)}"
            if current_cents is not None
            else "No market price recorded yet."
        )
        if points:
            self._chart.setChart(_price_chart(card.name, points))
        else:
            self._show_message(
                f"{card.name} — no price history recorded yet "
                "(built by the daily market update)."
            )

    def _show_message(self, text: str) -> None:
        chart = QChart()
        chart.setTitle(text)
        self._chart.setChart(chart)


def _price_chart(name: str, points: list[tuple[str, int]]) -> QChart:
    chart = QChart()
    chart.setTitle(f"{name} — market price")

    pts = [(_msecs(as_of), cents / 100.0) for as_of, cents in points]
    # Carry the last known price forward to today so the line reaches "now".
    today_x = _msecs(date.today().isoformat())
    if pts and pts[-1][0] < today_x:
        pts.append((today_x, pts[-1][1]))
    # A single change-point needs a second vertex to render as a line.
    if len(pts) == 1:
        x, y = pts[0]
        pts.insert(0, (x - 86_400_000, y))  # one day earlier, same price

    series = QLineSeries()
    series.setName("Market price")
    for x, y in pts:
        series.append(x, y)
    chart.addSeries(series)

    axis_x = QDateTimeAxis()
    axis_x.setFormat("MMM d")
    axis_x.setTitleText("Date")
    chart.addAxis(axis_x, Qt.AlignBottom)
    axis_y = QValueAxis()
    axis_y.setTitleText("USD")
    chart.addAxis(axis_y, Qt.AlignLeft)
    series.attachAxis(axis_x)
    series.attachAxis(axis_y)

    xs = [x for x, _ in pts]
    axis_x.setRange(
        QDateTime.fromMSecsSinceEpoch(int(min(xs))),
        QDateTime.fromMSecsSinceEpoch(int(max(xs))),
    )
    ys = [y for _, y in pts]
    low, high = min(ys), max(ys)
    pad = max(1.0, (high - low) * 0.1)
    axis_y.setRange(low - pad, high + pad)
    return chart
