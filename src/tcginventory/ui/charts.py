"""Shared charting: a QChartView that tracks the cursor along the nearest line.

While the cursor is over the plot, a marker snaps to the line nearest the
cursor at the cursor's x-position, interpolating the value between data points,
and a tooltip shows that value (and date, for time-axis plots). Used by every
chart in the app so the behaviour is consistent.
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCharts import (
    QChart,
    QChartView,
    QDateTimeAxis,
    QLineSeries,
    QScatterSeries,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QToolTip


class HoverChartView(QChartView):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setMouseTracking(True)
        self._marker: QScatterSeries | None = None
        self._lines: list[QLineSeries] = []
        self._x_is_datetime = False

    def setChart(self, chart: QChart) -> None:  # noqa: N802 (Qt override)
        super().setChart(chart)
        self._marker = None
        self._lines = [s for s in chart.series() if isinstance(s, QLineSeries)]
        self._x_is_datetime = any(
            isinstance(a, QDateTimeAxis) for a in chart.axes(Qt.Horizontal)
        )
        if not self._lines:
            return
        marker = QScatterSeries()
        marker.setMarkerSize(11.0)
        marker.setBorderColor(QColor("#ffffff"))
        chart.addSeries(marker)
        for axis in self._lines[0].attachedAxes():
            marker.attachAxis(axis)
        for legend_marker in chart.legend().markers(marker):
            legend_marker.setVisible(False)  # keep the cursor dot out of the legend
        self._marker = marker

    # -- cursor tracking ----------------------------------------------------

    @staticmethod
    def _interp_y(points, x: float):
        """Linearly interpolate a line's y at x (None if x is out of range)."""
        if not points or x < points[0].x() or x > points[-1].x():
            return None
        for a, b in zip(points, points[1:], strict=False):
            if a.x() <= x <= b.x():
                span = b.x() - a.x()
                if span == 0:
                    return a.y()
                return a.y() + (x - a.x()) / span * (b.y() - a.y())
        return points[-1].y()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().mouseMoveEvent(event)
        chart = self.chart()
        if chart is None or self._marker is None or not self._lines:
            return
        pos = event.position()
        if not chart.plotArea().contains(pos):
            self._clear()
            return

        value = chart.mapToValue(pos, self._lines[0])
        x, cursor_y = value.x(), value.y()
        best = None  # (vertical distance, line, y-on-line)
        for line in self._lines:
            y = self._interp_y(line.points(), x)
            if y is None:
                continue
            dist = abs(y - cursor_y)
            if best is None or dist < best[0]:
                best = (dist, line, y)
        if best is None:
            self._clear()
            return

        _, line, y = best
        self._marker.setColor(line.color())
        self._marker.clear()
        self._marker.append(x, y)
        QToolTip.showText(
            event.globalPosition().toPoint(), self._label(line, x, y), self
        )

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().leaveEvent(event)
        self._clear()

    def _clear(self) -> None:
        if self._marker is not None:
            self._marker.clear()
        QToolTip.hideText()

    def _label(self, line: QLineSeries, x: float, y: float) -> str:
        text = f"${y:,.2f}"
        if self._x_is_datetime:
            try:
                when = datetime.fromtimestamp(
                    x / 1000.0, tz=timezone.utc
                ).astimezone()
                text = f"{when.strftime('%b %d, %Y')}: {text}"
            except (ValueError, OSError, OverflowError):
                pass
        name = line.name()
        return f"{name}\n{text}" if name else text
