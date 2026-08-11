"""The owned/global toggle on Top Gainers and Top Losers (offscreen).

The ranking service is tested headless in `test_global_movers.py`. What is pinned here
is the part only a widget can get wrong: a row for a card you do **not** hold must be
inert and must not dress itself as a link. That was decision 3 — no navigation for
global rows, because Inventory cannot select what is not in it and Price Lookup is not
settled yet.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tcginventory.services.reporting import MoverRow  # noqa: E402


@pytest.fixture
def columns(app):
    """Owns every column a test builds, and delivers pending deletes before dropping.

    `_PagedBox._clear` drops rows with `deleteLater()`, which only takes effect when an
    event loop delivers the event — and the suite never spins one. These are the first
    tests to clear rows a box *already had* (switching scope does exactly that), so
    they are the first to leave deferred deletes pending. Left to teardown order, the
    column's Python wrapper died first, the C++ object went with it, and the
    module-scoped drain in conftest then delivered DeferredDelete to freed children:
    an access violation, at a site nowhere near the cause.

    So the fixture holds the references, flushes the queue while the parents are still
    alive, and only then lets go.
    """
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    made: list = []
    yield made
    QApplication.instance().sendPostedEvents(None, QEvent.Type.DeferredDelete)
    made.clear()


@pytest.fixture(autouse=True)
def _no_thumbnails(monkeypatch):
    """Bind no card art, so no fetch thread exists to outlive the test.

    `_ImageLoader.bind` queues a `_ThumbFetchWorker` for anything it has no cached art
    for. These tests build a column per test, so the module's teardown `gc.collect()`
    dropped the loader's Python wrapper while its thread was still running and Qt
    aborted the process — `QTHREAD-LIFETIME`, the same shape as ticket 007. Nothing
    here is about thumbnails, so the honest fix is not to ask for any.
    """
    from tcginventory.ui.views.dashboard import _ImageLoader

    monkeypatch.setattr(_ImageLoader, "bind", lambda *a, **k: None)


def _owned(item_id: int = 7, **over) -> MoverRow:
    row = dict(
        item_id=item_id,
        name="Charizard",
        set_name="Base Set",
        card_number="4/102",
        qty=2,
        price_cents=40_000,
        change_cents=5_000,
        change_pct=14.3,
        cost_cents=30_000,
    )
    row.update(over)
    return MoverRow(**row)


def _global(**over) -> MoverRow:
    row = dict(
        item_id=None,
        name="Blastoise",
        set_name="Base Set",
        card_number="2/102",
        qty=0,
        price_cents=20_000,
        change_cents=4_000,
        change_pct=25.0,
        cost_cents=0,
        card_id="base1-2",
        owned_qty=0,
    )
    row.update(over)
    return MoverRow(**row)


def _click(row_widget) -> None:
    """Left-click the row the way a user would (release inside its rect)."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    center = QPointF(row_widget.rect().center())
    row_widget.mouseReleaseEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            center,
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
    )


def _column(columns, conn, title="Top Gainers", *, scoped=True):
    """Build a column and hand it to `columns`, which owns its lifetime."""
    from tcginventory.ui.views.dashboard import MoversColumn, _ImageLoader

    loader = _ImageLoader(conn)
    column = MoversColumn(title, "#ffffff", loader, scoped=scoped)
    columns.extend((column, loader))
    return column


def _texts(row_widget) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [lbl.text() for lbl in row_widget.findChildren(QLabel)]


def test_the_movers_columns_are_scoped_and_sellers_is_not(app, conn, columns):
    gainers = _column(columns, conn)
    sellers = _column(columns, conn, "Top Sellers", scoped=False)

    assert gainers.scope() == "owned", "must open on the ledger view, as before"
    assert gainers._scope_box is not None
    assert sellers._scope_box is None, "sellers has no global scope to offer"


def test_choosing_global_announces_the_scope(app, conn, columns):
    """The view reloads on this signal rather than the column fetching for itself:
    global ranking runs on a worker thread."""
    column = _column(columns, conn)
    seen: list[str] = []
    column.scope_changed.connect(seen.append)

    column._scope_box.setCurrentIndex(1)
    assert seen == ["global"] and column.scope() == "global"

    column._scope_box.setCurrentIndex(0)
    assert seen == ["global", "owned"] and column.scope() == "owned"


def test_an_owned_row_is_clickable_and_emits_its_item(app, conn, columns):
    from PySide6.QtCore import Qt

    column = _column(columns, conn)
    fired: list[int] = []
    column.activated.connect(fired.append)

    column.set_movers([_owned(item_id=7)], gain=True)
    row = column._rows.itemAt(0).widget()
    _click(row)

    assert fired == [7]
    assert row.cursor().shape() == Qt.PointingHandCursor
    assert "Inventory" in row.toolTip()


