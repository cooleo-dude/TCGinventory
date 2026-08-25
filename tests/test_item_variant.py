"""Variant is part of a card's identity (ticket 52).

Two printings of one collector number — a base Mew ex 205/165 and the "151 Metal
Card" one — used to agree on every identity column, so they merged into a single
item carrying both copies' lots. Editing item-level Notes on one Inventory row
then changed all of them, and acquiring a metal copy overwrote the base card's
market value.
"""

from __future__ import annotations

import pytest

from conftest import buy
from tcginventory.data.repositories import ItemRepository, LotRepository
from tcginventory.domain.enums import Category, Condition, Language
from tcginventory.domain.models import Item
from tcginventory.services.exceptions import TransactionStateError
from tcginventory.services.inventory import InventoryService
from tcginventory.services.sku import make_base
from tcginventory.services.transactions import (
    OutgoingCard,
    TransactionRequest,
    TransactionService,
)


def _mew(variant: str | None = None) -> Item:
    return Item(
        name="Mew ex",
        category=Category.SINGLE,
        set_name="151",
        set_code="MEW",
        card_number="205/165",
        condition=Condition.NM,
        language=Language.EN,
        variant=variant,
    )


# -- identity ---------------------------------------------------------------


def test_variant_separates_two_printings_of_one_number(conn):
    repo = ItemRepository(conn)
    repo.add(_mew())
    repo.add(_mew("151 Metal Card"))
    conn.commit()

    assert repo.count() == 2
    assert repo.find_by_identity(_mew()).variant is None
    assert repo.find_by_identity(_mew("151 Metal Card")).variant == "151 Metal Card"


def test_items_without_a_variant_still_match_each_other(conn):
    """Migration 0022 leaves every existing row NULL — nothing may fragment."""
    repo = ItemRepository(conn)
    first = repo.add(_mew())
    conn.commit()

    assert repo.find_by_identity(_mew()).id == first.id


def test_variant_round_trips_through_update(conn):
    repo = ItemRepository(conn)
    item = repo.add(_mew())
    item.variant = "Jumbo"
    repo.update(item)
    conn.commit()

    assert repo.get(item.id).variant == "Jumbo"


def test_buying_a_variant_does_not_reprice_the_base_card(conn):
    """The sharp edge: _match_or_create_item refreshes the matched item's value.

    While both printings were one item, acquiring the cheaper metal copy
    overwrote the base card's market value.
    """
    svc = TransactionService(conn)
    buy(svc, "Mew ex", 3235, 3235, "2026-08-01", set_name="151", card_number="205/165")
    buy(
        svc,
        "Mew ex",
        2557,
        2557,
        "2026-08-02",
        set_name="151",
        card_number="205/165",
        variant="151 Metal Card",
    )

    by_variant = {i.variant: i for i in ItemRepository(conn).list_all()}
    assert by_variant[None].market_value_cents == 3235
    assert by_variant["151 Metal Card"].market_value_cents == 2557


# -- SKU --------------------------------------------------------------------


def test_sku_base_is_unchanged_for_a_base_print():
    """Every SKU already issued must stay byte-identical."""
    assert make_base(_mew()) == "MEW-MEW-205-NM-EN"


def test_sku_base_distinguishes_a_variant():
    assert make_base(_mew("151 Metal Card")) == "MEW-MEW-205-NM-EN-151M"


# -- the split --------------------------------------------------------------


def _three_mew(conn) -> tuple[TransactionService, list]:
    svc = TransactionService(conn)
    for day in ("2026-06-29", "2026-08-03", "2026-08-24"):
        buy(svc, "Mew ex", 2000, 2000, day, set_name="151", card_number="205/165")
    return svc, LotRepository(conn).list_for_item(1)


def test_split_moves_one_copy_onto_its_own_item(conn):
    """The reported case: three lots under one item, two of them metal."""
    svc, lots = _three_mew(conn)
    assert ItemRepository(conn).count() == 1

    metal = svc.split_lot_to_new_item(lots[1].id, variant="151 Metal Card")

    assert ItemRepository(conn).count() == 2
    assert metal.variant == "151 Metal Card"
    assert InventoryService(conn).on_hand(1) == 2
    assert InventoryService(conn).on_hand(metal.id) == 1


