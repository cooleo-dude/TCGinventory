"""Regression: a non-divisible multi-quantity purchase splits into two lots
that share one SKU — it must not crash on the (removed) UNIQUE sku index."""

from __future__ import annotations

from conftest import buy
from tcginventory.data.repositories import LotRepository
from tcginventory.services.transactions import TransactionService


def test_non_divisible_purchase_splits_lots_sharing_one_sku(conn):
    svc = TransactionService(conn)
    lots_repo = LotRepository(conn)

    # $5.00 across 6 copies doesn't divide evenly → 5 @ 83¢ + 1 @ 85¢.
    result = buy(svc, "Meowth", 500, 500, "2026-06-01", quantity=6)
    item_id = result.lots[0].item_id

    lots = lots_repo.list_for_item(item_id)
    assert len(lots) == 2
    assert sum(lot.quantity for lot in lots) == 6
    # cost basis reconciles to the cent (invariant 6)
    assert sum(lot.unit_cost_cents * lot.quantity for lot in lots) == 500
    # the split lots share the acquisition's single SKU
    assert len({lot.sku for lot in lots}) == 1
    assert lots_repo.distinct_sku_count(item_id) == 1
