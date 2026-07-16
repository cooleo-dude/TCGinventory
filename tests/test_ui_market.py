"""Market view: search any card + render its price-history graph (offscreen)."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tcginventory.data.repositories import CardCatalogRepository  # noqa: E402
from tcginventory.domain.models import CatalogCard  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _FakeHistory:
    def __init__(self, points, current):
        self._points = points
        self._current = current

    def history_for_card(self, card_id, since=None):
        return self._points

    def current_price(self, card_id):
        return self._current


def _seed(conn):
    CardCatalogRepository(conn).replace_all(
        [
            # No image_small → CardImagePane won't spawn a fetch thread in tests.
            CatalogCard(card_id="sv9-1", name="Pikachu", set_id="sv9",
                        set_name="Journey Together", number="1", language="EN"),
        ],
        "t",
    )
    conn.commit()


def test_market_search_selects_and_charts(app, conn):
    from tcginventory.ui.views.market import MarketView

    _seed(conn)
    view = MarketView(conn)
    view._history = _FakeHistory([("2026-06-01", 500), ("2026-07-01", 750)], 750)

    view._search.setText("Pikachu")
    assert view._results.count() == 1

    view._results.setCurrentRow(0)  # fires _on_select
    assert "Current market" in view._current.text()
    assert view._chart.chart().series()  # a price line was plotted
    view.deleteLater()


def test_market_no_history_shows_message(app, conn):
    from tcginventory.ui.views.market import MarketView

    _seed(conn)
    view = MarketView(conn)
    view._history = _FakeHistory([], None)  # nothing recorded yet
    view._search.setText("Pikachu")
    view._results.setCurrentRow(0)
    assert "No market price" in view._current.text()
    assert not view._chart.chart().series()  # message chart, no series
    view.deleteLater()


def test_price_chart_forward_fills_and_handles_single_point(app):
    from tcginventory.ui.views.market import _price_chart

    multi = _price_chart("X", [("2026-06-01", 500), ("2026-07-01", 750)])
    assert multi.series() and multi.series()[0].count() >= 2

    single = _price_chart("Y", [("2026-07-01", 750)])
    assert single.series()[0].count() >= 2  # a second vertex was synthesised
