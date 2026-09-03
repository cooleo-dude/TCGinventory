"""TransactionService tests — the acceptance scenarios plus void/edit/adjust."""

from __future__ import annotations

import pytest

from conftest import buy, incoming, make_item, sell
from tcginventory.data.repositories import (
    AuditLogRepository,
    ItemRepository,
    LotRepository,
    TransactionRepository,
)
from tcginventory.domain.enums import (
    AuditAction,
    Condition,
    Direction,
    Intent,
    TransactionStatus,
    TransactionType,
)
from tcginventory.services.exceptions import (
    EmptyTransactionError,
    InsufficientQuantityError,
    InvalidTransactionError,
    RippingNotSellableError,
    TransactionStateError,
)
from tcginventory.services.transactions import (
    OutgoingCard,
    TransactionRequest,
    TransactionService,
)


@pytest.fixture()
def svc(conn):
    return TransactionService(conn)


# -- acceptance scenarios ---------------------------------------------------


def test_scenario_1_multi_card_acquisition_split(svc, conn):
    req = TransactionRequest(
        occurred_at="2026-01-01",
        cash_out_cents=8000,
        incoming=[
            incoming("A", 6000, intent=Intent.RESALE),
            incoming("B", 3000, intent=Intent.PERSONAL),
            incoming("C", 1000, intent=Intent.RESALE),
        ],
    )
    result = svc.post(req)

    assert result.transaction.type is TransactionType.PURCHASE
    assert result.transaction.cash_delta_cents == -8000
    assert result.realized_pl_cents == 0
    assert result.net_consideration_in_cents == 8000
    assert sorted(lot.unit_cost_cents for lot in result.lots) == [800, 2400, 4800]
    # Invariant 6: allocated basis sums exactly to net consideration.
    assert sum(lot.unit_cost_cents * lot.quantity for lot in result.lots) == 8000
    # One item created per distinct card.
    assert len(ItemRepository(conn).list_all()) == 3


def test_scenario_2_two_lots_fifo_sale(svc, conn):
    first = buy(svc, "Charizard X", 9000, 9000, "2026-01-01")
    buy(svc, "Charizard X", 7500, 7500, "2026-02-01")
    item_id = first.lots[0].item_id

    # one item, two lots
    assert len(ItemRepository(conn).list_all()) == 1
    assert len(LotRepository(conn).list_for_item(item_id)) == 2

    result = sell(svc, item_id, 1, 12000, "2026-03-01")
    assert result.transaction.type is TransactionType.SALE
    assert result.realized_pl_cents == 3000  # 12000 − 9000 (FIFO took the $90 lot)

    # The $75 lot remains on hand; the $90 lot is exhausted.
    open_lots = LotRepository(conn).open_lots_for_item(item_id)
    assert [lot.unit_cost_cents for lot in open_lots] == [7500]
    assert open_lots[0].remaining_qty == 1


def test_scenario_3_rounding_residual(svc):
    req = TransactionRequest(
        occurred_at="2026-01-01",
        cash_out_cents=10000,
        incoming=[
            incoming("A", 1200),
            incoming("B", 1100),
            incoming("C", 1000),
        ],
    )
    result = svc.post(req)
    costs = sorted(lot.unit_cost_cents for lot in result.lots)
    assert costs == [3030, 3333, 3637]  # residual cent to the $12 line
    assert sum(lot.unit_cost_cents * lot.quantity for lot in result.lots) == 10000


def test_scenario_4_trade_with_cash_back(svc, conn):
    a = buy(svc, "Card A", 2000, 2000, "2026-01-01")
    item_a = a.lots[0].item_id

    req = TransactionRequest(
        occurred_at="2026-03-01",
        outgoing=[OutgoingCard(item_id=item_a, quantity=1)],  # value from the deal
        incoming=[incoming("Card B", 4000, intent=Intent.RESALE)],
        cash_in_cents=1000,  # cash back
    )
    result = svc.post(req)

    assert result.transaction.type is TransactionType.TRADE
    # Value rule: A given at its $20 market. B's basis = cash_out + given − cash_in
    # = 0 + 2000 − 1000 = 1000.
    assert result.net_consideration_in_cents == 1000
    # A's proceeds = cash_in + B market − cash_out = 1000 + 4000 = 5000; − 2000 cost.
    assert result.realized_pl_cents == 3000
    assert result.transaction.cash_delta_cents == 1000
    (b_lot,) = result.lots
    assert b_lot.unit_cost_cents == 1000


