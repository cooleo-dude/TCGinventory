"""Two views over one model: the paginated art grid and the sortable list.

Ticket 64. The guarantee under test throughout is that grid and list read the
same sorted, filtered proxy, so they cannot disagree about what is shown.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import (
    CardCatalogRepository,
    CollectionRepository,
    ItemRepository,
    LotRepository,
)
from tcginventory.domain.enums import Category, Condition, Intent
from tcginventory.domain.models import CatalogCard, Collection, Item, Lot
from tcginventory.services.collections import MISSING
from tcginventory.ui.models.collection_model import (
    FILTER_IMPORTABLE,
    FILTER_MISSING,
    NUMBER_COL,
    VALUE_COL,
)


@pytest.fixture()
def view(app, conn):
    from tcginventory.ui.views.collections import CollectionsView

    v = CollectionsView(conn)
    yield v
    v.deleteLater()


def _big(conn, n=60, owned=4):
    """A collection wide enough to need more than one page."""
    CardCatalogRepository(conn).replace_for_language(
        [
            CatalogCard(
                card_id=f"b-{i}",
                name=f"Card {i}",
                set_id="b",
                set_name="Big Set",
                number=str(i),
                printed_total=n,
                artist="Arita" if i % 2 else "Sugimori",
                rarity="Rare" if i % 3 else "Common",
            )
            for i in range(1, n + 1)
        ],
        "EN",
        now_iso(),
    )
    for i in range(1, owned + 1):
        item = ItemRepository(conn).add(
            Item(
                name=f"Card {i}",
                set_name="Big Set",
                card_number=str(i),
                category=Category.SINGLE,
                condition=Condition.NM,
                market_value_cents=100 * i,
            )
        )
        LotRepository(conn).add(
            Lot(
                item_id=item.id,
                acquired_at="2026-08-01",
                quantity=1,
                remaining_qty=1,
                unit_cost_cents=50,
                acquired_market_value_cents=100 * i,
                intent=Intent.RESALE,
            )
        )
    coll = CollectionRepository(conn).add(
        Collection(name="Big Set", template="set", selector="Big Set")
    )
    conn.commit()
    return coll


def _open_big(view, conn, **kw):
    _big(conn, **kw)
    view.resize(700, 420)
    view.refresh()
    view._open(view._nav_order[0])


def _grid_numbers(view) -> list[str]:
    return [
        view._grid.itemAt(i).widget().slot.number for i in range(view._grid.count())
    ]


# ---------------------------------------------------------------------------
# the toggle
# ---------------------------------------------------------------------------


def test_the_toggle_switches_between_grid_and_list(view, conn):
    _open_big(view, conn)
    assert view._view_stack.currentIndex() == 0  # grid by default
    view._set_view_mode("list")
    assert view._view_stack.currentIndex() == 1
    view._set_view_mode("grid")
    assert view._view_stack.currentIndex() == 0


def test_the_pager_belongs_to_the_grid_only(view, conn):
    """The table scrolls, as tables do; pages are a grid affordance."""
    _open_big(view, conn)
    view._set_view_mode("list")
    assert not view._pager.isVisibleTo(view)


def test_both_views_read_the_same_proxy(view, conn):
    """One source of order and filtering — the whole point of the rewrite."""
    _open_big(view, conn)
    assert view._table.model() is view._proxy


# ---------------------------------------------------------------------------
# filtering, shared
# ---------------------------------------------------------------------------


def test_a_state_filter_applies_to_both_views(view, conn):
    _open_big(view, conn, n=60, owned=4)
    assert view._proxy.rowCount() == 60
    view._set_state_filter(FILTER_IMPORTABLE)
    assert view._proxy.rowCount() == 4  # what the table shows
    assert len(view._proxy.visible_slots()) == 4  # and what the grid pages over
    view._set_state_filter(FILTER_MISSING)
    assert view._proxy.rowCount() == 56


def test_only_one_chip_is_active_at_a_time(view, conn):
    _open_big(view, conn)
    view._set_state_filter(FILTER_MISSING)
    assert [k for k, c in view._chips.items() if c.isChecked()] == [FILTER_MISSING]


def test_the_text_filter_narrows_both_views(view, conn):
    _open_big(view, conn, n=60, owned=0)
    view._search.setText("Card 42")
    assert view._proxy.rowCount() == 1
    assert view._proxy.visible_slots()[0].name == "Card 42"


def test_the_text_filter_also_matches_the_artist(view, conn):
    _open_big(view, conn, n=60, owned=0)
    view._search.setText("sugimori")
    assert view._proxy.rowCount() == 30  # every even-numbered card
    assert all(s.artist == "Sugimori" for s in view._proxy.visible_slots())


# ---------------------------------------------------------------------------
# sorting, shared
# ---------------------------------------------------------------------------


def test_the_default_order_is_collector_order_not_lexicographic(view, conn):
    """1, 2, 3 … 10 — not 1, 10, 100, 11. Sorting is on collector_key."""
    _open_big(view, conn, n=60, owned=0)
    numbers = [s.number for s in view._proxy.visible_slots()][:12]
    assert numbers == [str(i) for i in range(1, 13)]


def test_sorting_by_value_uses_cents_not_the_formatted_text(view, conn):
    """As text, "$9.00" sorts after "$100.00"."""
    _open_big(view, conn, n=12, owned=4)
    view._proxy.sort(VALUE_COL, Qt.DescendingOrder)
    values = [
        s.market_value_cents
        for s in view._proxy.visible_slots()
        if s.market_value_cents
    ]
    assert values == sorted(values, reverse=True)


def test_sorting_reaches_the_grid_too(view, conn):
    """Grid sortability was asked for explicitly; it comes from the shared proxy
    rather than from a second code path."""
    _open_big(view, conn, n=60, owned=0)
    ascending = _grid_numbers(view)
    view._proxy.sort(NUMBER_COL, Qt.DescendingOrder)
    view._render_grid_page()
    descending = _grid_numbers(view)
    assert descending != ascending
    assert descending[0] == "60"


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------


def test_the_grid_paginates_rather_than_scrolling(view, conn):
    _open_big(view, conn, n=60, owned=0)
    per = view._cells_per_page()
    assert per < 60, "the fixture must be big enough to need more than one page"
    assert view._grid.count() <= per
    assert view._pager.pages > 1


def test_paging_shows_the_next_slice_of_the_same_order(view, conn):
    _open_big(view, conn, n=60, owned=0)
    per = view._cells_per_page()
    first = _grid_numbers(view)
    view._pager.set_page(1)
    second = _grid_numbers(view)
    assert first and second and first != second
    assert second == [s.number for s in view._proxy.visible_slots(per, per)]


def test_the_page_count_follows_the_filter(view, conn):
    """Filtering to 4 rows must not leave several pages of empty grid."""
    _open_big(view, conn, n=60, owned=4)
    assert view._pager.pages > 1
    view._set_state_filter(FILTER_IMPORTABLE)
    view._render_grid_page()
    assert view._pager.pages == 1
    assert view._grid.count() == 4


def test_opening_another_collection_returns_to_page_one(view, conn):
    _big(conn, n=60, owned=0)
    second = CollectionRepository(conn).add(
        Collection(name="Second", template="set", selector="Big Set")
    )
    conn.commit()
    view.resize(700, 420)
    view.refresh()
    view._open(view._nav_order[0])
    view._pager.set_page(2)
    view._open(second)
    assert view._pager.page == 0


def test_a_page_holds_no_more_cells_than_the_page_size(view, conn):
    """Guards the paging arithmetic: an off-by-one here draws a partial second
    page over the first."""
    _open_big(view, conn, n=60, owned=0)
    per = view._cells_per_page()
    for page in range(view._pager.pages):
        view._pager.set_page(page)
        assert view._grid.count() <= per


# ---------------------------------------------------------------------------
# card art
# ---------------------------------------------------------------------------


def test_an_uncached_card_still_renders_with_its_number(view, conn):
    """Art comes from the disk cache only — nothing downloads on a paint path,
    so a card with no cached image must not leave an empty cell."""
    _open_big(view, conn, n=6, owned=0)
    cell = view._grid.itemAt(0).widget()
    art = cell.findChild(QLabel, "pcCellArtNone")
    assert art is not None
    assert art.text() == cell.slot.number


def test_missing_slots_still_get_cells(view, conn):
    """A gap in a set has to read as a gap, not as absence — an unowned card
    still occupies a cell. Counted against the page size rather than the
    collection length, because the page is deliberately bounded."""
    _open_big(view, conn, n=6, owned=0)
    drawn = view._grid.count()
    assert drawn == min(6, view._cells_per_page())
    assert drawn > 0
    states = {view._grid.itemAt(i).widget().slot.state for i in range(drawn)}
    assert states == {MISSING}


def test_thumbnails_are_bounded_and_never_fetch():
    """Why this cache exists: full-size decodes are ~335 KB each, so a
    2,072-slot collection would have held ~694 MB of pixmaps."""
    from tcginventory.ui import card_thumbs

    card_thumbs.clear()
    assert card_thumbs.thumbnail("no-such-card", 120) is None
    assert card_thumbs.thumbnail(None, 120) is None
    assert card_thumbs.thumbnail("anything", 0) is None
    assert card_thumbs._decode.cache_info().maxsize == 512


def test_thumbnail_height_keeps_card_proportions():
    from tcginventory.ui import card_thumbs

    assert card_thumbs.height_for(245) == 342
    assert card_thumbs.height_for(120) == round(120 * 342 / 245)
