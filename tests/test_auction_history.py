"""Auction History — the History window's third page (offscreen).

Covers the two halves the feature is actually made of: a delete that stays
restorable, and the export→post chain that turns a watched auction into a
recorded win. The repository-level guarantees (snapshot fidelity, the one-shot
restore) live in ``test_auctions.py``; this file drives them through the UI.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QDialog, QMessageBox  # noqa: E402

from tcginventory.data.auctions import (  # noqa: E402
    EVENT_ARCHIVED,
    EVENT_DELETED,
    EVENT_UNARCHIVED,
    EVENT_WON,
    Auction,
    AuctionCard,
    AuctionEventRepository,
    AuctionRepository,
)
from tcginventory.data.db import connect  # noqa: E402
from tcginventory.data.migrator import migrate  # noqa: E402
from tcginventory.domain.enums import Category, Intent  # noqa: E402
from tcginventory.domain.models import Item  # noqa: E402
from tcginventory.services.transactions import IncomingCard  # noqa: E402
from tcginventory.ui.main_window import MainWindow  # noqa: E402
from tcginventory.ui.views.history import HistoryView  # noqa: E402

_AUCTION_PAGE = 2  # 0 Transaction, 1 Recognition, 2 Auction


@pytest.fixture(autouse=True)
def _silence_dialogs(monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    # The post-success confirmation is a custom QDialog — don't block on exec().
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.Accepted)
    # Auction deletes go through their own confirm dialog, not QMessageBox.
    monkeypatch.setattr(
        "tcginventory.ui.views.auctions._confirm_delete", lambda *a, **k: True
    )


@pytest.fixture()
def win(app):
    conn = connect(":memory:")
    migrate(conn)
    window = MainWindow(conn)
    yield window
    window.deleteLater()


def _watch(window, title="Vintage lot", **kw) -> Auction:
    """Put a saved auction on the watchlist and hand it back."""
    auction = Auction(
        title=title,
        cards=kw.pop(
            "cards",
            [AuctionCard(name="Charizard", quantity=2, market_value_cents=25_000)],
        ),
        **kw,
    )
    AuctionRepository(window._auctions_conn).save(auction)
    window._auctions.refresh()
    return auction


def _events(window) -> list:
    return AuctionEventRepository(window._auctions_conn).list_all()


def _auction_pane(window):
    """The Auction History page, refreshed as a visit to it would."""
    window._history._set_mode(_AUCTION_PAGE)
    return window._history._auc


def _select_row(pane, proxy_row: int = 0) -> None:
    pane._table.selectRow(proxy_row)


# ---------------------------------------------------------------------------
# the pane
# ---------------------------------------------------------------------------


def test_history_has_a_third_page(win):
    assert win._history._pages.count() == 3
    assert win._history._mode_auc.text() == "Auction History"
    win._history._set_mode(_AUCTION_PAGE)
    assert win._history._pages.currentIndex() == _AUCTION_PAGE
    assert win._history._mode_auc.isChecked()
    assert not win._history._mode_txn.isChecked()


def test_deleting_an_auction_logs_a_restorable_row(win):
    auction = _watch(win, "Vintage lot")
    win._auctions._delete(auction)

    pane = _auction_pane(win)
    assert pane._model.rowCount() == 1
    row = pane._model.row_at(0)
    assert row.event.kind == EVENT_DELETED
    assert row.state == "restorable"
    # The row reads correctly even though the auction it names is gone.
    assert pane._model.data(pane._model.index(0, 2)) == "Vintage lot"
    assert pane._model.data(pane._model.index(0, 4)) == "$500.00"  # 2 x $250
    assert pane._stat_restorable.text() == "1"


def test_restore_puts_the_auction_back_on_the_watchlist(win):
    auction = _watch(win, "Vintage lot")
    win._auctions._delete(auction)
    repo = AuctionRepository(win._auctions_conn)
    assert repo.list_all(archived=None) == []

    pane = _auction_pane(win)
    _select_row(pane)
    assert pane._restore_btn.isEnabled()
    pane._restore_selected()

    (back,) = repo.list_all(archived=None)
    assert back.title == "Vintage lot"
    assert [(c.name, c.quantity) for c in back.cards] == [("Charizard", 2)]
    # The event survives, marked consumed — Auction History never loses a row.
    (event,) = _events(win)
    assert event.restored_id == back.id
    assert not event.restorable
    assert pane._model.row_at(0).state == "restored"
    assert not pane._restore_btn.isEnabled()


def test_restore_is_offered_only_on_a_restorable_row(win):
    auction = _watch(win, "Kept")
    win._auctions._toggle_archived(auction)  # an archive event: nothing destroyed

    pane = _auction_pane(win)
    _select_row(pane)
    assert pane._model.row_at(0).event.kind == EVENT_ARCHIVED
    assert pane._model.row_at(0).state == "logged"
    assert not pane._restore_btn.isEnabled()

    # …and clicking it anyway (a stale enable) is a no-op, not a crash.
    pane._restore_selected()
    assert len(_events(win)) == 1


def test_archive_and_unarchive_both_log(win):
    auction = _watch(win, "Later")
    win._auctions._toggle_archived(auction)
    auction.archived = True
    win._auctions._toggle_archived(auction)

    pane = _auction_pane(win)
    kinds = [pane._model.row_at(i).event.kind for i in range(pane._model.rowCount())]
    assert sorted(kinds) == sorted([EVENT_ARCHIVED, EVENT_UNARCHIVED])


def test_detail_pane_previews_what_a_restore_would_bring_back(win):
    auction = _watch(
        win,
        "Bundle",
        cards=[
            AuctionCard(name="Charizard", set_name="Base", market_value_cents=25_000),
            AuctionCard(name="Pikachu", set_name="Jungle", market_value_cents=500),
        ],
    )
    win._auctions._delete(auction)

    pane = _auction_pane(win)
    _select_row(pane)
    assert pane._detail_table.rowCount() == 2
    assert pane._detail_table.item(0, 0).text() == "Charizard"
    assert pane._detail_table.item(0, 1).text() == "Base"
    assert pane._detail_table.item(0, 3).text() == "$250.00"


def test_kind_filter_and_search_narrow_the_log(win):
    doomed = _watch(win, "Doomed lot")
    kept = _watch(win, "Kept lot")
    win._auctions._delete(doomed)
    win._auctions._toggle_archived(kept)

    pane = _auction_pane(win)
    assert pane._proxy.rowCount() == 2

    pane._kind.setCurrentText(EVENT_DELETED)
    assert pane._proxy.rowCount() == 1
    assert pane._model.row_at(pane._proxy.mapToSource(pane._proxy.index(0, 0)).row())

    pane._clear_filters()
    pane._search.setText("kept")
    assert pane._proxy.rowCount() == 1


def test_empty_pane_without_an_auctions_connection(app):
    """A bare HistoryView must not reach for the user's real auctions.db —
    five existing test files construct one, and the suite would write to it."""
    conn = connect(":memory:")
    migrate(conn)
    view = HistoryView(conn)
    view._set_mode(_AUCTION_PAGE)

    assert view._auc._events is None
    assert view._auc._model.rowCount() == 0
    assert view._auc._stack.currentIndex() == 1  # the empty state
    assert not view._auc._restore_btn.isEnabled()
    view.deleteLater()


# ---------------------------------------------------------------------------
# the won chain: export → post
# ---------------------------------------------------------------------------


def _post_purchase(win, cents: int = 20_000) -> None:
    """Enter a winning bid on the prefilled Transaction form and post it."""
    win._transaction._cash_out.setValue(cents / 100)
    win._transaction.submit()


def test_export_then_post_records_exactly_one_won_event(win):
    auction = _watch(win, "Vintage lot")
    win._auctions._export_auction(auction)
    assert _events(win) == []  # nothing yet — the export alone wins nothing

    _post_purchase(win)

    (event,) = _events(win)
    assert event.kind == EVENT_WON
    assert event.auction_id == auction.id
    assert event.title == "Vintage lot"
    assert event.transaction_id is not None
    # One user action, one row: won implies archived, it does not log both.
    assert AuctionRepository(win._auctions_conn).get(auction.id).archived
    assert win._auctions._repo.list_all(archived=False) == []

    pane = _auction_pane(win)
    assert pane._model.row_at(0).result == f"transaction {event.transaction_id}"
    assert pane._stat_won.text() == "1"


def test_abandoned_export_records_nothing(win):
    auction = _watch(win, "Lost it")
    win._auctions._export_auction(auction)
    win._transaction.clear()  # the user walked away

    assert _events(win) == []
    assert not AuctionRepository(win._auctions_conn).get(auction.id).archived


def test_the_auction_tag_does_not_leak_into_the_next_transaction(win):
    auction = _watch(win, "Vintage lot")
    win._auctions._export_auction(auction)
    _post_purchase(win)
    assert len(_events(win)) == 1

    # A second, unrelated purchase typed by hand must not be a second win.
    win._transaction.add_incoming(
        IncomingCard(
            Item(name="Snorlax", category=Category.SINGLE), 1, 1_000, Intent.RESALE
        )
    )
    _post_purchase(win, 1_000)
    assert len(_events(win)) == 1


def test_editing_a_transaction_never_wins_an_auction(win):
    auction = _watch(win, "Vintage lot")
    win._auctions._export_auction(auction)
    _post_purchase(win)
    assert len(_events(win)) == 1
    txn_id = _events(win)[0].transaction_id

    # An edit voids and re-posts that same transaction. Nothing is won twice.
    assert win._transaction.load_transaction(txn_id)
    win._transaction._source_auction_id = auction.id  # even if the tag were set
    win._transaction.submit()
    assert len(_events(win)) == 1


def test_an_unsaved_editor_bundle_carries_no_auction_id(win):
    win._auctions._add_card_row().set_card(
        AuctionCard(name="Charizard", quantity=1, market_value_cents=25_000)
    )
    win._auctions._title.setText("Not saved yet")
    win._auctions._export_current()

    _post_purchase(win)
    assert _events(win) == []  # no watchlist entry exists to have been won


def test_won_survives_the_auction_being_deleted_first(win):
    """The auction can go between the export and the post — the post must still
    succeed, and simply record nothing."""
    auction = _watch(win, "Vintage lot")
    win._auctions._export_auction(auction)
    win._auctions._delete(auction)

    _post_purchase(win)

    kinds = [e.kind for e in _events(win)]
    assert kinds == [EVENT_DELETED]  # the delete, and no phantom win


def test_restoring_from_history_refreshes_the_watchlist(win):
    """A restore is a data change, so every other screen must go stale."""
    auction = _watch(win, "Vintage lot")
    win._auctions._delete(auction)
    before = win._data_version

    pane = _auction_pane(win)
    _select_row(pane)
    pane._restore_selected()

    assert win._data_version > before
    win._nav.setCurrentRow(6)  # eBay Auctions
    assert len(win._auctions._repo.list_all(archived=None)) == 1


def test_pane_money_uses_the_shared_formatter(win):
    auction = _watch(
        win,
        "Odd amount",
        cards=[AuctionCard(name="X", quantity=3, market_value_cents=1_233)],
    )
    win._auctions._delete(auction)
    pane = _auction_pane(win)
    # 3 x $12.33 = $36.99 — integer cents all the way to the string.
    assert pane._model.data(pane._model.index(0, 4)) == "$36.99"
    assert pane._model.data(pane._model.index(0, 3)) == "3"
    assert pane._model.data(pane._model.index(0, 3), Qt.UserRole) == 3


def test_date_is_shown_to_the_minute_but_sorted_on_the_raw_stamp(win):
    auction = _watch(win, "Vintage lot")
    win._auctions._delete(auction)
    pane = _auction_pane(win)

    shown = pane._model.data(pane._model.index(0, 0))
    raw = pane._model.row_at(0).event.occurred_at
    assert shown == raw[:16].replace("T", " ")
    assert "T" not in shown and "+00:00" not in shown
    # Trimming is display-only — the sort key stays the full ISO stamp, so two
    # events in the same minute still order correctly.
    assert pane._model.data(pane._model.index(0, 0), Qt.UserRole) == raw
