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
