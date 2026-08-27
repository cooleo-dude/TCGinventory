"""Splitting a lot by quantity to change one copy's intent (ticket 60).

The Personal Collection import needs "keep one of these three for myself". This
is the money path in that feature, so the invariants are asserted directly:
totals must not move, only the per-intent slices.
"""

from __future__ import annotations

import sqlite3

import pytest

from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.data.repositories import (
    AuditLogRepository,
    ItemRepository,
    LotRepository,
)
from tcginventory.domain.enums import Category, Condition, Intent
from tcginventory.domain.models import Item, Lot
from tcginventory.services.exceptions import TransactionStateError
from tcginventory.services.inventory import InventoryService
from tcginventory.services.reporting import ReportingService


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    yield c
    c.close()


def _lot(conn, qty=3, unit=1000, intent=Intent.RESALE) -> Lot:
    item = ItemRepository(conn).add(
        Item(name="Testmon", category=Category.SINGLE, condition=Condition.NM)
    )
    return LotRepository(conn).add(
        Lot(
            item_id=item.id,
            acquired_at="2026-08-01",
            quantity=qty,
            remaining_qty=qty,
            unit_cost_cents=unit,
            acquired_market_value_cents=1200,
            intent=intent,
            sku="TEST-0001",
        )
    )


# ---------------------------------------------------------------------------
# the split
# ---------------------------------------------------------------------------


def test_one_unit_of_three_moves_to_personal(conn):
    """The shape the user asked for: keep one, the rest stay stock."""
    svc = InventoryService(conn)
    lot = _lot(conn, qty=3)
    moved = svc.split_lot_for_intent(lot.id, 1, Intent.PERSONAL)
    conn.commit()

    source = LotRepository(conn).get(lot.id)
    assert (source.quantity, source.remaining_qty) == (2, 2)
    assert source.intent is Intent.RESALE
    assert (moved.quantity, moved.remaining_qty) == (1, 1)
    assert moved.intent is Intent.PERSONAL
    assert moved.item_id == source.item_id


def test_the_split_preserves_cost_basis_exactly(conn):
    """No money is re-divided, so there is no residual to place:
    2 x unit + 1 x unit == 3 x unit. Reverting to a re-allocation would break
    this the moment unit_cost did not divide evenly."""
    svc = InventoryService(conn)
    lot = _lot(conn, qty=3, unit=1001)  # deliberately not divisible by 3
    before = ReportingService(conn).cost_basis_cents()
    moved = svc.split_lot_for_intent(lot.id, 1, Intent.PERSONAL)
    conn.commit()

    source = LotRepository(conn).get(lot.id)
    assert source.unit_cost_cents == moved.unit_cost_cents == 1001
    assert source.quantity * 1001 + moved.quantity * 1001 == 3 * 1001
    assert ReportingService(conn).cost_basis_cents() == before


def test_totals_do_not_move_only_the_per_intent_slices(conn):
    """Intent is a filter dimension, not a valuation input. This is what makes
    the import safe, so it is asserted rather than assumed."""
    svc = InventoryService(conn)
    rep = ReportingService(conn)
    lot = _lot(conn, qty=3, unit=1000)
    total_before = rep.cost_basis_cents()
    resale_before = rep.cost_basis_cents(Intent.RESALE)
    personal_before = rep.cost_basis_cents(Intent.PERSONAL)

    svc.split_lot_for_intent(lot.id, 1, Intent.PERSONAL)
    conn.commit()

    assert rep.cost_basis_cents() == total_before
    assert rep.cost_basis_cents(Intent.RESALE) == resale_before - 1000
    assert rep.cost_basis_cents(Intent.PERSONAL) == personal_before + 1000


def test_on_hand_is_unchanged_by_the_split(conn):
    """INV-ONHAND-SUM: the sum over open lots still equals the units held."""
    svc = InventoryService(conn)
    lot = _lot(conn, qty=3)
    item_id = LotRepository(conn).get(lot.id).item_id
    assert svc.on_hand(item_id) == 3
    svc.split_lot_for_intent(lot.id, 1, Intent.PERSONAL)
    conn.commit()
    assert svc.on_hand(item_id) == 3
    assert svc.on_hand(item_id, Intent.PERSONAL) == 1
    assert svc.on_hand(item_id, Intent.RESALE) == 2


