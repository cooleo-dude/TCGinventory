"""A review-protected item's value/flag survive a later acquisition."""

from __future__ import annotations

from conftest import buy, incoming
from tcginventory.data.repositories import ItemRepository
from tcginventory.services.transactions import TransactionRequest, TransactionService


def test_deliberate_review_acquisition_overwrites_value(conn):
    # A re-acquisition that *deliberately* carries a reviewed value (e.g. a manual
    # auction-export price) DOES overwrite the item's current value + review flag.
    svc = TransactionService(conn)
    r = buy(svc, "Alakazam-EX", 8000, 15500, "2026-01-01")
    item_id = r.lots[0].item_id
    items = ItemRepository(conn)
    it = items.get(item_id)
    it.review = True
    it.market_value_cents = 8000
    items.update(it)
    conn.commit()

    inc = incoming("Alakazam-EX", 6000)  # same identity as the buy
    inc.review = True  # deliberate reviewed value
    svc.post(TransactionRequest(
        occurred_at="2026-02-01", cash_out_cents=9000, incoming=[inc]
    ))
    it = items.get(item_id)
    assert it.review is True
    assert it.market_value_cents == 6000  # overwritten by the deliberate value


def test_reacquiring_review_item_preserves_reviewed_value(conn):
    svc = TransactionService(conn)
    # Acquire a card, then flag it for review with a hand-set (MP) value.
    r = buy(svc, "Alakazam-EX", 8000, 15500, "2026-01-01")
    item_id = r.lots[0].item_id
    items = ItemRepository(conn)
    it = items.get(item_id)
    it.review = True
    it.market_value_cents = 8000  # user's condition-adjusted value ($80)
    items.update(it)
    conn.commit()

    # Acquire ANOTHER copy of the same identity, entering a higher (NM) value,
    # not flagged for review.
    r2 = buy(svc, "Alakazam-EX", 9000, 16000, "2026-02-01")
    assert r2.lots[0].item_id == item_id  # matched the same item

    it = items.get(item_id)
    assert it.review is True  # flag preserved
    assert it.market_value_cents == 8000  # reviewed value NOT overwritten
    # The new lot still captures its own acquisition snapshot (cost allocation).
    assert r2.lots[0].acquired_market_value_cents == 16000


def test_reacquiring_non_review_item_still_updates_value(conn):
    # Control: a normal (non-review) item still adopts the new acquisition value.
    svc = TransactionService(conn)
    r = buy(svc, "Pikachu", 100, 200, "2026-01-01")
    item_id = r.lots[0].item_id
    buy(svc, "Pikachu", 100, 350, "2026-02-01")
    assert ItemRepository(conn).get(item_id).market_value_cents == 350
