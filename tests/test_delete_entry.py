"""Hard-delete of a mistaken inventory entry (a lot + its purchase line)."""

from __future__ import annotations

import pytest

from conftest import buy, incoming, sell
from tcginventory.data.repositories import (
    ItemRepository,
    LotRepository,
    TransactionRepository,
)
from tcginventory.services.exceptions import TransactionStateError
from tcginventory.services.inventory import InventoryService
from tcginventory.services.reporting import ReportingService
from tcginventory.services.transactions import TransactionRequest, TransactionService


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


def test_delete_blocked_when_units_sold(conn):
    svc = TransactionService(conn)
    result = buy(svc, "Sold", 1000, 1000, "2026-01-01")
    item_id = result.lots[0].item_id
    sell(svc, item_id, 1, 1500, "2026-02-01")

    with pytest.raises(TransactionStateError):
        svc.delete_inventory_entry(result.lots[0].id)
    # Nothing removed.
    assert LotRepository(conn).get(result.lots[0].id) is not None