def test_the_new_lot_shares_the_acquisition(conn):
    """Migration 0016: several lots on one acquisition line share a SKU. The
    split is the same acquired copies, so it keeps the line, SKU and date."""
    svc = InventoryService(conn)
    lot = _lot(conn, qty=2)
    moved = svc.split_lot_for_intent(lot.id, 1, Intent.PERSONAL)
    conn.commit()
    source = LotRepository(conn).get(lot.id)
    assert moved.sku == source.sku
    assert moved.acquired_at == source.acquired_at
    assert moved.acquired_txn_line_id == source.acquired_txn_line_id
    assert moved.acquired_market_value_cents == source.acquired_market_value_cents


def test_moving_the_whole_lot_flips_intent_without_splitting(conn):
    """A lot of 1 — the common case — needs no second lot."""
    svc = InventoryService(conn)
    lot = _lot(conn, qty=1)
    assert svc.split_lot_for_intent(lot.id, 1, Intent.PERSONAL) is None
    conn.commit()
    assert LotRepository(conn).get(lot.id).intent is Intent.PERSONAL
    assert len(LotRepository(conn).list_all()) == 1


def test_the_split_is_audited(conn):
    svc = InventoryService(conn)
    lot = _lot(conn, qty=3)
    moved = svc.split_lot_for_intent(lot.id, 1, Intent.PERSONAL)
    conn.commit()
    entries = AuditLogRepository(conn).list_for_entity("lot", lot.id)
    assert entries, "a split must leave an audit trail"
    after = entries[-1].after_json
    assert str(moved.id) in after and "personal" in after


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_a_partially_sold_lot_cannot_be_split(conn):
    """The negative test. Such a lot has lot_consumption rows referencing the
    quantity it would be leaving; re-pointing those is its own design."""
    svc = InventoryService(conn)
    lot = _lot(conn, qty=3)
    stored = LotRepository(conn).get(lot.id)
    stored.remaining_qty = 2  # one unit sold
    LotRepository(conn).update(stored)
    conn.commit()
    with pytest.raises(TransactionStateError, match="sold units"):
        svc.split_lot_for_intent(lot.id, 1, Intent.PERSONAL)


def test_moving_more_units_than_the_lot_holds_is_refused(conn):
    svc = InventoryService(conn)
    lot = _lot(conn, qty=2)
    with pytest.raises(TransactionStateError):
        svc.split_lot_for_intent(lot.id, 3, Intent.PERSONAL)


def test_moving_zero_units_is_refused(conn):
    svc = InventoryService(conn)
    lot = _lot(conn, qty=2)
    with pytest.raises(TransactionStateError):
        svc.split_lot_for_intent(lot.id, 0, Intent.PERSONAL)


def test_an_unknown_lot_is_refused(conn):
    with pytest.raises(TransactionStateError, match="no lot"):
        InventoryService(conn).split_lot_for_intent(999, 1, Intent.PERSONAL)


# ---------------------------------------------------------------------------
# the audited intent change on its own
# ---------------------------------------------------------------------------


def test_set_lot_intent_writes_an_audit_row(conn):
    """The gap this closes: the Inventory grid changed intent straight through
    LotRepository.update() with no audit row at all."""
    svc = InventoryService(conn)
    lot = _lot(conn, qty=1, intent=Intent.RESALE)
    assert svc.set_lot_intent(lot.id, Intent.PERSONAL) is True
    conn.commit()
    assert LotRepository(conn).get(lot.id).intent is Intent.PERSONAL
    entries = AuditLogRepository(conn).list_for_entity("lot", lot.id)
    assert entries and "personal" in entries[-1].after_json


def test_setting_the_same_intent_is_a_no_op_and_writes_nothing(conn):
    svc = InventoryService(conn)
    lot = _lot(conn, qty=1, intent=Intent.RESALE)
    assert svc.set_lot_intent(lot.id, Intent.RESALE) is False
    conn.commit()
    assert AuditLogRepository(conn).list_for_entity("lot", lot.id) == []


def test_ripping_lots_stay_valued_at_zero_after_an_intent_change(conn):
    """RIPPING-ZERO is the one intent that changes a number; make sure the new
    path cannot be used to sneak a ripping lot into a positive valuation."""
    svc = InventoryService(conn)
    lot = _lot(conn, qty=1, intent=Intent.RIPPING)
    rows = {r.lot_id: r for r in svc.inventory_rows()}
    assert rows[lot.id].current_price_cents == 0
    svc.set_lot_intent(lot.id, Intent.PERSONAL)
    conn.commit()
    rows = {r.lot_id: r for r in svc.inventory_rows()}
    assert (
        rows[lot.id].current_price_cents != 0
        or rows[lot.id].current_price_cents is None
    )