def test_scenario_5_oversell_guard_is_atomic(svc, conn):
    result = buy(svc, "Common", 3000, 1000, "2026-01-01", quantity=3)
    item_id = result.lots[0].item_id
    txns_before = len(TransactionRepository(conn).list_all())

    with pytest.raises(InsufficientQuantityError):
        sell(svc, item_id, 5, 5000, "2026-02-01")

    # Nothing was written: no new transaction, lot untouched.
    assert len(TransactionRepository(conn).list_all()) == txns_before
    assert LotRepository(conn).open_lots_for_item(item_id)[0].remaining_qty == 3


# -- type inference & validation -------------------------------------------


def test_pure_purchase_type_and_cash(svc):
    result = buy(svc, "X", 5000, 5000, "2026-01-01")
    assert result.transaction.type is TransactionType.PURCHASE
    assert result.transaction.cash_delta_cents == -5000


def test_empty_transaction_rejected(svc):
    with pytest.raises(EmptyTransactionError):
        svc.post(TransactionRequest(occurred_at="2026-01-01"))


def test_incoming_requires_market_value(svc):
    req = TransactionRequest(
        occurred_at="2026-01-01",
        cash_out_cents=1000,
        incoming=[incoming("X", 0)],
    )
    with pytest.raises(InvalidTransactionError):
        svc.post(req)


# -- match-or-create --------------------------------------------------------


def test_same_identity_matches_one_item_two_lots(svc, conn):
    buy(svc, "Pikachu", 1000, 1000, "2026-01-01", set_name="Base", card_number="58/102")
    buy(svc, "Pikachu", 1200, 1200, "2026-02-01", set_name="Base", card_number="58/102")
    items = ItemRepository(conn).list_all()
    assert len(items) == 1
    assert len(LotRepository(conn).list_for_item(items[0].id)) == 2
    # Most-recent market value wins on the shared item.
    assert items[0].market_value_cents == 1200


# -- void -------------------------------------------------------------------


def test_void_sale_restores_consumed_lot(svc, conn):
    p = buy(svc, "X", 9000, 9000, "2026-01-01")
    item_id = p.lots[0].item_id
    s = sell(svc, item_id, 1, 12000, "2026-02-01")

    svc.void(s.transaction.id)

    lots = LotRepository(conn).open_lots_for_item(item_id)
    assert lots[0].remaining_qty == 1  # restored
    voided = TransactionRepository(conn).get(s.transaction.id)
    assert voided.status is TransactionStatus.VOIDED
    assert voided.voided_at is not None


def test_void_purchase_deletes_created_lots(svc, conn):
    p = buy(svc, "X", 9000, 9000, "2026-01-01")
    item_id = p.lots[0].item_id
    svc.void(p.transaction.id)
    assert LotRepository(conn).list_for_item(item_id) == []


def test_cannot_void_purchase_with_partially_sold_lot(svc, conn):
    p = buy(svc, "X", 9000, 9000, "2026-01-01", quantity=2)
    item_id = p.lots[0].item_id
    sell(svc, item_id, 1, 6000, "2026-02-01")
    with pytest.raises(TransactionStateError):
        svc.void(p.transaction.id)


def test_cannot_void_twice(svc):
    p = buy(svc, "X", 1000, 1000, "2026-01-01")
    svc.void(p.transaction.id)
    with pytest.raises(TransactionStateError):
        svc.void(p.transaction.id)


# -- edit -------------------------------------------------------------------


def test_edit_voids_old_and_posts_new(svc, conn):
    p = buy(svc, "X", 8000, 8000, "2026-01-01")
    old_id = p.transaction.id

    new_req = TransactionRequest(
        occurred_at="2026-01-01",
        cash_out_cents=9000,
        incoming=[incoming("X", 8000)],
    )
    result = svc.edit(old_id, new_req)

    assert result.transaction.id != old_id
    assert TransactionRepository(conn).get(old_id).status is TransactionStatus.VOIDED
    assert result.transaction.status is TransactionStatus.POSTED
    # New basis reflects the corrected price.
    assert sum(lot.unit_cost_cents * lot.quantity for lot in result.lots) == 9000


# -- adjustments ------------------------------------------------------------


def test_adjust_found_adds_inventory(svc, conn):
    item = ItemRepository(conn).add(make_item("Found"))
    txn = svc.adjust_found(item.id, 2, "2026-01-01", unit_cost_cents=500)
    assert txn.type is TransactionType.ADJUSTMENT
    lots = LotRepository(conn).open_lots_for_item(item.id)
    assert sum(lot.remaining_qty for lot in lots) == 2


def test_adjust_writeoff_realizes_loss(svc, conn):
    p = buy(svc, "X", 1000, 1000, "2026-01-01")
    item_id = p.lots[0].item_id
    txn = svc.adjust_writeoff(item_id, 1, "2026-02-01")
    line = next(
        ln
        for ln in svc.lines.list_for_transaction(txn.id)
        if ln.direction.value == "out"
    )
    assert line.realized_pl_cents == -1000  # full cost written off
    assert LotRepository(conn).open_lots_for_item(item_id) == []