def test_a_global_row_for_an_unheld_card_is_inert(app, conn, columns):
    """Decision 3. No emit from a real click, no hand cursor, and no tooltip promising
    Inventory — the old one said "Show … in Inventory" unconditionally, which a card
    you do not hold cannot honour."""
    from PySide6.QtCore import Qt

    column = _column(columns, conn)
    fired: list[int] = []
    column.activated.connect(fired.append)

    column.set_movers([_global()], gain=True)
    row = column._rows.itemAt(0).widget()
    _click(row)

    assert fired == [], "a card no lot holds navigated somewhere"
    assert row.cursor().shape() != Qt.PointingHandCursor
    assert "Inventory" not in row.toolTip()
    assert "not in your inventory" in row.toolTip()


def test_a_global_row_you_do_hold_stays_navigable_and_says_so(app, conn, columns):
    """Ownership is the marker; `item_id` is what makes it reachable. Both come from
    the same lookup, so this is the row that proves they are wired independently."""
    column = _column(columns, conn)
    fired: list[int] = []
    column.activated.connect(fired.append)

    column.set_movers([_global(item_id=11, owned_qty=3, qty=3)], gain=True)
    row = column._rows.itemAt(0).widget()
    _click(row)

    assert fired == [11]
    assert "Inventory" in row.toolTip()
    assert any("you hold 3" in text for text in _texts(row)), _texts(row)


def test_a_global_loser_does_not_claim_a_cost_basis(app, conn, columns):
    """An owned loser appends "paid $X" so the drop reads against basis. A card you
    never bought has no basis, and "paid $0.00" would read as a fact rather than an
    absence."""
    column = _column(columns, conn, "Top Losers")

    column.set_movers([_global(change_cents=-4_000, change_pct=-20.0)], gain=False)
    texts = _texts(column._rows.itemAt(0).widget())

    assert not any("paid" in text for text in texts), texts


def test_an_owned_loser_still_shows_what_was_paid(app, conn, columns):
    """The other half of the same rule: the existing behaviour must be untouched."""
    column = _column(columns, conn, "Top Losers")

    column.set_movers([_owned(change_cents=-5_000, change_pct=-11.0)], gain=False)
    texts = _texts(column._rows.itemAt(0).widget())

    assert any("paid" in text for text in texts), texts


def test_switching_to_global_clears_the_owned_rows_first(app, conn, columns):
    """Found by driving the real app, not by these tests.

    The ranking takes ~900ms against the real history, and nothing cleared the column
    in the meantime — so switching to global displayed the **owned** list under a
    "Global" heading, complete with "paid $X", which only an owned row has. Stale data
    labelled as something else is worse than an empty box.
    """
    column = _column(columns, conn)
    column.set_movers([_owned(item_id=7)], gain=True)
    assert column._items, "nothing to go stale in the first place"

    column.set_ranking(gain=True)

    # `_items` rather than the layout's child count: dropped rows go out through
    # `deleteLater`, so the layout still holds them until an event loop runs.
    assert column._items == [], "kept owned rows while ranking the catalog"
    assert "Ranking" in column._empty_text()


def test_arriving_rows_end_the_ranking_state(app, conn, columns):
    column = _column(columns, conn)
    column.set_ranking(gain=True)

    column.set_movers([_global()], gain=True)

    assert len(column._items) == 1
    assert "Ranking" not in column._empty_text()


def test_the_global_fetch_is_bounded(app, conn, columns):
    """The other half of the same bug. The view asked for `limit=None`, copying the
    owned path where 759 rows are free; globally that ranked 18,558 and then hydrated
    a catalog name for every one, so the panel never arrived. Ten pages is plenty."""
    from tcginventory.ui.views import dashboard

    assert dashboard._GLOBAL_LIMIT is not None, "unbounded global fetch"
    assert dashboard._GLOBAL_LIMIT >= dashboard._PAGE, "cannot fill even one page"
    assert dashboard._GLOBAL_LIMIT <= 200, "hydrating this many names is the stall"


def test_the_global_empty_state_names_the_sweep(app, conn, columns):
    """An empty global list means there is no catalog history, which is fixable.
    "No gainers in this range" would send the reader to look at the wrong thing."""
    column = _column(columns, conn)
    column._scope_box.setCurrentIndex(1)

    column.set_movers([], gain=True)

    assert "sweep" in column._empty_text()


def test_the_owned_empty_state_is_unchanged(app, conn, columns):
    column = _column(columns, conn)

    column.set_movers([], gain=True)

    assert column._empty_text() == "No gainers in this range."
