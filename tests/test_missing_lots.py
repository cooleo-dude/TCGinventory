"""Stolen / missing lots — reversible $0 impairment (keeps cost basis)."""

from __future__ import annotations

import pytest

from conftest import buy, sell
from tcginventory.services.exceptions import InsufficientQuantityError
from tcginventory.services.reporting import ReportingService


def test_missing_lot_impairment_and_reversal(svc, inv, conn):
    # Paid $80 for a card whose market is $100.
    lot_id = buy(svc, "X", 8000, 10000, "2026-01-01").lots[0].id
    rpt = ReportingService(conn)
    assert rpt.cost_basis_cents() == 8000
    assert rpt.collection_value_cents() == 10000
    assert rpt.unrealized_pl_cents() == 2000

    # Mark stolen → valued at $0 market, cost basis retained → loss = cost basis.
    assert inv.set_lot_missing(lot_id, True) is True
    conn.commit()
    assert rpt.collection_value_cents() == 0
    assert rpt.cost_basis_cents() == 8000
    assert rpt.unrealized_pl_cents() == -8000  # the purchase price, as a loss

    # Reversible → restored exactly.
    assert inv.set_lot_missing(lot_id, False) is True
    conn.commit()
    assert rpt.collection_value_cents() == 10000
    assert rpt.unrealized_pl_cents() == 2000


def test_missing_row_shows_missing_status(svc, inv, conn):
    lot_id = buy(svc, "X", 8000, 10000, "2026-01-01").lots[0].id
    inv.set_lot_missing(lot_id, True)
    conn.commit()
    row = next(r for r in inv.inventory_rows() if r.lot_id == lot_id)
    assert row.sold_status == "Missing"
    assert row.missing is True
    assert row.current_price_cents == 0
    assert row.unrealized_cents == -8000  # loss = purchase price
    assert row.in_stock is False


def test_missing_lot_not_on_hand_or_sellable(svc, inv, conn):
    result = buy(svc, "X", 8000, 10000, "2026-01-01")
    item_id = result.lots[0].item_id
    lot_id = result.lots[0].id
    assert inv.on_hand(item_id) == 1
    assert any(i.id == item_id for i, _ in inv.sellable_items())

    inv.set_lot_missing(lot_id, True)
    conn.commit()
    assert inv.on_hand(item_id) == 0
    assert inv.item_position(item_id).remaining_qty == 0
    assert not any(i.id == item_id for i, _ in inv.sellable_items())


def test_missing_lot_cannot_be_sold(svc, inv, conn):
    result = buy(svc, "X", 8000, 10000, "2026-01-01")
    item_id = result.lots[0].item_id
    inv.set_lot_missing(result.lots[0].id, True)
    conn.commit()
    # Its units are gone → a disposal finds no open lot to draw from.
    with pytest.raises(InsufficientQuantityError):
        sell(svc, item_id, 1, 12000, "2026-02-01")


def test_toggle_is_audited_and_noop_safe(svc, inv, conn):
    lot_id = buy(svc, "X", 8000, 10000, "2026-01-01").lots[0].id
    assert inv.set_lot_missing(lot_id, False) is False  # already not missing
    assert inv.set_lot_missing(lot_id, True) is True
    conn.commit()
    entries = inv.audit.list_for_entity("lot", lot_id)
    assert entries and entries[-1].note == "marked missing"
