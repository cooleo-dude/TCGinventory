"""Repository CRUD round-trip tests against an in-memory migrated DB — no Qt."""

from __future__ import annotations

import sqlite3

import pytest

from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.data.repositories import (
    AuditLogRepository,
    ItemRepository,
    LotConsumptionRepository,
    LotRepository,
    TransactionLineRepository,
    TransactionRepository,
)
from tcginventory.domain.enums import (
    AuditAction,
    Category,
    Condition,
    Direction,
    Edition,
    GradingCompany,
    HoloPattern,
    Intent,
    Language,
    TransactionStatus,
    TransactionType,
)
from tcginventory.domain.models import (
    AuditLogEntry,
    Item,
    Lot,
    LotConsumption,
    Transaction,
    TransactionLine,
)


@pytest.fixture()
def conn():
    c = connect(":memory:")
    migrate(c)
    yield c
    c.close()


def _raw_item(name="Charizard VMAX", **overrides) -> Item:
    base = dict(
        name=name,
        category=Category.SINGLE,
        set_name="Champion's Path",
        set_code="CPA",
        card_number="074/073",
        condition=Condition.NM,
        market_value_cents=9000,
    )
    base.update(overrides)
    return Item(**base)


def test_item_add_assigns_id_and_timestamps(conn):
    repo = ItemRepository(conn)
    item = repo.add(_raw_item())
    assert item.id is not None
    assert item.created_at and item.updated_at


def test_item_round_trips_enums_and_none_condition(conn):
    repo = ItemRepository(conn)
    graded = repo.add(
        _raw_item(
            name="Charizard",
            condition=None,
            is_graded=True,
            grading_company=GradingCompany.PSA,
            grade="10",
            cert_number="12345678",
        )
    )
    fetched = repo.get(graded.id)
    assert fetched is not None
    assert fetched.category is Category.SINGLE
    assert fetched.condition is None
    assert fetched.is_graded is True
    assert fetched.grading_company is GradingCompany.PSA
    assert fetched.grade == "10"


def test_item_update_and_delete(conn):
    repo = ItemRepository(conn)
    item = repo.add(_raw_item())
    item.market_value_cents = 12000
    item.location = "Safe"
    repo.update(item)
    fetched = repo.get(item.id)
    assert fetched.market_value_cents == 12000
    assert fetched.location == "Safe"

    repo.delete(item.id)
    assert repo.get(item.id) is None


def test_item_defaults_to_english(conn):
    repo = ItemRepository(conn)
    item = repo.add(_raw_item())  # no language given
    assert repo.get(item.id).language is Language.EN


def test_item_round_trips_language(conn):
    repo = ItemRepository(conn)
    jp = repo.add(_raw_item(language=Language.JP))
    assert repo.get(jp.id).language is Language.JP


def test_item_find_by_identity_matches_and_distinguishes(conn):
    repo = ItemRepository(conn)
    raw_nm = repo.add(_raw_item(condition=Condition.NM))
    # Same identity → matches the existing row.
    match = repo.find_by_identity(_raw_item(condition=Condition.NM))
    assert match is not None and match.id == raw_nm.id
    # Different condition → a distinct item, no match yet.
    assert repo.find_by_identity(_raw_item(condition=Condition.LP)) is None
    # Different language → also a distinct item (language is part of identity).
    assert repo.find_by_identity(_raw_item(language=Language.JP)) is None


def test_item_round_trips_holo_and_edition(conn):
    repo = ItemRepository(conn)
    item = repo.add(
        _raw_item(holo_pattern=HoloPattern.REVERSE_HOLO, edition=Edition.FIRST)
    )
    got = repo.get(item.id)
    assert got.holo_pattern is HoloPattern.REVERSE_HOLO
    assert got.edition is Edition.FIRST


def test_holo_and_edition_are_part_of_identity(conn):
    repo = ItemRepository(conn)
    base = repo.add(
        _raw_item(holo_pattern=HoloPattern.HOLO, edition=Edition.UNLIMITED)
    )
    # Same holo + edition → matches.
    same = _raw_item(holo_pattern=HoloPattern.HOLO, edition=Edition.UNLIMITED)
    assert repo.find_by_identity(same).id == base.id
    # A different foil or edition is a distinct item.
    assert (
        repo.find_by_identity(
            _raw_item(holo_pattern=HoloPattern.REVERSE_HOLO, edition=Edition.UNLIMITED)
        )
        is None
    )
    assert (
        repo.find_by_identity(
            _raw_item(holo_pattern=HoloPattern.HOLO, edition=Edition.FIRST)
        )
        is None
    )