# -- ripping ----------------------------------------------------------------


def test_ripping_lot_cannot_be_sold(svc, conn):
    p = buy(svc, "Booster Box", 12000, 12000, "2026-01-01", intent=Intent.RIPPING)
    item_id = p.lots[0].item_id
    with pytest.raises(RippingNotSellableError):
        sell(svc, item_id, 1, 15000, "2026-02-01")


# -- audit ------------------------------------------------------------------


def test_preview_infers_type_and_realized(svc, conn):
    p = buy(svc, "X", 9000, 9000, "2026-01-01")
    item_id = p.lots[0].item_id

    req = TransactionRequest(
        occurred_at="2026-02-01",
        outgoing=[OutgoingCard(item_id=item_id, quantity=1)],
        cash_in_cents=12000,
    )
    preview = svc.preview(req)
    assert preview.inferred_type is TransactionType.SALE
    assert preview.proceeds_cents == 12000  # a sale's proceeds = the cash in
    assert preview.realized_pl_cents == 3000  # 12000 − 9000 FIFO cost
    assert preview.feasible is True


def test_preview_flags_oversell(svc, conn):
    p = buy(svc, "X", 1000, 1000, "2026-01-01")
    item_id = p.lots[0].item_id
    req = TransactionRequest(
        occurred_at="2026-02-01",
        outgoing=[OutgoingCard(item_id=item_id, quantity=5)],
        cash_in_cents=2500,
    )
    preview = svc.preview(req)
    assert preview.feasible is False
    assert preview.shortfalls == {item_id: 4}


def test_preview_purchase_has_no_realized(svc, conn):
    from conftest import incoming

    req = TransactionRequest(
        occurred_at="2026-01-01",
        cash_out_cents=8000,
        incoming=[incoming("A", 6000), incoming("B", 4000)],
    )
    preview = svc.preview(req)
    assert preview.inferred_type is TransactionType.PURCHASE
    assert preview.net_consideration_in_cents == 8000
    assert preview.realized_pl_cents == 0
    assert preview.value_received_cents == 10000


def test_sellable_items_excludes_ripping(svc, conn):
    from tcginventory.services.inventory import InventoryService

    buy(svc, "Sellable", 1000, 1000, "2026-01-01", intent=Intent.RESALE)
    buy(svc, "Box", 12000, 12000, "2026-01-02", intent=Intent.RIPPING)
    names = {item.name for item, _ in InventoryService(conn).sellable_items()}
    assert "Sellable" in names
    assert "Box" not in names


def test_audit_log_records_create_and_void(svc, conn):
    p = buy(svc, "X", 1000, 1000, "2026-01-01")
    svc.void(p.transaction.id)
    entries = AuditLogRepository(conn).list_for_entity("transaction", p.transaction.id)
    actions = [e.action for e in entries]
    assert AuditAction.CREATE in actions
    assert AuditAction.VOID in actions


def _item_id(conn, name):
    return conn.execute("SELECT id FROM item WHERE name = ?", (name,)).fetchone()[0]


def _out_lines(result):
    return [ln for ln in result.lines if ln.direction is Direction.OUT]


def _consumed(conn, line_id):
    return conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM lot_consumption "
        "WHERE transaction_line_id = ?",
        (line_id,),
    ).fetchone()[0]


def test_multiqty_indivisible_sale_records_exact_proceeds(svc, conn):
    # 3 units @ 100 cost; sell all 3 for 1000 (1000 % 3 != 0) → proceeds must not
    # lose the odd cent, and the split reconciles exactly (invariants 2/4/6).
    buy(svc, "Charizard", 300, 100, "2026-01-01", quantity=3)
    item_id = _item_id(conn, "Charizard")
    req = TransactionRequest(
        occurred_at="2026-02-01",
        outgoing=[OutgoingCard(item_id=item_id, quantity=3)],
        cash_in_cents=1000,
    )
    preview = svc.preview(req)
    result = svc.post(req)

    out = _out_lines(result)
    assert len(out) == 2  # split because 1000 % 3 != 0
    proceeds = sum(ln.unit_price_cents * ln.quantity for ln in out)
    realized = sum(ln.realized_pl_cents for ln in out)
    assert proceeds == 1000  # exact — not 999
    assert realized == 1000 - 300  # exact realized P/L
    assert realized == result.realized_pl_cents == preview.realized_pl_cents
    # Invariant 2: each out line's consumption sums to its own quantity.
    for ln in out:
        assert _consumed(conn, ln.id) == ln.quantity


