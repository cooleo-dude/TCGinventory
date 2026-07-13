"""Hard-delete of a mistaken inventory entry (a lot + its purchase line)."""

from __future__ import annotations

from conftest import buy, incoming, sell
from tcginventory.data.repositories import (
    ItemRepository,
    LotConsumptionRepository,
    LotRepository,
    PriceSnapshotRepository,
    TransactionRepository,
)
from tcginventory.domain.models import PriceSnapshot
from tcginventory.services.inventory import InventoryService
from tcginventory.services.reporting import ReportingService
from tcginventory.services.transactions import (
    OutgoingCard,
    TransactionRequest,
    TransactionService,
)


def _item_id(conn, name):
    return next(i.id for i in ItemRepository(conn).list_all() if i.name == name)


def test_delete_single_card_purchase_removes_all_traces(conn):
    svc = TransactionService(conn)
    result = buy(svc, "Oops", 1000, 1000, "2026-01-01")
    lot_id = result.lots[0].id

    svc.delete_inventory_entry(lot_id)

    assert LotRepository(conn).list_all() == []
    assert TransactionRepository(conn).list_all() == []  # empty txn removed
    assert ItemRepository(conn).list_all() == []  # orphaned item removed
    assert ReportingService(conn).net_cash_cents() == 0


def test_delete_one_card_from_multicard_purchase_adjusts_cash(conn):
    svc = TransactionService(conn)
    # Buy A (mv $60) + B (mv $40) for $80 cash → basis A=4800, B=3200.
    svc.post(
        TransactionRequest(
            occurred_at="2026-01-01",
            cash_out_cents=8000,
            incoming=[incoming("A", 6000), incoming("B", 4000)],
        )
    )
    a_lot = LotRepository(conn).list_for_item(_item_id(conn, "A"))[0]
    b_lot = LotRepository(conn).list_for_item(_item_id(conn, "B"))[0]
    assert (a_lot.unit_cost_cents, b_lot.unit_cost_cents) == (4800, 3200)

    svc.delete_inventory_entry(a_lot.id)

    # A is gone (item orphaned + removed); B untouched; cash reduced by A's basis.
    assert "A" not in {i.name for i in ItemRepository(conn).list_all()}
    (remaining,) = InventoryService(conn).inventory_rows()
    assert remaining.item.name == "B"
    assert remaining.purchase_price_cents == 3200  # unchanged
    assert ReportingService(conn).net_cash_cents() == -3200  # was -8000, +4800
    (txn,) = TransactionRepository(conn).list_all()
    assert txn.cash_delta_cents == -3200


def test_delete_priced_item_removes_snapshots(conn):
    # An item with price-history snapshots must still delete: the snapshots
    # FK-reference the item, so they have to be cleared first (regression —
    # this previously failed with a FOREIGN KEY constraint error).
    svc = TransactionService(conn)
    result = buy(svc, "Priced", 1000, 1000, "2026-01-01")
    item_id = result.lots[0].item_id
    snaps = PriceSnapshotRepository(conn)
    snaps.add(
        PriceSnapshot(item_id=item_id, price_cents=1200, source="tcgcsv", as_of="")
    )
    conn.commit()

    svc.delete_inventory_entry(result.lots[0].id)

    assert ItemRepository(conn).get(item_id) is None
    assert snaps.history_for_item(item_id) == []


def test_delete_sold_lot_succeeds_and_stays_consistent(conn):
    # A lot that has been sold can still be deleted (no guardrails); its
    # consumption record is cleaned up so no dangling references remain.
    svc = TransactionService(conn)
    result = buy(svc, "Sold", 1000, 1000, "2026-01-01")
    item_id = result.lots[0].item_id
    sale = sell(svc, item_id, 1, 1500, "2026-02-01")
    lot_id = result.lots[0].id

    svc.delete_inventory_entry(lot_id)

    assert LotRepository(conn).get(lot_id) is None
    assert LotConsumptionRepository(conn).list_for_lot(lot_id) == []
    # The sale transaction is left intact (only the cost-basis link is gone).
    assert TransactionRepository(conn).get(sale.transaction.id) is not None


def test_delete_from_trade_succeeds(conn):
    # Previously blocked ("came from a trade or a transaction with cash
    # received"); now it just deletes.
    svc = TransactionService(conn)
    buy(svc, "Give", 1000, 1000, "2026-01-01")
    give_id = _item_id(conn, "Give")
    svc.post(
        TransactionRequest(
            occurred_at="2026-02-01",
            outgoing=[
                OutgoingCard(item_id=give_id, quantity=1, agreed_value_cents=1500)
            ],
            incoming=[incoming("Get", 1200)],
            cash_in_cents=300,  # cash received → cash_delta > 0
        )
    )
    get_lot = LotRepository(conn).list_for_item(_item_id(conn, "Get"))[0]

    svc.delete_inventory_entry(get_lot.id)

    assert LotRepository(conn).get(get_lot.id) is None
    assert "Get" not in {i.name for i in ItemRepository(conn).list_all()}
