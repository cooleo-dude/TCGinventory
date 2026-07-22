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
    QValueAxis,
)
from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

_DAY_MS = 86_400_000
# Selectable display windows (label, days); None = all recorded data.
_RANGES: list[tuple[str, int | None]] = [
    ("1M", 30),
    ("3M", 90),
    ("6M", 180),
    ("9M", 270),
    ("1Y", 365),
    ("All Time", None),
]


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


def apply_date_range(
    chart: QChart | None, days: int | None, now_ms: float | None = None
) -> None:
    """Zoom a time-series chart's x-axis and rescale the y-axis to fit the
    window. A windowed range shows ``[today − days, today]`` (anchored to
    today); ``None`` = all recorded data (its full extent). No-op for charts
    without a datetime x-axis / value y-axis / lines (e.g. an empty message
    chart)."""
    if chart is None:
        return
    x_axes = [a for a in chart.axes(Qt.Horizontal) if isinstance(a, QDateTimeAxis)]
    y_axes = [a for a in chart.axes(Qt.Vertical) if isinstance(a, QValueAxis)]
    lines = [s for s in chart.series() if isinstance(s, QLineSeries)]
    if not (x_axes and y_axes and lines):
        return
    xs = [p.x() for s in lines for p in s.points()]
    if not xs:
        return
    x_min, x_max = min(xs), max(xs)
    if days is None:
        start, end = x_min, x_max  # All Time → the full recorded extent
    else:
        if now_ms is None:
            now_ms = datetime.now().timestamp() * 1000.0
        # Today-anchored window, clamped to the data so the plot fills with data
        # rather than showing empty margins where there is none.
        start = max(now_ms - days * _DAY_MS, x_min)
        end = min(now_ms, x_max)
        if start >= end:  # the window doesn't overlap the data → show it all
            start, end = x_min, x_max

    # y over the visible window: each line's points inside it, plus its
    # interpolated value at the window's left edge so a line crossing into the
    # window (e.g. a flat reference line) is accounted for at the right height.
    ys: list[float] = []
    for series in lines:
        pts = series.points()
        ys.extend(p.y() for p in pts if start <= p.x() <= end)
        edge = HoverChartView._interp_y(pts, start)
        if edge is not None:
            ys.append(edge)

    x_axes[0].setRange(
        QDateTime.fromMSecsSinceEpoch(int(start)),
        QDateTime.fromMSecsSinceEpoch(int(end)),
    )
    if ys:
        low, high = min(ys), max(ys)
        pad = max(1.0, (high - low) * 0.1)
        y_axes[0].setRange(low - pad, high + pad)


class RangeSelector(QWidget):
    """A row of exclusive range buttons; emits ``changed(days | None)``."""

    changed = Signal(object)

    def __init__(self, default_days: int | None = None, parent=None) -> None:
        super().__init__(parent)
        self._days = default_days
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Range:"))
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for label, days in _RANGES:
            button = QPushButton(label)
            button.setCheckable(True)
            button.setFixedWidth(64)
            button.clicked.connect(lambda _=False, d=days: self._select(d))
            self._group.addButton(button)
            layout.addWidget(button)
            if days == default_days:
                button.setChecked(True)
        layout.addStretch(1)

    def _select(self, days: int | None) -> None:
        self._days = days
        self.changed.emit(days)

    def days(self) -> int | None:
        return self._days


class RangedChartView(QWidget):
    """A :class:`HoverChartView` with a date-range selector bar above it.

    Drop-in for the plain chart view: ``setChart`` / ``chart`` behave the same,
    and the selected range is (re)applied automatically whenever a chart is set
    or the range changes.
    """

    def __init__(self, default_days: int | None = None, parent=None) -> None:
        super().__init__(parent)
        self._range = RangeSelector(default_days)
        self._range.changed.connect(self._on_range_changed)
        self._view = HoverChartView()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._range)
        layout.addWidget(self._view, 1)

    def setChart(self, chart: QChart) -> None:  # noqa: N802 (QChartView-like API)
        self._view.setChart(chart)
        apply_date_range(chart, self._range.days())

    def chart(self) -> QChart | None:
        return self._view.chart()

    def _on_range_changed(self, days: int | None) -> None:
        apply_date_range(self._view.chart(), days)
