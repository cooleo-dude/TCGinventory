"""Price Lookup view: search, chart, and multi-card overlay (offscreen)."""

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
    """Returns per-card history/current price from a dict."""

    def __init__(self, by_card):
        self._by_card = by_card  # card_id -> list[(as_of, cents)]

    def history_for_card(self, card_id, since=None):
        return self._by_card.get(card_id, [])

    def current_price(self, card_id):
        pts = self._by_card.get(card_id)
        return pts[-1][1] if pts else None


def _seed(conn):
    CardCatalogRepository(conn).replace_all(
        [
            # No image_small → CardImagePane won't spawn a fetch thread in tests.
            CatalogCard(card_id="sv9-1", name="Pikachu", set_id="sv9",
                        set_name="Journey Together", set_code="JTG", number="1",
                        language="EN"),
            CatalogCard(card_id="cel-5", name="Charizard", set_id="cel",
                        set_name="Celebrations", set_code="CEL", number="4",
                        language="EN"),
        ],
        "t",
    )
    conn.commit()


def test_select_shows_current_and_charts(app, conn):
    from tcginventory.ui.views.market import PriceLookupView

    _seed(conn)
    view = PriceLookupView(conn)
    view._history = _FakeHistory({"sv9-1": [("2026-06-01", 500), ("2026-07-01", 750)]})
    view._search.setText("Pikachu")
    assert view._results.count() == 1
    view._results.setCurrentRow(0)
    assert "current market" in view._current.text().lower()
    assert view._chart.chart().series()  # previewed the selected card
    view.deleteLater()


def test_tag_persists_and_overlays_multiple(app, conn):
    from tcginventory.ui.views.market import PriceLookupView

    _seed(conn)
    view = PriceLookupView(conn)
    view._history = _FakeHistory({
        "sv9-1": [("2026-06-01", 500), ("2026-07-01", 750)],
        "cel-5": [("2026-06-01", 9000), ("2026-07-01", 12000)],
    })

    def line_count():
        from PySide6.QtCharts import QLineSeries
        return sum(isinstance(s, QLineSeries) for s in view._chart.chart().series())

    view._search.setText("Pikachu")
    view._results.setCurrentRow(0)
    view._tag_selected()
    assert view._tagged_list.count() == 1

    # Select a different card → its preview overlays the tagged one (2 lines).
    view._search.setText("Charizard")
    view._results.setCurrentRow(0)
    assert line_count() == 2

    view._tag_selected()  # now both tagged
    assert view._tagged_list.count() == 2
    assert line_count() == 2

    view._clear_tags()
    assert view._tagged_list.count() == 0
    view.deleteLater()


def test_overlay_chart_forward_fills_and_single_point(app):
    from tcginventory.ui.views.market import _overlay_chart

    multi = _overlay_chart([
        ("A", [("2026-06-01", 500), ("2026-07-01", 750)]),
        ("B", [("2026-07-01", 900)]),  # single point → synthesised second vertex
    ])
    lines = multi.series()
    assert len(lines) == 2
    assert all(s.count() >= 2 for s in lines)

    assert not _overlay_chart([]).series()  # empty → message chart, no lines
