"""Offscreen smoke tests for the M3 read views over a seeded database."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tcginventory.data.db import connect  # noqa: E402
from tcginventory.data.migrator import migrate  # noqa: E402


def test_inventory_view_populates(app, seeded_conn):
    from tcginventory.ui.views.inventory import InventoryView

    view = InventoryView(seeded_conn)
    # Every seeded item that has a lot shows as a row.
    assert view._model.rowCount() >= 6
    view.deleteLater()


def test_inventory_trend_column_renders_arrows(app, seeded_conn):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor

    from tcginventory.services.trends import Trend
    from tcginventory.ui.models.inventory_model import TREND_COL, InventoryTableModel

    model = InventoryTableModel()
    from tcginventory.services.inventory import InventoryService

    model.set_rows(InventoryService(seeded_conn).inventory_rows())
    item_id = model.row_at(0).item.id
    # A strong up-trend → green up-arrows scaling with magnitude.
    model.set_trends(
        {
            item_id: Trend("up", 12.0, 40.0, 3, 30, 30),
        }
    )
    from tcginventory.ui import theme

    idx = model.index(0, TREND_COL)
    assert model.data(idx, Qt.DisplayRole) == "▲▲▲"
    assert model.data(idx, Qt.ForegroundRole).color() == QColor(theme.active().pos)
    assert model.data(idx, Qt.UserRole) == 40.0  # sorts by signed %
    # An item with no computed trend shows the neutral placeholder.
    other = model.index(1, TREND_COL)
    assert model.data(other, Qt.DisplayRole) == "·"


def test_inventory_mark_missing_from_view(app, seeded_conn):
    from tcginventory.ui.views.inventory import InventoryView

    view = InventoryView(seeded_conn)
    row = view._model.row_at(0)
    assert not row.missing

    view._toggle_missing(row)  # the right-click action handler
    updated = next(
        view._model.row_at(i)
        for i in range(view._model.rowCount())
        if view._model.row_at(i).lot_id == row.lot_id
    )
    assert updated.sold_status == "Missing"
    assert updated.missing is True

    view._toggle_missing(updated)  # reversible
    restored = next(
        view._model.row_at(i)
        for i in range(view._model.rowCount())
        if view._model.row_at(i).lot_id == row.lot_id
    )
    assert restored.missing is False
    view.deleteLater()


def test_name_tag_labels_stolen_sold_partial(app, svc, inv, conn):
    """Ticket 70: a stolen lot gets its own pill beside the card name, and it
    wins over the sold states. The sold/partial pills are unchanged.

    Rows are built here rather than taken from the demo seed, which carries no
    partly-sold lot."""
    from conftest import buy, sell
    from tcginventory.ui import theme
    from tcginventory.ui.views._inventory_delegates import name_tag

    keeper = buy(svc, "Keeper", 1000, 1200, "2026-01-01").lots[0]
    flip = buy(svc, "Flip", 1000, 1200, "2026-01-02").lots[0]
    sell(svc, flip.item_id, 1, 1500, "2026-02-01")
    part = buy(svc, "Part", 1000, 1200, "2026-01-03", quantity=2).lots[0]
    sell(svc, part.item_id, 1, 1500, "2026-02-02")
    conn.commit()

    def row_for(lot_id):
        return next(r for r in inv.inventory_rows() if r.lot_id == lot_id)

    t = theme.active()

    assert row_for(keeper.id).sold_status == "unsold"
    assert name_tag(row_for(keeper.id)) is None  # nothing to say about it

    assert name_tag(row_for(flip.id))[0] == "Sold"
    assert name_tag(row_for(flip.id))[1] == t.ink2

    assert name_tag(row_for(part.id))[0] == "Partial"
    assert name_tag(row_for(part.id))[1] == t.warn

    # Mark the untouched lot stolen → the Stolen pill, in the loss colour.
    inv.set_lot_missing(keeper.id, True)
    conn.commit()
    label, fg, bg = name_tag(row_for(keeper.id))
    assert label == "Stolen"
    assert fg == t.neg
    assert bg.alpha() < 255  # a soft fill, like the other pills

    # A part-sold lot that is also stolen shows Stolen, not Partial.
    inv.set_lot_missing(part.id, True)
    conn.commit()
    assert row_for(part.id).sold_status == "Missing"
    assert name_tag(row_for(part.id))[0] == "Stolen"

    assert name_tag(None) is None  # an empty cell paints no pill


def test_inventory_stolen_filter_segment(app, seeded_conn):
    """Ticket 70: the Sold filter's Stolen segment shows exactly the stolen
    lots, and selecting it drops the contradictory In-stock chip."""
    from tcginventory.services.inventory import InventoryService
    from tcginventory.ui.views.inventory import InventoryView

    view = InventoryView(seeded_conn)
    stolen_lot = view._model.row_at(0).lot_id
    InventoryService(seeded_conn).set_lot_missing(stolen_lot, True)
    seeded_conn.commit()
    view.refresh()

    # "In stock" on top of "Stolen" would be an empty grid — the chip gives way.
    view._in_stock.setChecked(True)
    view._sold.select("Missing")
    assert not view._in_stock.isChecked()

    visible = [
        view._model.row_at(view._proxy.mapToSource(view._proxy.index(i, 0)).row())
        for i in range(view._proxy.rowCount())
    ]
    assert [r.lot_id for r in visible] == [stolen_lot]
    assert all(r.missing for r in visible)

    # Back to All → the stolen row is still there, beside everything else.
    view._sold.select("All")
    assert view._proxy.rowCount() == view._model.rowCount()
    view.deleteLater()


def test_inventory_ebay_link_auto_populated(app, seeded_conn):
    from tcginventory.services.inventory import InventoryService
    from tcginventory.ui.models.inventory_model import (
        EBAY_COL,
        LINK_URL_ROLE,
        InventoryTableModel,
    )

    model = InventoryTableModel()
    model.set_rows(InventoryService(seeded_conn).inventory_rows())
    # Every row's eBay link resolves (derived sold-search when none is stored).
    url = model.data(model.index(0, EBAY_COL), LINK_URL_ROLE)
    assert url and "ebay.com/sch" in url and "LH_Sold=1" in url


def test_inventory_ignores_a_retired_prefetch_workers_results(app, seeded_conn):
    """A retired prefetch worker now survives to finish, so its results have to
    be ignored explicitly.

    It breaks out of its loop at the next row and emits `resolved` regardless, so
    what it carries is partial and describes a row set that has been filtered
    away. Before `retire()` it was dropped mid-run — the crash hid this — so
    without an identity guard the stale maps silently overwrite the live
    worker's, and the grid shows the wrong art and trend.
    """
    from types import SimpleNamespace

    from tcginventory.ui.views.inventory import InventoryView

    view = InventoryView(seeded_conn)
    live = SimpleNamespace(images={"live": 1}, history={}, trends={})
    view._prefetch_worker = live
    view._row_image = {"live": 1}

    stale = SimpleNamespace(images={"stale": 2}, history={"s": 1}, trends={"s": 1})
    view._on_previews_resolved(stale)  # a worker that is no longer the current one
    assert view._row_image == {"live": 1}  # not overwritten

    view._on_previews_resolved(live)  # the current one still publishes
    assert view._row_image == {"live": 1}
    view.deleteLater()


def test_retired_worker_outlives_being_replaced(app):
    """A worker that has been asked to stop but is still running must stay
    referenced until it actually exits.

    Freeing it early destroys the C++ QThread underneath a live thread, and Qt
    answers that with a hard abort (0xC0000409) rather than an exception — which
    is how startup died once Inventory and History each refreshed twice fast
    enough to replace their own still-running worker.
    """
    import gc

    from PySide6.QtCore import QThread

    from tcginventory.ui.views._worker import retire

    class _Spinner(QThread):
        def run(self) -> None:
            while not self.isInterruptionRequested():
                self.msleep(5)

    parked: list = []
    worker = _Spinner()
    worker.start()
    while not worker.isRunning():
        pass

    retire(worker, parked)
    assert parked == [worker]  # held, not dropped

    del worker  # the caller's reference goes away (self._worker = new one)
    gc.collect()  # …this is the collection that used to kill the process
    assert len(parked) == 1

    parked[0].wait(5000)
    for _ in range(50):  # let finished() land, however it's delivered
        app.processEvents()
        if not parked:
            break
    assert parked == []  # released once it really stopped


def test_history_view_populates_and_details(app, seeded_conn):
    from tcginventory.ui.views.history import HistoryView

    view = HistoryView(seeded_conn)
    view.resize(900, 600)
    view.show()  # force a render so the pill delegate paints
    app.processEvents()
    assert view._model.rowCount() >= 6
    # Selecting a row populates the bottom item table (SKU, name, price, …).
    view._table.selectRow(0)
    app.processEvents()
    assert view._detail_table.rowCount() > 0
    assert view._detail_table.columnCount() == 8
    assert "Select a transaction" not in view._detail_header.text()  # filled in
    view.close()
    view.deleteLater()


def test_history_preview_gallery_follows_selection_then_hover(
    app, monkeypatch, tmp_path
):
    """Selecting a transaction fills the bottom-right region with every card in
    it; hovering a detail row focuses that one card, and leaving reverts — to the
    pinned row if one is selected, else to the full gallery."""
    from PySide6.QtGui import QPixmap

    from conftest import incoming
    from tcginventory.services import cardimages
    from tcginventory.services.transactions import (
        TransactionRequest,
        TransactionService,
    )
    from tcginventory.ui.views import history as history_mod

    # A real file on disk, so the panes paint from cache and no fetch thread runs.
    png = tmp_path / "art.png"
    QPixmap(4, 6).save(str(png), "PNG")
    monkeypatch.setattr(cardimages, "cache_path", lambda card_id: png)
    monkeypatch.setattr(cardimages, "is_cached", lambda card_id: True)
    art = {
        "Charizard ex": ("sv3-125", "https://img.example/1.png"),
        "Blastoise": ("base1-2", "https://img.example/2.png"),
    }
    monkeypatch.setattr(
        cardimages,
        "art_for",
        lambda conn, name, set_name=None, number=None: art.get(name),
    )
    history_mod._ART_MEMO.clear()

    conn = connect(":memory:")
    migrate(conn)
    TransactionService(conn).post(
        TransactionRequest(
            occurred_at="2026-01-01",
            cash_out_cents=3000,
            incoming=[incoming("Charizard ex", 2000), incoming("Blastoise", 1000)],
        )
    )

    view = history_mod.HistoryView(conn)
    view._table.selectRow(0)
    app.processEvents()

    assert view._detail_table.rowCount() == 2  # one lot per card
    assert view._gallery._gallery.count() == 2  # both cards in the gallery
    assert view._gallery._stack.currentIndex() == 1  # …showing all of them

    view._on_detail_hover(0)  # pointer over the first lot
    assert view._gallery._stack.currentIndex() == 0  # focused on that one card
    view._on_detail_hover(None)  # pointer leaves the table
    assert view._gallery._stack.currentIndex() == 1  # back to the full gallery

    view._detail_table.selectRow(1)  # pin a row: it outlives the hover
    view._on_detail_hover(0)
    view._on_detail_hover(None)
    assert view._gallery._stack.currentIndex() == 0

    # Selecting a different transaction rebuilds the gallery from scratch.
    view._table.clearSelection()
    app.processEvents()
    assert view._gallery._stack.currentIndex() == 1
    assert not view._detail_art

    view.deleteLater()
    conn.close()


def test_history_search_matches_card_or_counterparty(app):
    from conftest import incoming
    from tcginventory.services.transactions import (
        TransactionRequest,
        TransactionService,
    )
    from tcginventory.ui.views.history import HistoryView

    conn = connect(":memory:")
    migrate(conn)
    svc = TransactionService(conn)
    svc.post(
        TransactionRequest(
            occurred_at="2026-01-01",
            cash_out_cents=1000,
            incoming=[incoming("Charizard ex", 1000)],
            counterparty="eBay",
        )
    )
    svc.post(
        TransactionRequest(
            occurred_at="2026-01-02",
            cash_out_cents=500,
            incoming=[incoming("Blastoise", 500)],
            counterparty="Card Cavern",
        )
    )

    view = HistoryView(conn)
    assert view._proxy.rowCount() == 2
    view._search.setText("chari")  # matches a card name
    assert view._proxy.rowCount() == 1
    view._search.setText("cavern")  # matches a counterparty
    assert view._proxy.rowCount() == 1
    view._search.setText("zzzz")  # matches nothing → the "no match" empty state
    assert view._proxy.rowCount() == 0
    assert view._stack.currentWidget() is view._no_match
    view._search.clear()
    assert view._proxy.rowCount() == 2
    assert view._stack.currentWidget() is view._table
    view.deleteLater()
    conn.close()


def test_dashboard_builds_charts(app, seeded_conn):
    from tcginventory.ui.views.dashboard import DashboardView

    view = DashboardView(seeded_conn)
    chart = view._chart_view.chart()
    assert chart is not None
    from PySide6.QtCharts import QLineSeries

    lines = [s for s in chart.series() if isinstance(s, QLineSeries)]
    # money-movement + realized-profit + collection-value (the collection line
    # now shows for any holdings, valued at current/reviewed prices).
    assert len(lines) == 3
    view.deleteLater()


def test_row_editor_dialog_opens(app, seeded_conn):
    from tcginventory.data.repositories import ItemRepository
    from tcginventory.ui.views.item_editor import ItemEditorDialog

    item = ItemRepository(seeded_conn).list_all()[0]
    dialog = ItemEditorDialog(seeded_conn, item.id)
    assert dialog.windowTitle().endswith("edit")
    dialog.deleteLater()


def test_app_icon_loads(app):
    from tcginventory.ui.assets import app_icon

    assert not app_icon().isNull()


def test_main_window_hosts_views(app, seeded_conn):
    from tcginventory.ui.main_window import NAV_ITEMS, MainWindow

    window = MainWindow(seeded_conn)
    assert window.windowTitle().startswith("TCGinventory")
    assert not window.windowIcon().isNull()
    # Visit each nav destination; refresh must not raise.
    for row in range(len(NAV_ITEMS)):
        window._on_nav_changed(row)
        app.processEvents()
    window.close()
    window.deleteLater()


def test_history_recognition_tab_summarizes_the_corpus(app, seeded_conn):
    """The Recognition History toggle shows one row per capture with corpus
    counts, KPI totals, and the growth sparkline."""
    from tcginventory.data.vision import CardLabel, VisionRepository, open_vision
    from tcginventory.ui.views.history import HistoryView

    vrepo = VisionRepository(open_vision(":memory:"))
    cap = vrepo.add_capture(source="transaction", now="2026-01-01T09:00:00+00:00")
    img = vrepo.add_image(
        cap, "s", "/a.png", width=100, height=60, now="2026-01-01T09:00:00+00:00"
    )
    vrepo.add_region(img, 1, (0, 0, 0.5, 1), None, CardLabel("cel-4", name="Zard"))
    vrepo.add_region(img, 2, (0.5, 0, 0.5, 1), None, CardLabel())  # drawn, unlabeled

    view = HistoryView(seeded_conn, vision_repo=vrepo)
    assert view._pages.currentIndex() == 0  # defaults to Transaction History

    view._set_mode(1)  # → Recognition History (refreshes from the corpus)
    assert view._pages.currentIndex() == 1
    assert view._rec._model.rowCount() == 1
    row = view._rec._model.row_at(0)
    assert (row.images, row.regions, row.labeled) == (1, 2, 1)
    assert row.state == "partial"  # one of two boxes labeled
    assert view._rec._stat_images.text() == "1"
    assert view._rec._stat_labeled.text() == "1"
    view.deleteLater()


def test_recognition_preview_and_filmstrip(app, seeded_conn):
    """Selecting a capture loads its images into the preview; the filmstrip shows
    only when the capture has more than one image."""
    from tcginventory.data.vision import CardLabel, VisionRepository, open_vision
    from tcginventory.ui.views.history import HistoryView

    vrepo = VisionRepository(open_vision(":memory:"))
    c1 = vrepo.add_capture(source="listing", now="2026-01-01T00:00:00+00:00")
    i1 = vrepo.add_image(c1, "a", "/a.png", now="2026-01-01T00:00:00+00:00")
    vrepo.add_region(i1, 1, (0, 0, 1, 1), None, CardLabel(name="One"))
    c2 = vrepo.add_capture(source="listing", now="2026-02-01T00:00:00+00:00")  # newer
    vrepo.add_image(c2, "b", "/b.png", now="2026-02-01T00:00:00+00:00")
    vrepo.add_image(c2, "c", "/c.png", now="2026-02-01T00:00:05+00:00")

    view = HistoryView(seeded_conn, vision_repo=vrepo)
    view._set_mode(1)
    rec = view._rec

    rec._table.selectRow(0)  # newest-first → the two-image capture
    assert len(rec._cap_images) == 2
    assert not rec._filmstrip.isHidden()  # filmstrip shown for multi-image
    rec._table.selectRow(1)  # the single-image capture
    assert len(rec._cap_images) == 1
    assert rec._filmstrip.isHidden()  # hidden for a lone image
    view.deleteLater()


def test_thumbcache_decodes_each_thumbnail_once(app, tmp_path):
    """A thumbnail is decoded+scaled once per session; a not-yet-written file is
    remembered as a miss until a worker reports new files on disk."""
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor, QPixmap

    from tcginventory.ui import thumbcache

    src = QPixmap(48, 64)
    src.fill(QColor("#ff8800"))
    path = tmp_path / "t.png"
    src.save(str(path))

    thumbcache.forget_misses()
    first = thumbcache.scaled("test", "t", path, 24, 32)
    second = thumbcache.scaled("test", "t", path, 24, 32)
    assert first is not None and first.size() == QSize(24, 32)
    assert second is not None and second.cacheKey() == first.cacheKey()  # reused

    missing = tmp_path / "later.png"
    assert thumbcache.scaled("test", "later", missing, 24, 32) is None
    src.save(str(missing))  # a crop/download lands after the first paint
    assert thumbcache.scaled("test", "later", missing, 24, 32) is None  # miss cached
    thumbcache.forget_misses()
    assert thumbcache.scaled("test", "later", missing, 24, 32) is not None


def test_history_thumbnails_persist_across_refreshes(app, monkeypatch):
    """Resolved card art is memoised for the session: a rebuild paints the same
    thumbnails straight away instead of re-resolving them from the catalog."""
    from conftest import incoming
    from tcginventory.services import cardimages
    from tcginventory.services.transactions import (
        TransactionRequest,
        TransactionService,
    )
    from tcginventory.ui.views import history as history_mod

    conn = connect(":memory:")
    migrate(conn)
    TransactionService(conn).post(
        TransactionRequest(
            occurred_at="2026-01-01",
            cash_out_cents=1000,
            incoming=[incoming("Charizard ex", 1000)],
            counterparty="eBay",
        )
    )

    history_mod._ART_MEMO.clear()
    lookups: list[str] = []
    monkeypatch.setattr(cardimages, "is_cached", lambda card_id: True)
    monkeypatch.setattr(
        cardimages,
        "art_for",
        lambda conn, name, set_name=None, number=None: (
            lookups.append(name),
            ("cel-4", "https://img.example/cel-4.png"),
        )[1],
    )

    view = history_mod.HistoryView(conn)  # in-memory DB → resolves inline
    assert view._model.row_at(0).thumb_ids == ["cel-4"]
    assert len(lookups) == 1

    view.refresh()  # the ledger changed → rows rebuilt
    assert view._model.row_at(0).thumb_ids == ["cel-4"]  # no blank flash
    assert len(lookups) == 1  # and no second art lookup

    view.deleteLater()
    conn.close()


def test_history_mode_toggle_reuses_loaded_pages(app, seeded_conn, monkeypatch):
    """Toggling Transaction/Recognition re-shows an already-loaded page as-is; only
    a data change (refresh) rebuilds it."""
    from tcginventory.ui.views.history import HistoryView

    view = HistoryView(seeded_conn)  # __init__ loads the Transaction page
    txn_loads: list[int] = []
    rec_loads: list[int] = []
    monkeypatch.setattr(view, "_refresh_txn", lambda: txn_loads.append(1))
    monkeypatch.setattr(view._rec, "refresh", lambda: rec_loads.append(1))

    view._set_mode(1)  # first visit to Recognition → loads once
    assert (txn_loads, rec_loads) == ([], [1])
    view._set_mode(0)  # both pages now loaded → toggling is free
    view._set_mode(1)
    assert (txn_loads, rec_loads) == ([], [1])

    view.refresh()  # data changed → the visible page reloads
    assert (txn_loads, rec_loads) == ([], [1, 1])
    view._set_mode(0)  # …and the other one on its next visit
    assert txn_loads == [1]

    view.deleteLater()


def test_nav_refresh_is_gated_until_data_changes(app, seeded_conn):
    """Switching to an unchanged screen must not re-run its refresh; a data
    mutation invalidates the views so the next visit refreshes once.

    Startup pre-loads every view, so even the *first* visit is free — that's
    what keeps navigation from stalling on a screen nobody has opened yet.
    """
    from tcginventory.ui.main_window import NAV_ITEMS, MainWindow

    window = MainWindow(seeded_conn)
    inv_index = NAV_ITEMS.index("Inventory")
    view = window._stack.widget(inv_index)
    calls = {"n": 0}
    original = view.refresh

    def counting() -> None:
        calls["n"] += 1
        original()

    view.refresh = counting

    window._on_nav_changed(inv_index)  # pre-loaded at startup → nothing to do
    assert calls["n"] == 0
    window._on_nav_changed(0)  # away…
    window._on_nav_changed(inv_index)  # …and back, nothing changed → cached
    assert calls["n"] == 0

    window._on_nav_changed(0)  # sit on another view
    window._on_ledger_changed()  # a mutation invalidates all views
    window._on_nav_changed(inv_index)  # revisit → refreshes exactly once
    assert calls["n"] == 1

    window.close()
    window.deleteLater()


def _visible_rows(view):
    return [
        view._model.row_at(view._proxy.mapToSource(view._proxy.index(i, 0)).row())
        for i in range(view._proxy.rowCount())
    ]


def test_inventory_search_matches_every_token_across_fields(app, seeded_conn):
    """Ticket 76: "charizard 004/102" finds the row whose name and number live in
    different fields — one substring never could, since the set name sits
    between them in the haystack. A token absent from every row empties the grid."""
    from tcginventory.ui.views.inventory import InventoryView

    view = InventoryView(seeded_conn)
    target = next(
        view._model.row_at(i)
        for i in range(view._model.rowCount())
        if view._model.row_at(i).item.card_number
    )
    first_word = target.item.name.split()[0].lower()
    view._search.setText(f"{first_word} {target.item.card_number}")
    visible = _visible_rows(view)
    assert target.lot_id in [r.lot_id for r in visible]
    for row in visible:
        haystack = f"{row.item.name} {row.item.set_name} {row.item.card_number}".lower()
        assert first_word in haystack and target.item.card_number in haystack

    view._search.setText(f"{first_word} zzzz")
    assert view._proxy.rowCount() == 0
    view._search.clear()
    assert view._proxy.rowCount() == view._model.rowCount()
    view.deleteLater()


def test_inventory_voice_query_lands_in_the_search_field(app, seeded_conn, monkeypatch):
    """Ticket 76: the mic's transcription path — worker thread, snap against the
    grid's own names and sets, ``recognized`` → search field — with Whisper
    replaced by a canned transcript so no microphone or model is involved."""
    from tcginventory.services import voice
    from tcginventory.ui.views.inventory import InventoryView

    view = InventoryView(seeded_conn)
    target = next(
        view._model.row_at(i)
        for i in range(view._model.rowCount())
        if view._model.row_at(i).item.card_number
    )
    number = target.item.card_number  # e.g. "004/102"
    spoken_number = f"{int(number.split('/')[0])} out of {number.split('/')[1]}"
    # Misspell the last letter: the snap step, fed the grid's names, repairs it.
    heard = f"Um, {target.item.name[:-1]}o {spoken_number}, please."
    monkeypatch.setattr(voice, "transcribe", lambda pcm, rate=16000: heard)

    view._mic.transcribe(b"\x00\x10" * 16000)
    worker = view._mic._worker
    assert worker is not None and worker.wait(10_000)
    for _ in range(20):  # deliver the queued cross-thread signal
        app.processEvents()
        if view._search.text():
            break
    expected_number = f"{int(number.split('/')[0])}/{number.split('/')[1]}"
    assert view._search.text() == f"{target.item.name.lower()} {expected_number}"
    # The grid filtered on it: "004" is a substring of "004/102", so the leading
    # zeros the speaker never says do not matter.
    assert view._proxy.rowCount() >= 1
    assert all(r.item.name == target.item.name for r in _visible_rows(view))
    view.deleteLater()


def test_voice_button_hears_speech_above_the_floor_and_not_silence(app):
    import numpy as np

    from tcginventory.ui.views._voice_input import SPEECH_RMS, _rms

    assert _rms(b"") == 0.0
    assert _rms(b"\x00" * 3200) == 0.0
    loud = np.full(1600, 8000, dtype="<i2").tobytes()
    assert _rms(loud) > SPEECH_RMS


def test_preview_prefetch_isolates_a_failing_row(app, seeded_conn, monkeypatch):
    """One row that throws must not cost every later row its preview.

    The warming loop's only handler used to sit outside it, so a single bad row
    abandoned the rest and the grid drew "No preview" for cards whose art was
    already on disk (ticket 67).
    """
    from types import SimpleNamespace

    from tcginventory.services import cardimages
    from tcginventory.ui.views.inventory import _PreviewPrefetchWorker

    rows = [
        (1, 101, "Fine One", "151", "199/165"),
        (2, 102, "Bad One", "151", "176/165"),
        (3, 103, "Also Fine", "Scarlet & Violet", "211/198"),
    ]

    def fake_art_for(conn, name, set_name, number):
        if name == "Bad One":
            raise RuntimeError("unreadable art")
        return (f"card-{name}", "https://example/art.png")

    monkeypatch.setattr(cardimages, "art_for", fake_art_for)
    monkeypatch.setattr(cardimages, "ensure_cached", lambda *a, **k: None)

    worker = _PreviewPrefetchWorker(":memory:", rows, "2026-01-01")
    snaps = SimpleNamespace(history_for_item=lambda item_id, since: [])
    trend_svc = SimpleNamespace(trend_for=lambda item, as_of: None)

    images, _history, _trends = worker.warm_rows(
        seeded_conn, snaps, trend_svc, "2026-09-03"
    )

    # The row after the failure still got its art — that is the regression.
    assert 3 in images, "a later row lost its preview to an earlier failure"
    assert 1 in images
    assert 2 not in images  # the failing row alone goes without
