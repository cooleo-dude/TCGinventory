"""Dashboard view — summary tiles + over-time charts.

Charts: net cash and realized gain/loss (the exact, event-based series) plus a
**collection-value** line reconstructed from recorded price history (F-VW-6/7).
Tiles show the current portfolio summary, intent-filterable (F-VW-8).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from PySide6.QtCharts import (
    QChart,
    QDateTimeAxis,
    QLineSeries,
    QValueAxis,
)
from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from tcginventory.domain.enums import Intent
from tcginventory.services.reporting import ReportingService, SeriesPoint
from tcginventory.ui.charts import HoverChartView
from tcginventory.ui.formatting import money
from tcginventory.ui.views.shows import ShowsView

_INTENTS = ["All", "personal", "resale", "ripping"]


def _to_msecs(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = datetime.fromisoformat(iso[:10])
    return dt.timestamp() * 1000.0


class _Tile(QFrame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumWidth(150)
        layout = QVBoxLayout(self)
        self._title = QLabel(title)
        self._title.setStyleSheet(
            "color: palette(windowText); font-size: 11px; font-weight: 600;"
        )
        self._value = QLabel("—")
        self._value.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(self._title)
        layout.addWidget(self._value)

    def set_value(self, text: str) -> None:
        self._value.setText(text)


class DashboardView(QWidget):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._reporting = ReportingService(conn)

        self._intent = QComboBox()
        self._intent.addItems(_INTENTS)
        self._intent.currentTextChanged.connect(self.refresh)

        top = QHBoxLayout()
        top.addWidget(QLabel("<b>Dashboard</b>"))
        top.addStretch(1)
        top.addWidget(QLabel("Intent:"))
        top.addWidget(self._intent)

        self._tiles = {
            key: _Tile(label)
            for key, label in [
                ("cost", "Cost Basis"),
                ("market", "Market Value"),
                ("unrealized", "Unrealized Gain/Loss"),
                ("realized", "Realized Gain/Loss"),
                ("collection", "Collection Value"),
                ("cash", "Net Cash"),
            ]
        }
        tile_grid = QGridLayout()
        for col, tile in enumerate(self._tiles.values()):
            tile_grid.addWidget(tile, 0, col)

        self._chart_view = HoverChartView()
        self._chart_view.setMinimumHeight(300)

        self._note = QLabel("")
        # palette(windowText) adapts to the theme (readable in light and dark).
        self._note.setStyleSheet("color: palette(windowText);")

        # Overview (tiles + chart) then the shows calendar, stacked in a
        # scrollable page so more sections can be added below over time.
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(0, 0, 0, 0)
        overview_layout.addLayout(top)
        overview_layout.addLayout(tile_grid)
        overview_layout.addWidget(self._chart_view, 1)
        overview_layout.addWidget(self._note)

        # Overview stays at its natural height; the calendar section absorbs any
        # extra vertical space so a full month shows at a glance and grows with
        # the window — while the page scrolls once total content exceeds it.
        overview.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._shows = ShowsView(conn)
        self._shows.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(overview)
        page_layout.addWidget(self._shows)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

        self.refresh()

    def _selected_intent(self) -> Intent | None:
        text = self._intent.currentText()
        return None if text == "All" else Intent(text)

    def refresh(self, *_args) -> None:
        intent = self._selected_intent()
        summary = self._reporting.portfolio_summary(intent)
        self._tiles["cost"].set_value(money(summary.cost_basis_cents))
        self._tiles["market"].set_value(money(summary.market_value_cents))
        self._tiles["unrealized"].set_value(money(summary.unrealized_pl_cents))
        self._tiles["realized"].set_value(money(summary.realized_pl_cents))
        self._tiles["collection"].set_value(
            money(self._reporting.collection_value_cents(intent))
        )
        self._tiles["cash"].set_value(money(summary.net_cash_cents))

        money_series = self._reporting.money_movement_series()
        collection_series = self._reporting.collection_value_series(intent)
        self._chart_view.setChart(
            self._build_chart(
                money_series,
                self._reporting.realized_profit_series(intent),
                collection_series,
            )
        )
        if not money_series:
            self._note.setText(
                "No data yet — open the Transaction tab to record your first "
                "purchase, sale, or trade."
            )
        elif not collection_series:
            self._note.setText(
                "Collection Value line needs price history — run "
                "File ▸ Refresh prices to start recording it."
            )
        else:
            self._note.setText("")

    def _build_chart(
        self,
        money_pts: list[SeriesPoint],
        profit_pts: list[SeriesPoint],
        collection_pts: list[SeriesPoint],
    ) -> QChart:
        chart = QChart()
        chart.setTitle("Net cash, realized gain/loss & collection value over time")

        cash_series = _line_series("Net cash", money_pts)
        profit_series = _line_series("Realized Gain/Loss", profit_pts)
        collection_series = _line_series("Collection Value", collection_pts)
        all_pts = money_pts + profit_pts + collection_pts
        if not all_pts:
            chart.setTitle("No transactions yet")
            return chart

        plotted = [cash_series, profit_series]
        if collection_pts:
            plotted.append(collection_series)
        for series in plotted:
            chart.addSeries(series)

        axis_x = QDateTimeAxis()
        axis_x.setFormat("MMM d")
        axis_x.setTitleText("Date")
        chart.addAxis(axis_x, Qt.AlignBottom)

        axis_y = QValueAxis()
        axis_y.setTitleText("USD")
        chart.addAxis(axis_y, Qt.AlignLeft)

        for series in plotted:
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)

        xs = [_to_msecs(pt.at) for pt in all_pts]
        ys = [pt.value_cents / 100.0 for pt in all_pts]
        axis_x.setRange(
            QDateTime.fromMSecsSinceEpoch(int(min(xs))),
            QDateTime.fromMSecsSinceEpoch(int(max(xs))),
        )
        low, high = min(ys + [0.0]), max(ys + [0.0])
        pad = max(1.0, (high - low) * 0.1)
        axis_y.setRange(low - pad, high + pad)
        return chart


def _line_series(name: str, points: list[SeriesPoint]) -> QLineSeries:
    series = QLineSeries()
    series.setName(name)
    for pt in points:
        series.append(_to_msecs(pt.at), pt.value_cents / 100.0)
    return series