def test_lot_add_list_and_open_lots_fifo(conn):
    items = ItemRepository(conn)
    lots = LotRepository(conn)
    item = items.add(_raw_item())

    lot_a = lots.add(
        Lot(
            item_id=item.id,
            acquired_at="2026-01-01",
            quantity=1,
            remaining_qty=1,
            unit_cost_cents=9000,
            acquired_market_value_cents=9000,
            intent=Intent.RESALE,
        )
    )
    lot_b = lots.add(
        Lot(
            item_id=item.id,
            acquired_at="2026-02-01",
            quantity=1,
            remaining_qty=0,  # fully sold → not "open"
            unit_cost_cents=7500,
            acquired_market_value_cents=9000,
            intent=Intent.RESALE,
        )
    )

    assert {lo.id for lo in lots.list_for_item(item.id)} == {lot_a.id, lot_b.id}
    open_lots = lots.open_lots_for_item(item.id)
    assert [lo.id for lo in open_lots] == [lot_a.id]  # FIFO, excludes sold-out lot

    lot_a.remaining_qty = 0
    lots.update(lot_a)
    assert lots.open_lots_for_item(item.id) == []


def test_lot_requires_existing_item_foreign_key(conn):
    lots = LotRepository(conn)
    with pytest.raises(sqlite3.IntegrityError):
        lots.add(
            Lot(
                item_id=9999,  # no such item
                acquired_at="2026-01-01",
                quantity=1,
                remaining_qty=1,
                unit_cost_cents=100,
                acquired_market_value_cents=100,
                intent=Intent.PERSONAL,
            )
        )


def test_transaction_line_and_consumption_round_trip(conn):
    items = ItemRepository(conn)
    txns = TransactionRepository(conn)
    lines = TransactionLineRepository(conn)
    lots = LotRepository(conn)
    consumptions = LotConsumptionRepository(conn)

    item = items.add(_raw_item())
    txn = txns.add(
        Transaction(
            type=TransactionType.SALE,
            occurred_at="2026-03-01T10:00:00+00:00",
            cash_delta_cents=12000,
        )
    )
    assert txn.status is TransactionStatus.POSTED

    lot = lots.add(
        Lot(
            item_id=item.id,
            acquired_at="2026-01-01",
            quantity=1,
            remaining_qty=1,
            unit_cost_cents=9000,
            acquired_market_value_cents=9000,
            intent=Intent.RESALE,
        )
    )
    line = lines.add(
        TransactionLine(
            transaction_id=txn.id,
            item_id=item.id,
            direction=Direction.OUT,
            quantity=1,
            unit_price_cents=12000,
            realized_pl_cents=3000,
        )
    )
    consumptions.add(
        LotConsumption(
            transaction_line_id=line.id,
            lot_id=lot.id,
            quantity=1,
            unit_cost_cents=9000,
        )
    )

    assert [ln.id for ln in lines.list_for_transaction(txn.id)] == [line.id]
    drawn = consumptions.list_for_line(line.id)
    assert len(drawn) == 1
    assert drawn[0].lot_id == lot.id
    assert drawn[0].unit_cost_cents == 9000
    assert consumptions.list_for_lot(lot.id)[0].transaction_line_id == line.id


def test_transaction_line_cascades_on_transaction_delete(conn):
    items = ItemRepository(conn)
    txns = TransactionRepository(conn)
    lines = TransactionLineRepository(conn)

    item = items.add(_raw_item())
    txn = txns.add(
        Transaction(
            type=TransactionType.PURCHASE, occurred_at="2026-03-01T00:00:00+00:00"
        )
    )
    lines.add(
        TransactionLine(
            transaction_id=txn.id,
            item_id=item.id,
            direction=Direction.IN,
            quantity=1,
            unit_price_cents=9000,
        )
    )
    txns.delete(txn.id)
    assert lines.list_for_transaction(txn.id) == []


def test_audit_log_add_and_list(conn):
    audit = AuditLogRepository(conn)
    entry = audit.add(
        AuditLogEntry(
            entity="transaction",
            entity_id=1,
            action=AuditAction.CREATE,
            after_json='{"x": 1}',
        )
    )
    assert entry.id is not None
    assert entry.at
    listed = audit.list_for_entity("transaction", 1)
    assert len(listed) == 1
    assert listed[0].action is AuditAction.CREATE
