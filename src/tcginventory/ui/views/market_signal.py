"""Market Signals screen (spec 06, M-Market).

A read-only analytics table of each resale card's **monetary significance** —
liquidity (sell-through, days-to-sell), markup (realized + unrealized), and price
volatility — from :class:`~tcginventory.services.market_signal.MarketSignalService`.
Liquidity/markup are all-time; volatility is over the selected window. Sortable by
any column; defaults to most-liquid first.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tcginventory.services.market_signal import MarketSignalService
from tcginventory.ui.formatting import percent

_HEADERS = [
    "Card",
    "On hand",
    "Sold",
    "Sell-through",
    "Days to sell",
    "Realized",
    "Markup",
    "Volatility",
]
_WINDOWS = [("30 days", 30), ("90 days", 90), ("1 year", 365)]


class _NumItem(QTableWidgetItem):
    """A right-aligned cell that sorts by an underlying number, not its text."""

    def __init__(self, text: str, value: float | None) -> None:
        super().__init__(text)
        self._value = value if value is not None else float("-inf")
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other) -> bool:
        return self._value < getattr(other, "_value", float("-inf"))


class MarketView(QWidget):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._service = MarketSignalService(conn)

        self._window = QComboBox()
        for label, days in _WINDOWS:
            self._window.addItem(label, days)
        self._window.setCurrentIndex(1)  # 90 days
        self._window.currentIndexChanged.connect(self.refresh)

        self._summary = QLabel("")
        self._summary.setWordWrap(True)

        controls = QHBoxLayout()
        controls.addWidget(self._summary, 1)
        controls.addWidget(QLabel("Volatility window:"))
        controls.addWidget(self._window)

        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSortingEnabled(True)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

        self._empty = QLabel(
            "No resale cards yet. Buy cards for resale and refresh market prices, "
            "then their liquidity, markup, and volatility appear here."
        )
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._empty)

    def refresh(self) -> None:
        signals = self._service.signals(window_days=self._window.currentData())
        overview = self._service.overview(signals)

        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        for m in signals:
            r = self._table.rowCount()
            self._table.insertRow(r)
            title = m.name + (f"  ·  {m.set_name}" if m.set_name else "")
            if m.card_number:
                title += f"  #{m.card_number}"
            self._table.setItem(r, 0, QTableWidgetItem(title))
            self._table.setItem(r, 1, _NumItem(str(m.units_on_hand), m.units_on_hand))
            self._table.setItem(r, 2, _NumItem(str(m.units_sold), m.units_sold))
            self._table.setItem(
                r,
                3,
                _NumItem(
                    percent(m.sell_through * 100)
                    if m.sell_through is not None
                    else "—",
                    m.sell_through,
                ),
            )
            self._table.setItem(
                r,
                4,
                _NumItem(
                    f"{m.avg_days_to_sell:.0f}"
                    if m.avg_days_to_sell is not None
                    else "—",
                    m.avg_days_to_sell,
                ),
            )
            self._table.setItem(
                r, 5, _NumItem(percent(m.realized_margin_pct), m.realized_margin_pct)
            )
            self._table.setItem(
                r,
                6,
                _NumItem(percent(m.unrealized_markup_pct), m.unrealized_markup_pct),
            )
            self._table.setItem(
                r, 7, _NumItem(percent(m.volatility_pct), m.volatility_pct)
            )
        self._table.setSortingEnabled(True)

        has_rows = bool(signals)
        self._table.setVisible(has_rows)
        self._empty.setVisible(not has_rows)
        if has_rows:
            st = (
                percent(overview.overall_sell_through * 100)
                if overview.overall_sell_through is not None
                else "—"
            )
            days = (
                f"{overview.avg_days_to_sell:.0f}d"
                if overview.avg_days_to_sell is not None
                else "—"
            )
            vol = (
                percent(overview.median_volatility_pct)
                if overview.median_volatility_pct is not None
                else "—"
            )
            self._summary.setText(
                f"{overview.tracked} resale cards · {overview.total_sold} of "
                f"{overview.total_acquired} units sold (sell-through {st}) · "
                f"avg {days} to sell · median volatility {vol}"
            )
        else:
            self._summary.setText("")
