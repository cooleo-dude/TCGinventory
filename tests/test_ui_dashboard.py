"""Dashboard chart: the collection-value line appears when price history exists."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCharts import QLineSeries  # noqa: E402

from conftest import buy  # noqa: E402
from tcginventory.data.repositories import PriceSnapshotRepository  # noqa: E402
from tcginventory.domain.models import PriceSnapshot  # noqa: E402
from tcginventory.services.transactions import TransactionService  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _line_names(view) -> set[str]:
    return {
        s.name()
        for s in view._chart_view.chart().series()
        if isinstance(s, QLineSeries)
    }


def test_dashboard_plots_collection_value_line(app, conn):
    from tcginventory.ui.views.dashboard import DashboardView

    p = buy(TransactionService(conn), "Pikachu", 1000, 500, "2026-01-01", quantity=2)
    PriceSnapshotRepository(conn).add(
        PriceSnapshot(
            item_id=p.lots[0].item_id,
            price_cents=600,
            source="t",
            as_of="2026-02-01T00:00:00",
        )
    )
    conn.commit()

    view = DashboardView(conn)
    names = _line_names(view)
    assert "Collection Value" in names
    assert "Realized Gain/Loss" in names
    assert "Net cash" in names
    view.deleteLater()


def test_dashboard_omits_collection_line_without_price_history(app, conn):
    from tcginventory.ui.views.dashboard import DashboardView

    buy(TransactionService(conn), "Pikachu", 1000, 500, "2026-01-01")
    view = DashboardView(conn)
    names = _line_names(view)
    assert "Collection Value" not in names  # no snapshots recorded yet
    assert "Net cash" in names
    view.deleteLater()