def test_multiqty_divisible_sale_is_a_single_line(svc, conn):
    buy(svc, "Blastoise", 300, 100, "2026-01-01", quantity=3)
    item_id = _item_id(conn, "Blastoise")
    result = sell(svc, item_id, 3, 900, "2026-02-01")  # 900 % 3 == 0 → no split
    out = _out_lines(result)
    assert len(out) == 1
    assert out[0].unit_price_cents == 300 and out[0].quantity == 3


def test_multiqty_indivisible_trade_away_reconciles_exactly(svc, conn):
    # Give 3 of A (mv 200 each) for 1 of B; net_consideration_out = mv(B) = 500,
    # split across the 3 outgoing units (500 % 3 != 0).
    buy(svc, "A", 300, 200, "2026-01-01", quantity=3)
    a_id = _item_id(conn, "A")
    req = TransactionRequest(
        occurred_at="2026-02-01",
        outgoing=[OutgoingCard(item_id=a_id, quantity=3)],
        incoming=[incoming("B", 500, quantity=1)],
    )
    preview = svc.preview(req)
    result = svc.post(req)

    out = _out_lines(result)
    assert len(out) == 2
    assert sum(ln.unit_price_cents * ln.quantity for ln in out) == 500  # exact
    realized = sum(ln.realized_pl_cents for ln in out)
    assert realized == 500 - 300  # proceeds − FIFO cost
    assert realized == result.realized_pl_cents == preview.realized_pl_cents
    for ln in out:
        assert _consumed(conn, ln.id) == ln.quantity


def test_preview_flags_a_card_already_held_under_another_foil(conn):
    """The advisory that stops the holo drift minting silent duplicates.

    Eight of the nine duplicate groups in the real ledger differ by nothing but
    holo_pattern, all created the day card entry began taking the foil from the
    catalog (ticket 67). Neither value is provably wrong, so this warns and lets
    the post through.
    """
    from tcginventory.domain.enums import HoloPattern

    svc = TransactionService(conn)
    buy(
        svc,
        "Charizard-GX",
        1000,
        1000,
        "2026-01-01",
        set_name="SM Black Star Promos",
        card_number="SM60/SM248",
        condition=Condition.NM,
        holo_pattern=HoloPattern.NON_HOLO,
    )

    # The same card again, as the catalog now spells and foils it.
    req = TransactionRequest(
        occurred_at="2026-02-01",
        cash_out_cents=1200,
        incoming=[
            incoming(
                "Charizard GX",
                1200,
                set_name="SM Black Star Promos",
                card_number="SM60/SM248",
                condition=Condition.NM,
                holo_pattern=HoloPattern.HOLO,
            )
        ],
    )
    preview = svc.preview(req)

    (near,) = preview.near_matches
    assert near.incoming_name == "Charizard GX"
    assert near.item_name == "Charizard-GX"
    assert near.holo_pattern == "NH"
    assert near.on_hand == 1
    # Advisory only — the post is still allowed.
    assert preview.feasible


def test_preview_does_not_flag_a_card_that_matches_exactly(conn):
    """Re-buying a card you already own is ordinary, not suspicious."""
    from tcginventory.domain.enums import HoloPattern

    svc = TransactionService(conn)
    kw = dict(
        set_name="SM Black Star Promos",
        card_number="SM60/SM248",
        condition=Condition.NM,
        holo_pattern=HoloPattern.HOLO,
    )
    buy(svc, "Charizard GX", 1000, 1000, "2026-01-01", **kw)

    req = TransactionRequest(
        occurred_at="2026-02-01",
        cash_out_cents=1200,
        incoming=[incoming("Charizard GX", 1200, **kw)],
    )
    assert svc.preview(req).near_matches == []


def test_preview_does_not_flag_a_near_match_with_nothing_on_hand(conn):
    """A printing you have owned but sold out of is not a duplicate risk."""
    from tcginventory.domain.enums import HoloPattern

    svc = TransactionService(conn)
    kw = dict(
        set_name="SM Black Star Promos",
        card_number="SM60/SM248",
        condition=Condition.NM,
    )
    bought = buy(
        svc,
        "Charizard-GX",
        1000,
        1000,
        "2026-01-01",
        holo_pattern=HoloPattern.NON_HOLO,
        **kw,
    )
    svc.post(
        TransactionRequest(
            occurred_at="2026-01-15",
            cash_in_cents=1500,
            outgoing=[OutgoingCard(item_id=bought.lots[0].item_id, quantity=1)],
        )
    )

    req = TransactionRequest(
        occurred_at="2026-02-01",
        cash_out_cents=1200,
        incoming=[incoming("Charizard GX", 1200, holo_pattern=HoloPattern.HOLO, **kw)],
    )
    assert svc.preview(req).near_matches == []
