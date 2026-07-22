"""Tests for the shared cursor-tracking chart view (HoverChartView)."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCharts import (  # noqa: E402
    QChart,
    QLineSeries,
    QScatterSeries,
    QValueAxis,
)
from PySide6.QtCore import QPointF, Qt  # noqa: E402

from tcginventory.ui.charts import HoverChartView  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_interp_y_between_and_out_of_range():
    pts = [QPointF(0, 10), QPointF(10, 20), QPointF(20, 20)]
    assert HoverChartView._interp_y(pts, 5) == 15  # halfway on the first segment
    assert HoverChartView._interp_y(pts, 0) == 10  # at a vertex
    assert HoverChartView._interp_y(pts, 15) == 20  # flat segment
    assert HoverChartView._interp_y(pts, -1) is None  # before first point
    assert HoverChartView._interp_y(pts, 21) is None  # after last point


def test_set_chart_adds_a_tracking_marker(app):
    chart = QChart()
    line = QLineSeries()
    line.setName("Market price")
    line.append(0, 1)
    line.append(1, 2)
    chart.addSeries(line)
    ax, ay = QValueAxis(), QValueAxis()
    chart.addAxis(ax, Qt.AlignBottom)
    chart.addAxis(ay, Qt.AlignLeft)
    line.attachAxis(ax)
    line.attachAxis(ay)

    view = HoverChartView()
    view.setChart(chart)
    assert view._marker is not None
    assert sum(isinstance(s, QScatterSeries) for s in chart.series()) == 1

    # A chart with no line series (e.g. an empty "select a card" message) → no marker.
    empty = QChart()
    empty.setTitle("nothing")
    view.setChart(empty)
    assert view._marker is None
    view.deleteLater()


_DAY_MS = 86_400_000


def _timeseries_chart():
    from PySide6.QtCharts import QDateTimeAxis

    chart = QChart()
    line = QLineSeries()
    base = 1_700_000_000_000  # arbitrary epoch ms
    for i, y in enumerate([10.0, 20.0, 30.0, 40.0]):  # 0, 30, 60, 90 days
        line.append(base + i * 30 * _DAY_MS, y)
    chart.addSeries(line)
    ax, ay = QDateTimeAxis(), QValueAxis()
    chart.addAxis(ax, Qt.AlignBottom)
    chart.addAxis(ay, Qt.AlignLeft)
    line.attachAxis(ax)
    line.attachAxis(ay)
    return chart, ax, ay, base


def test_apply_date_range_anchors_window_to_today(app):
    from tcginventory.ui.charts import apply_date_range

    chart, ax, ay, base = _timeseries_chart()
    # "Today" = the last data point; a 30-day window → [base+60d, base+90d].
    now_ms = base + 90 * _DAY_MS
    apply_date_range(chart, 30, now_ms=now_ms)

    assert abs(ax.min().toMSecsSinceEpoch() - (base + 60 * _DAY_MS)) < 1000
    assert abs(ax.max().toMSecsSinceEpoch() - now_ms) < 1000  # right edge = today
    # Visible values are 30 (left edge) and 40 — not the earlier 10/20.
    assert ay.max() >= 40.0
    assert ay.min() >= 28.0  # interpolated edge (30), not the out-of-window 10


def test_apply_date_range_fills_plot_when_data_ends_before_today(app):
    from tcginventory.ui.charts import apply_date_range

    chart, ax, _ay, base = _timeseries_chart()
    # Today is 10 days after the last data point.
    now_ms = base + 100 * _DAY_MS
    apply_date_range(chart, 30, now_ms=now_ms)
    # No empty right margin — the right edge clamps to the last data point…
    assert abs(ax.max().toMSecsSinceEpoch() - (base + 90 * _DAY_MS)) < 1000
    # …while the left edge stays today-anchored (today − 30d).
    assert abs(ax.min().toMSecsSinceEpoch() - (now_ms - 30 * _DAY_MS)) < 1000


def test_apply_date_range_falls_back_to_all_data_when_window_misses(app):
    from tcginventory.ui.charts import apply_date_range

    chart, ax, _ay, base = _timeseries_chart()
    # Today is well past the data → a 30-day window contains none of it, so the
    # plot fills with the full recorded extent instead of showing empty space.
    now_ms = base + 400 * _DAY_MS
    apply_date_range(chart, 30, now_ms=now_ms)
    assert abs(ax.min().toMSecsSinceEpoch() - base) < 1000
    assert abs(ax.max().toMSecsSinceEpoch() - (base + 90 * _DAY_MS)) < 1000


def test_apply_date_range_all_time_uses_full_extent(app):
    from tcginventory.ui.charts import apply_date_range

    chart, ax, ay, base = _timeseries_chart()
    apply_date_range(chart, None)  # All Time
    assert abs(ax.min().toMSecsSinceEpoch() - base) < 1000
    assert abs(ax.max().toMSecsSinceEpoch() - (base + 90 * _DAY_MS)) < 1000


def test_apply_date_range_ignores_message_chart(app):
    from tcginventory.ui.charts import apply_date_range

    empty = QChart()
    empty.setTitle("select a card")
    apply_date_range(empty, 30)  # no axes/series → no error


def test_range_selector_emits_days(app):
    from tcginventory.ui.charts import RangeSelector

    sel = RangeSelector(default_days=90)
    assert sel.days() == 90
    seen = []
    sel.changed.connect(seen.append)
    # Click the "1Y" button.
    for b in sel._group.buttons():
        if b.text() == "1Y":
            b.click()
    assert seen == [365]
    assert sel.days() == 365
    sel.deleteLater()


def test_ranged_chart_view_applies_range_on_set(app):
    from datetime import datetime

    from PySide6.QtCharts import QDateTimeAxis

    from tcginventory.ui.charts import RangedChartView

    # 90 days of data ending ~today so a 30-day window overlaps it (real "now").
    now_ms = datetime.now().timestamp() * 1000.0
    chart = QChart()
    line = QLineSeries()
    for i in range(4):
        line.append(now_ms - (90 - i * 30) * _DAY_MS, 10.0 * (i + 1))
    chart.addSeries(line)
    ax, ay = QDateTimeAxis(), QValueAxis()
    chart.addAxis(ax, Qt.AlignBottom)
    chart.addAxis(ay, Qt.AlignLeft)
    line.attachAxis(ax)
    line.attachAxis(ay)

    view = RangedChartView(default_days=30)
    view.setChart(chart)
    assert view.chart() is chart  # delegates to the inner HoverChartView
    x_axis = next(iter(chart.axes(Qt.Horizontal)))
    span_days = (
        x_axis.max().toMSecsSinceEpoch() - x_axis.min().toMSecsSinceEpoch()
    ) / _DAY_MS
    assert abs(span_days - 30) < 2  # a 30-day window is applied
    view.deleteLater()
