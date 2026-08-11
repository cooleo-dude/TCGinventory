"""Price Lookup view: search/tag shuttle + multi-card overlay (offscreen)."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import type_catalog_search  # noqa: E402
from tcginventory.data.repositories import CardCatalogRepository  # noqa: E402
from tcginventory.domain.models import CatalogCard  # noqa: E402


class _FakeHistory:
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
            CatalogCard(
                card_id="sv9-1",
                name="Pikachu",
                set_id="sv9",
                set_name="Journey Together",
                set_code="JTG",
                number="1",
                language="EN",
            ),
            CatalogCard(
                card_id="cel-5",
                name="Charizard",
                set_id="cel",
                set_name="Celebrations",
                set_code="CEL",
                number="4",
                language="EN",
            ),
        ],
        "t",
    )
    conn.commit()


def _line_count(view):
    from PySide6.QtCharts import QLineSeries

    return sum(isinstance(s, QLineSeries) for s in view._chart.chart().series())


def test_click_updates_graph(app, conn):
    from tcginventory.ui.views.market import PriceLookupView

    _seed(conn)
    view = PriceLookupView(conn)
    view._history = _FakeHistory({"sv9-1": [("2026-06-01", 500), ("2026-07-01", 750)]})
    type_catalog_search(view._search, "Pikachu", view)
    assert view._results.count() == 1
    view._on_click(view._results.item(0))  # clicking updates the graph
    assert "current market" in view._current.text().lower()
    assert _line_count(view) == 1
    view.deleteLater()


def test_shuttle_tag_and_untag(app, conn):
    from tcginventory.ui.views.market import PriceLookupView

    _seed(conn)
    view = PriceLookupView(conn)
    view._history = _FakeHistory(
        {
            "sv9-1": [("2026-06-01", 500), ("2026-07-01", 750)],
            "cel-5": [("2026-06-01", 9000), ("2026-07-01", 12000)],
        }
    )

    type_catalog_search(view._search, "Pikachu", view)
    view._tag("sv9-1")  # move Pikachu → tagged
    assert view._tagged_list.count() == 1
    assert view._results.count() == 0  # removed from the search list

    type_catalog_search(view._search, "Charizard", view)
    assert view._results.count() == 1  # a non-tagged card is listable
    view._tag("cel-5")
    assert view._tagged_list.count() == 2
    assert _line_count(view) == 2  # two graphs overlaid

    view._untag("sv9-1")  # move one back to the search side
    assert view._tagged_list.count() == 1
    assert view._results.count() >= 1  # Pikachu returned to the search list
    assert _line_count(view) == 1
    view.deleteLater()


def test_click_off_drops_untagged_preview_line(app, conn):
    from tcginventory.ui.views.market import PriceLookupView

    _seed(conn)
    view = PriceLookupView(conn)
    view._history = _FakeHistory(
        {
            "sv9-1": [("2026-06-01", 500), ("2026-07-01", 750)],
            "cel-5": [("2026-06-01", 9000), ("2026-07-01", 12000)],
        }
    )
    # Tag one card, preview another → two lines.
    type_catalog_search(view._search, "Charizard", view)
    view._tag("cel-5")
    type_catalog_search(view._search, "Pikachu", view)
    view._on_click(view._results.item(0))
    assert _line_count(view) == 2

    # Clicking off the card removes only the untagged preview line.
    view._clear_preview()
    assert _line_count(view) == 1  # the tagged card's line persists
    view.deleteLater()


def test_overlay_chart_forward_fills_and_single_point(app):
    from tcginventory.ui.views.market import _overlay_chart

    multi = _overlay_chart(
        [
            ("A", [("2026-06-01", 500), ("2026-07-01", 750)], "#cc241d"),
            ("B", [("2026-07-01", 900)], "#458588"),  # single pt → synthesised vertex
        ]
    )
    lines = multi.series()
    assert len(lines) == 2
    assert all(s.count() >= 2 for s in lines)
    assert not _overlay_chart([]).series()  # empty → message chart, no lines


def test_gallery_set_cards_does_not_accumulate(app):
    """Rebuilding the gallery must replace, not stack, thumbnails — a regression
    guard: deleteLater() is deferred, so old frames used to linger on top of the
    new ones and only the last-tagged card showed."""
    from tcginventory.ui.card_gallery import CardGallery
    from tcginventory.ui.card_preview import CardImagePane

    g = CardGallery()
    for n in (1, 2, 3, 2):  # grow then shrink the tagged set
        entries = [(f"c{i}", None, "#cc241d") for i in range(n)]
        g.set_cards(entries)
        assert g._gallery.count() == n
        # No stale panes accumulate under the gallery widget across rebuilds.
        assert len(g._gallery_widget.findChildren(CardImagePane)) == n
    g.deleteLater()
