"""InventoryService tests — positions and per-lot valuation (ripping at $0)."""

from __future__ import annotations

import pytest

from conftest import buy, sell
from tcginventory.domain.enums import Intent
from tcginventory.services.inventory import InventoryService
from tcginventory.services.transactions import TransactionService


@pytest.fixture()
def svc(conn):
    return TransactionService(conn)


@pytest.fixture()
def inv(conn):
    return InventoryService(conn)


def test_position_reflects_cost_and_current_market(svc, inv):
    # Paid $80 for a card whose market is $100 → basis 80, market 100, +20.
    result = buy(svc, "X", 8000, 10000, "2026-01-01")
    item_id = result.lots[0].item_id
    pos = inv.item_position(item_id)
    assert pos.remaining_qty == 1
    assert pos.cost_basis_cents == 8000
    assert pos.market_value_cents == 10000
    assert pos.unrealized_pl_cents == 2000


def test_ripping_lot_valued_at_zero_shows_loss(svc, inv):
    result = buy(svc, "Booster Box", 12000, 12000, "2026-01-01", intent=Intent.RIPPING)
    item_id = result.lots[0].item_id
    pos = inv.item_position(item_id)
    assert pos.market_value_cents == 0
    assert pos.unrealized_pl_cents == -12000  # loss = full cost basis


def test_on_hand_and_intent_filter(svc, inv, conn):
    buy(svc, "Keeper", 1000, 1000, "2026-01-01", intent=Intent.PERSONAL)
    buy(svc, "Flip", 2000, 2000, "2026-01-02", intent=Intent.RESALE)

    all_positions = inv.all_positions()
    assert len(all_positions) == 2

    resale_only = inv.all_positions(intent=Intent.RESALE)
    assert len(resale_only) == 1
    assert resale_only[0].cost_basis_cents == 2000


def test_on_hand_counts_units(svc, inv):
    result = buy(svc, "Common", 3000, 1000, "2026-01-01", quantity=3)
    assert inv.on_hand(result.lots[0].item_id) == 3


def test_inventory_rows_sold_status_and_intent(svc, inv):
    # A resale card, fully sold.
    p = buy(svc, "Flip", 1000, 1000, "2026-01-01", intent=Intent.RESALE)
    sell(svc, p.lots[0].item_id, 1, 1500, "2026-02-01")
    # A personal keeper still held.
    buy(svc, "Keeper", 2000, 2000, "2026-01-02", intent=Intent.PERSONAL)

    rows = {r.item.name: r for r in inv.inventory_rows()}
    assert rows["Flip"].sold_status == "sold"
    assert rows["Flip"].on_hand == 0
    assert rows["Flip"].intent == "resale"
    assert rows["Keeper"].sold_status == "unsold"
    assert rows["Keeper"].intent == "personal"


def test_inventory_rows_market_at_purchase(svc, inv):
    # Paid $80 for a card whose entered market was $100 (an 80%-of-market buy).
    buy(svc, "Deal", 8000, 10000, "2026-01-01")
    (row,) = inv.inventory_rows()
    assert row.cost_basis_cents == 8000  # what you paid
    assert row.market_at_purchase_cents == 10000  # market when entered


def test_inventory_rows_partial_sale(svc, inv):
    p = buy(svc, "Box", 3000, 1000, "2026-01-01", quantity=3)
    sell(svc, p.lots[0].item_id, 1, 1500, "2026-02-01")
    (row,) = inv.inventory_rows()
    assert row.on_hand == 2
    assert row.acquired_qty == 3
    assert row.sold_status == "partial"
