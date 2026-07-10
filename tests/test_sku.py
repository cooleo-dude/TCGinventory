"""Tests for SKU generation, sequencing, lookup, and backfill (headless)."""

from __future__ import annotations

from conftest import buy
from tcginventory.data.repositories import ItemRepository, LotRepository
from tcginventory.domain.enums import Category, Condition, GradingCompany, Intent
from tcginventory.domain.models import Item, Lot
from tcginventory.services.inventory import InventoryService
from tcginventory.services.sku import backfill_skus, make_base
from tcginventory.services.transactions import TransactionService


def _charizard(svc, occurred_at, condition=Condition.NM):
    return buy(
        svc,
        "Charizard VMAX",
        6000,
        6000,
        occurred_at,
        set_code="CPA",
        set_name="Champion's Path",
        card_number="074/073",
        condition=condition,
    )


def test_make_base_formats():
    item = Item(
        name="Charizard VMAX",
        category=Category.SINGLE,
        set_code="CPA",
        card_number="074/073",
        condition=Condition.NM,
    )
    assert make_base(item) == "CHA-CPA-074-NM-EN"


def test_sku_sequence_per_identity(conn):
    svc = TransactionService(conn)
    first = _charizard(svc, "2026-01-01")
    assert first.lots[0].sku == "CHA-CPA-074-NM-EN-0001"

    # Second copy of the SAME identity → next in sequence.
    second = _charizard(svc, "2026-02-01")
    assert second.lots[0].sku == "CHA-CPA-074-NM-EN-0002"

    # A different condition is a different identity → its own sequence.
    lp = _charizard(svc, "2026-03-01", condition=Condition.LP)
    assert lp.lots[0].sku == "CHA-CPA-074-LP-EN-0001"


def test_graded_sku_uses_company_and_grade(conn):
    svc = TransactionService(conn)
    result = buy(
        svc,
        "Charizard",
        45000,
        45000,
        "2026-01-01",
        set_code="BAS",
        card_number="004/102",
        is_graded=True,
        grading_company=GradingCompany.PSA,
        grade="10",
    )
    assert result.lots[0].sku == "CHA-BAS-004-PSA10-EN-0001"


def test_sku_lookup_by_value(conn):
    svc = TransactionService(conn)
    result = _charizard(svc, "2026-01-01")
    sku = result.lots[0].sku
    found = LotRepository(conn).find_by_sku(sku)
    assert found is not None
    assert found.item_id == result.lots[0].item_id
    assert LotRepository(conn).find_by_sku("NOPE-0001") is None


def test_inventory_rows_expose_skus(conn):
    svc = TransactionService(conn)
    _charizard(svc, "2026-01-01")
    (row,) = InventoryService(conn).inventory_rows()
    assert row.skus == ("CHA-CPA-074-NM-EN-0001",)


def test_backfill_assigns_missing_skus(conn):
    items = ItemRepository(conn)
    lots = LotRepository(conn)
    item = items.add(
        Item(
            name="Blastoise",
            category=Category.SINGLE,
            set_code="BS",
            card_number="009/102",
            condition=Condition.NM,
        )
    )
    lots.add(
        Lot(
            item_id=item.id,
            acquired_at="2026-01-01",
            quantity=1,
            remaining_qty=1,
            unit_cost_cents=1000,
            acquired_market_value_cents=1000,
            intent=Intent.RESALE,
            sku=None,
        )
    )
    conn.commit()

    assert backfill_skus(conn) == 1
    (lot,) = lots.list_for_item(item.id)
    assert lot.sku == "BLA-BS-009-NM-EN-0001"
    # Idempotent: nothing left to fill.
    assert backfill_skus(conn) == 0
