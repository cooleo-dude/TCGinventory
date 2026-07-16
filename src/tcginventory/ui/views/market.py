"""Price Lookup view — chart the price history of any card, and overlay several.

Search any catalog card, see its image + current market price + price path, and
**tag** cards so their graphs persist and overlay on one chart for comparison. A
side panel lists the currently-tagged cards. Backed by the catalog-wide
``pricehistory.db`` (built by the daily market sweep).
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
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.ui.card_preview import CardImagePane
from tcginventory.ui.charts import HoverChartView
from tcginventory.ui.formatting import money

_CARD_ID_ROLE = Qt.UserRole + 1
_DAY_MS = 86_400_000


def _msecs(day: str) -> float:
    try:
        dt = datetime.fromisoformat(day)
    except ValueError:
        dt = datetime.fromisoformat(day[:10])
    return dt.timestamp() * 1000.0


def _label(card) -> str:
    tail = card.set_code or card.set_name or ""
    return f"{card.name} ({tail})" if tail else card.name


class PriceLookupView(QWidget):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._catalog = CardCatalogRepository(conn)
        self._history_conn: sqlite3.Connection | None = None
        self._history = None  # PriceHistoryRepository, opened lazily
        self._matches: dict[str, object] = {}
        self._selected = None  # CatalogCard currently previewed
        self._tagged: dict[str, object] = {}  # card_id -> CatalogCard (persistent)

        # -- left column: search block above the card display, equal sizes -----
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search a card — name, set, and/or number…")
        self._search.textChanged.connect(self._on_search)
        self._results = QListWidget()
        self._results.currentItemChanged.connect(self._on_select)
        search_block = QWidget()
        sb = QVBoxLayout(search_block)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.addWidget(self._search)
        sb.addWidget(self._results, 1)

        self._preview = CardImagePane()
        self._tag_btn = QPushButton("Tag card")
        self._tag_btn.setToolTip("Keep this card's graph on the chart to compare")
        self._tag_btn.clicked.connect(self._tag_selected)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(search_block, 1)  # same size as the card display…
        left_layout.addWidget(self._preview, 1)  # …below it
        left_layout.addWidget(self._tag_btn)

        # -- centre: overlay chart + current price -----------------------------
        centre = QWidget()
        centre_layout = QVBoxLayout(centre)
        centre_layout.setContentsMargins(0, 0, 0, 0)
        self._current = QLabel("Search for a card to see its price history.")
        self._current.setStyleSheet("color: #689d6a; font-weight: 600;")
        self._chart = HoverChartView()
        self._chart.setMinimumHeight(240)
        centre_layout.addWidget(self._current)
        centre_layout.addWidget(self._chart, 1)

        # -- right: tagged-cards panel ----------------------------------------
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Tagged cards"))
        self._tagged_list = QListWidget()
        self._tagged_list.itemDoubleClicked.connect(self._untag_item)
        right_layout.addWidget(self._tagged_list, 1)
        untag = QPushButton("Untag selected")
        untag.clicked.connect(self._untag_selected)
        clear = QPushButton("Clear all")
        clear.clicked.connect(self._clear_tags)
        right_layout.addWidget(untag)
        right_layout.addWidget(clear)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(centre)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(split)
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
        self._selected = card
        self._preview.show_card(card.card_id, getattr(card, "image_small", None))
        hist = self._hist()
        cents = hist.current_price(card.card_id) if hist else None
        self._current.setText(
            f"{_label(card)} — current market: {money(cents)}"
            if cents is not None
            else f"{_label(card)} — no market price recorded yet."
        )
        self._tag_btn.setText(f"Tag {card.name}")
        self._rebuild_chart()

    # -- tagging ------------------------------------------------------------

    def _tag_selected(self) -> None:
        if self._selected is None or self._selected.card_id in self._tagged:
            return
        card = self._selected
        self._tagged[card.card_id] = card
        item = QListWidgetItem(_label(card))
        item.setData(_CARD_ID_ROLE, card.card_id)
        self._tagged_list.addItem(item)
        self._rebuild_chart()

    def _untag_item(self, item: QListWidgetItem) -> None:
        self._tagged.pop(item.data(_CARD_ID_ROLE), None)
        self._tagged_list.takeItem(self._tagged_list.row(item))
        self._rebuild_chart()

    def _untag_selected(self) -> None:
        item = self._tagged_list.currentItem()
        if item is not None:
            self._untag_item(item)

    def _clear_tags(self) -> None:
        self._tagged.clear()
        self._tagged_list.clear()
        self._rebuild_chart()

    # -- chart --------------------------------------------------------------

    def _rebuild_chart(self) -> None:
        hist = self._hist()
        entries: list[tuple[str, list[tuple[str, int]]]] = []
        seen = set()
        for card in self._tagged.values():
            pts = hist.history_for_card(card.card_id) if hist else []
            if pts:
                entries.append((_label(card), pts))
            seen.add(card.card_id)
        # Overlay the currently-selected (untagged) card as a live preview.
        if self._selected is not None and self._selected.card_id not in seen:
            pts = hist.history_for_card(self._selected.card_id) if hist else []
            if pts:
                entries.append((_label(self._selected), pts))
        self._chart.setChart(_overlay_chart(entries))


def _overlay_chart(entries: list[tuple[str, list[tuple[str, int]]]]) -> QChart:
    chart = QChart()
    if not entries:
        chart.setTitle("Search a card, then Tag it to compare price histories.")
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