def test_split_lands_a_second_copy_beside_the_first(conn):
    """Separating the other metal copy must not mint a third item."""
    svc, lots = _three_mew(conn)

    first = svc.split_lot_to_new_item(lots[1].id, variant="151 Metal Card")
    second = svc.split_lot_to_new_item(lots[2].id, variant="151 Metal Card")

    assert first.id == second.id
    assert ItemRepository(conn).count() == 2
    assert InventoryService(conn).on_hand(1) == 1
    assert InventoryService(conn).on_hand(first.id) == 2


def test_split_preserves_the_money_and_moves_the_acquiring_line(conn):
    """Nothing about the basis moves — only which item the copy hangs from.

    The acquiring line moves with the lot, or History and per-item realized P/L
    would disagree with it.
    """
    svc, lots = _three_mew(conn)
    lot = lots[1]
    basis, qty, acquired = lot.unit_cost_cents, lot.quantity, lot.acquired_at

    metal = svc.split_lot_to_new_item(lot.id, variant="151 Metal Card")

    moved = LotRepository(conn).get(lot.id)
    assert (moved.unit_cost_cents, moved.quantity, moved.acquired_at) == (
        basis,
        qty,
        acquired,
    )
    line = svc.lines.get(moved.acquired_txn_line_id)
    assert line.item_id == metal.id, "the acquiring line stayed on the old item"
    # The SKU is reissued on the new item's own run.
    assert moved.sku.startswith("MEW-151-205-NA-EN-151M")


def test_split_writes_an_audit_row(conn):
    svc, lots = _three_mew(conn)
    svc.split_lot_to_new_item(lots[1].id, variant="151 Metal Card")

    entries = svc.audit.list_for_entity("lot", lots[1].id)
    assert entries and "separated" in (entries[-1].note or "")


def test_split_refuses_a_partially_sold_lot(conn):
    """A sold copy's out-lines and consumption rows reference the old item;
    re-pointing those is the money path and is deliberately out of scope."""
    svc = TransactionService(conn)
    buy(
        svc,
        "Mew ex",
        2000,
        2000,
        "2026-06-29",
        set_name="151",
        card_number="205/165",
        quantity=2,
    )
    lot = LotRepository(conn).list_for_item(1)[0]
    svc.post(
        TransactionRequest(
            occurred_at="2026-07-01",
            cash_in_cents=2500,
            outgoing=[OutgoingCard(item_id=1, quantity=1)],
        )
    )

    with pytest.raises(TransactionStateError, match="sold units"):
        svc.split_lot_to_new_item(lot.id, variant="151 Metal Card")


def test_split_rejects_a_field_that_is_not_on_item(conn):
    svc, lots = _three_mew(conn)
    with pytest.raises(TransactionStateError, match="not item fields"):
        svc.split_lot_to_new_item(lots[0].id, finish="Metal")


def test_split_refuses_an_unknown_lot(conn):
    svc = TransactionService(conn)
    with pytest.raises(TransactionStateError, match="no lot"):
        svc.split_lot_to_new_item(9999, variant="x")


# -- import / export --------------------------------------------------------


def test_full_dump_round_trip_keeps_the_two_printings_apart(conn, tmp_path):
    """A restore must not re-merge them.

    export_all/import_all replay raw rows column by column, so the new column
    rides along only because it exists on the table — worth pinning, since a
    restore that re-merged would silently undo every split the owner made.
    """
    from tcginventory.services.importexport import export_all, import_all

    repo = ItemRepository(conn)
    repo.add(_mew())
    repo.add(_mew("151 Metal Card"))
    conn.commit()

    export_all(conn, tmp_path)
    import_all(conn, tmp_path)

    variants = sorted((i.variant or "") for i in repo.list_all())
    assert variants == ["", "151 Metal Card"]


def test_item_csv_export_carries_the_variant(conn, tmp_path):
    from tcginventory.services.importexport import export_items_csv

    repo = ItemRepository(conn)
    repo.add(_mew("151 Metal Card"))
    conn.commit()

    text = export_items_csv(conn, tmp_path / "items.csv").read_text(encoding="utf-8")
    assert "variant" in text.splitlines()[0]
    assert "151 Metal Card" in text
