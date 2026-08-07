"""Tests for the 'Mark for Review' flag: poll protection + manual current value."""

from __future__ import annotations

from tcginventory.data.repositories import ItemRepository, PriceSnapshotRepository
from tcginventory.domain.enums import Category, Intent
from tcginventory.domain.models import Item
from tcginventory.services.inventory import InventoryService
from tcginventory.services.pricing import PricingService, TcgcsvProvider
from tcginventory.services.transactions import (
    IncomingCard,
    TransactionRequest,
    TransactionService,
)

_URL = "https://www.tcgplayer.com/product/250303/pokemon-celebrations-pikachu"


def _fake_fetch(url: str):
    if url.endswith("/groups"):
        return {
            "results": [
                {"groupId": 3170, "name": "Celebrations", "abbreviation": "CEL"}
            ]
        }
    if url.endswith("/3170/prices"):
        return {"results": [{"productId": 250303, "marketPrice": 9.99}]}
    raise AssertionError(url)


def _post_pikachu(conn, review: bool):
    svc = TransactionService(conn)
    svc.post(
        TransactionRequest(
            occurred_at="2026-01-01",
            cash_out_cents=3000,
            incoming=[
                IncomingCard(
                    item=Item(
                        name="Pikachu",
                        category=Category.SINGLE,
                        set_code="CEL",
                        set_name="Celebrations",
                        card_number="005/025",
                        tcgplayer_url=_URL,
                    ),
                    quantity=1,
                    market_value_cents=3000,
                    intent=Intent.RESALE,
                    review=review,
                )
            ],
        )
    )
    return ItemRepository(conn).list_all()[0]


def test_review_flag_set_on_post(conn):
    assert _post_pikachu(conn, review=True).review is True
    # (fresh conn per test via the fixture, so no cross-talk)


def test_review_item_gets_explanatory_note(conn):
    from tcginventory.services.transactions import REVIEW_NOTE

    item = _post_pikachu(conn, review=True)
    assert ItemRepository(conn).get(item.id).notes == REVIEW_NOTE


def test_non_review_item_has_no_forced_note(conn):
    item = _post_pikachu(conn, review=False)
    assert not ItemRepository(conn).get(item.id).notes


def test_set_item_review_marks_existing_item(conn):
    from tcginventory.services.review import REVIEW_NOTE, set_item_review

    item = _post_pikachu(conn, review=False)
    updated = set_item_review(conn, item.id, True)
    assert updated.review is True
    assert updated.notes == REVIEW_NOTE
    # Persisted, so polling will protect it just like an entry-marked item.
    assert ItemRepository(conn).get(item.id).review is True


def test_set_item_review_unmark_preserves_user_note(conn):
    from tcginventory.services.review import REVIEW_NOTE, set_item_review

    item = _post_pikachu(conn, review=False)
    it = ItemRepository(conn).get(item.id)
    it.notes = "Signed copy"
    ItemRepository(conn).update(it)
    conn.commit()

    marked = set_item_review(conn, item.id, True)
    assert marked.notes == f"Signed copy {REVIEW_NOTE}"
    unmarked = set_item_review(conn, item.id, False)
    assert unmarked.review is False
    assert unmarked.notes == "Signed copy"  # only the marker is stripped


def test_poll_protects_reviewed_current_but_saves_history(conn):
    item = _post_pikachu(conn, review=True)
    PricingService(conn, provider=TcgcsvProvider(fetch_json=_fake_fetch)).refresh(
        as_of="2026-02-01T00:00:00+00:00"
    )
    refreshed = ItemRepository(conn).get(item.id)
    assert refreshed.market_value_cents == 3000  # manual value protected (not 999)
    assert PriceSnapshotRepository(conn).history_for_item(item.id)  # history saved


def test_poll_updates_unmarked_current(conn):
    item = _post_pikachu(conn, review=False)
    PricingService(conn, provider=TcgcsvProvider(fetch_json=_fake_fetch)).refresh(
        as_of="2026-02-01T00:00:00+00:00"
    )
    assert ItemRepository(conn).get(item.id).market_value_cents == 999


def test_inventory_row_reviewed_uses_manual_value_without_polling(conn):
    _post_pikachu(conn, review=True)
    (row,) = InventoryService(conn).inventory_rows()
    assert row.review is True
    assert row.current_price_cents == 3000  # manual value, no poll required


def test_view_manual_price_and_unmark(app, conn):
    from tcginventory.ui.models.inventory_model import MARKET_CURRENT_COL, REVIEW_ROLE
    from tcginventory.ui.views.inventory import InventoryView

    item = _post_pikachu(conn, review=True)
    PricingService(conn, provider=TcgcsvProvider(fetch_json=_fake_fetch)).refresh(
        as_of="2026-02-01T00:00:00+00:00"
    )  # records a 999 snapshot; reviewed current stays 3000

    view = InventoryView(conn)
    idx = view._model.index(0, MARKET_CURRENT_COL)
    assert view._model.data(idx, REVIEW_ROLE) is True

    # Inline edit keeps the card marked and sets its price.
    view._set_manual_price(item.id, 5000)
    edited = ItemRepository(conn).get(item.id)
    assert edited.market_value_cents == 5000
    assert edited.review is True

    # Second-click commit: unmark and adopt the latest polled price (999).
    view._unmark_and_refresh(item.id)
    done = ItemRepository(conn).get(item.id)
    assert done.review is False
    assert done.market_value_cents == 999
    view.deleteLater()


def test_detail_dialog_marks_item_for_review(app, conn):
    from tcginventory.ui.views.item_detail import ItemDetailDialog

    item = _post_pikachu(conn, review=False)
    dlg = ItemDetailDialog(conn, item.id)
    dlg._review.setChecked(True)  # user ticks "Mark for review"
    marked = ItemRepository(conn).get(item.id)
    assert marked.review is True
    assert "Manual price changed detected." in (marked.notes or "")
    dlg.deleteLater()


def test_armed_button_reverts_when_selection_moves(app, conn):
    from tcginventory.ui.views.inventory import InventoryView

    item = _post_pikachu(conn, review=True)
    view = InventoryView(conn)
    delegate = view._review_delegate
    delegate._armed_item_id = item.id  # simulate the first click (armed)
    delegate.note_selected_item(999)  # selecting a different card
    assert delegate._armed_item_id is None  # reverted to stage one
    view.deleteLater()
