"""InventoryService tests — positions and per-lot valuation (ripping at $0)."""

from __future__ import annotations

from conftest import buy, sell
from tcginventory.domain.enums import Intent


def test_inventory_row_sold_price(svc, inv):
    result = buy(svc, "X", 8000, 10000, "2026-01-01")
    item_id = result.lots[0].item_id
    lot_id = result.lots[0].id

    # Unsold → no sale price yet.
    row = next(r for r in inv.inventory_rows() if r.lot_id == lot_id)
    assert row.sold_status == "unsold"
    assert row.sold_price_cents is None

    # Sell it for $120 → the row records what it sold for (per unit).
    sell(svc, item_id, 1, 12000, "2026-02-01")
    row = next(r for r in inv.inventory_rows() if r.lot_id == lot_id)
    assert row.sold_status == "sold"
    assert row.sold_price_cents == 12000


def test_inventory_row_sold_price_averages_multi_unit_lot(svc, inv):
    # A 2-unit lot sold across two sales at $100 and $140 → average $120/unit.
    result = buy(svc, "Y", 8000, 10000, "2026-01-01", quantity=2)
    item_id = result.lots[0].item_id
    lot_id = result.lots[0].id
    sell(svc, item_id, 1, 10000, "2026-02-01")
    sell(svc, item_id, 1, 14000, "2026-02-05")
    row = next(r for r in inv.inventory_rows() if r.lot_id == lot_id)
    assert row.sold_status == "sold"
    assert row.sold_price_cents == 12000  # (10000 + 14000) / 2


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
    assert row.purchase_price_cents == 8000  # per-unit price you paid
    assert row.market_at_purchase_cents == 10000  # market when entered


def test_inventory_rows_partial_sale(svc, inv):
    p = buy(svc, "Box", 3000, 1000, "2026-01-01", quantity=3)
    sell(svc, p.lots[0].item_id, 1, 1500, "2026-02-01")
    (row,) = inv.inventory_rows()
    assert row.on_hand == 2
    assert row.acquired_qty == 3
    assert row.sold_status == "partial"


# --- sellable_cost_groups: the give picker split by purchase price (71) -------


def test_cost_groups_split_one_item_by_purchase_price(svc, inv):
    # The same card bought twice at different prices: $20 then $50.
    buy(svc, "Charizard", 2000, 6000, "2026-01-01")
    buy(svc, "Charizard", 5000, 6000, "2026-02-01")

    groups = inv.sellable_cost_groups()
    assert [(g.item.name, g.unit_cost_cents, g.available) for g in groups] == [
        ("Charizard", 2000, 1),
        ("Charizard", 5000, 1),
    ]
    # sellable_items still sums them into one entry — the two views disagree
    # deliberately, and that is the point of the split.
    assert [(i.name, avail) for i, avail in inv.sellable_items()] == [("Charizard", 2)]


def test_cost_groups_merge_lots_bought_at_the_same_price(svc, inv):
    # Two separate purchases at the same price are one pickable group: the user
    # cannot tell them apart, so offering two entries would be a false choice.
    buy(svc, "Pikachu", 2000, 1500, "2026-01-01", quantity=2)  # $10 a unit
    buy(svc, "Pikachu", 1000, 1500, "2026-03-01")  # the same $10 a unit

    (group,) = inv.sellable_cost_groups()
    assert group.unit_cost_cents == 1000
    assert group.available == 3


def test_cost_groups_ordered_oldest_acquisition_first(svc, inv):
    # Bought expensive first, cheap second → the expensive group leads, because
    # that is the one an unrestricted FIFO sale would have consumed.
    buy(svc, "Mewtwo", 9000, 9000, "2026-01-01")
    buy(svc, "Mewtwo", 1000, 9000, "2026-06-01")

    assert [g.unit_cost_cents for g in inv.sellable_cost_groups()] == [9000, 1000]


def test_cost_groups_exclude_ripping_personal_and_missing(svc, inv, conn):
    buy(svc, "Stock", 1000, 1000, "2026-01-01")
    buy(svc, "Booster Box", 12000, 12000, "2026-01-02", intent=Intent.RIPPING)
    buy(svc, "Keeper", 2000, 2000, "2026-01-03", intent=Intent.PERSONAL)
    stolen = buy(svc, "Stolen", 3000, 3000, "2026-01-04")
    inv.set_lot_missing(stolen.lots[0].id, True)
    conn.commit()

    assert [g.item.name for g in inv.sellable_cost_groups()] == ["Stock"]


def test_cost_groups_drop_a_group_once_it_is_sold_out(svc, inv):
    cheap = buy(svc, "Snorlax", 1000, 4000, "2026-01-01")
    buy(svc, "Snorlax", 3000, 4000, "2026-02-01")
    item_id = cheap.lots[0].item_id

    # FIFO takes the $10 lot, so the $10 group empties and stops being offered.
    sell(svc, item_id, 1, 4000, "2026-03-01")
    assert [g.unit_cost_cents for g in inv.sellable_cost_groups()] == [3000]
