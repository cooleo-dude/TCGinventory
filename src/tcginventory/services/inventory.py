"""InventoryService — on-hand positions and per-lot valuation (Spec 02).

Valuation is computed **per lot** so intent can override it: ``ripping`` lots are
valued at $0 (their realizable value is nil), so they always show an unrealized
loss equal to their cost basis (invariant 8). Everything else uses the item's
current ``market_value_cents``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from tcginventory.data.repositories import (
    ItemRepository,
    LotRepository,
    PriceSnapshotRepository,
)
from tcginventory.domain.enums import Intent
from tcginventory.domain.models import Item


@dataclass
class Position:
    """An item's aggregate on-hand position."""

    item_id: int
    remaining_qty: int
    cost_basis_cents: int
    market_value_cents: int
    unrealized_pl_cents: int


@dataclass
class InventoryRow:
    """A full inventory-grid row: the item plus its aggregate stats.

    Values are raw (cents / ints / codes); the UI formats them for display.
    """

    item: Item
    on_hand: int
    acquired_qty: int
    cost_basis_cents: int  # what you paid for the on-hand units
    market_at_purchase_cents: int  # entered market value at acquisition (per lot)
    market_value_cents: int  # current market (deferred: last-entered, not live)
    unrealized_pl_cents: int
    avg_cost_cents: int
    sold_status: str  # "unsold" | "partial" | "sold"
    intent: str  # an Intent value, "mixed", or "" (no lots)
    # Total current market of on-hand units from the latest poll; None if the
    # item has never been priced (ripping units count as $0).
    current_value_cents: int | None = None
    skus: tuple[str, ...] = ()  # the item's lot SKUs (for lookup/search)

    @property
    def in_stock(self) -> bool:
        return self.on_hand > 0


class InventoryService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.items = ItemRepository(conn)
        self.lots = LotRepository(conn)
        self.prices = PriceSnapshotRepository(conn)

    def on_hand(self, item_id: int, intent: Intent | None = None) -> int:
        """Units currently on hand for an item (optionally filtered by intent)."""
        return sum(
            lot.remaining_qty
            for lot in self.lots.list_for_item(item_id)
            if lot.remaining_qty > 0 and (intent is None or lot.intent is intent)
        )

    def item_position(self, item_id: int, intent: Intent | None = None) -> Position:
        item = self.items.get(item_id)
        current_mv = (item.market_value_cents or 0) if item else 0
        remaining = cost = market = 0
        for lot in self.lots.list_for_item(item_id):
            if lot.remaining_qty <= 0:
                continue
            if intent is not None and lot.intent is not intent:
                continue
            remaining += lot.remaining_qty
            cost += lot.remaining_qty * lot.unit_cost_cents
            if lot.intent is not Intent.RIPPING:
                market += lot.remaining_qty * current_mv
        return Position(
            item_id=item_id,
            remaining_qty=remaining,
            cost_basis_cents=cost,
            market_value_cents=market,
            unrealized_pl_cents=market - cost,
        )

    def all_positions(self, intent: Intent | None = None) -> list[Position]:
        """Positions for every item that currently has units on hand."""
        positions = []
        for item in self.items.list_all():
            pos = self.item_position(item.id, intent)
            if pos.remaining_qty > 0:
                positions.append(pos)
        return positions

    def sellable_items(self) -> list[tuple[Item, int]]:
        """Items with sellable (non-ripping, on-hand) units, for the give picker.

        Returns ``(item, available_qty)`` pairs — ripping lots are excluded since
        they are never sold (invariant 8).
        """
        out: list[tuple[Item, int]] = []
        for item in self.items.list_all():
            available = sum(
                lot.remaining_qty
                for lot in self.lots.list_for_item(item.id)
                if lot.remaining_qty > 0 and lot.intent is not Intent.RIPPING
            )
            if available > 0:
                out.append((item, available))
        return out

    def inventory_rows(self) -> list[InventoryRow]:
        """One aggregate row per item that has ever had a lot (incl. sold-out).

        Powers the Inventory grid; includes fully-sold items so a sold-status
        filter has something to show.
        """
        rows: list[InventoryRow] = []
        latest_prices = self.prices.latest_prices()
        for item in self.items.list_all():
            lots = self.lots.list_for_item(item.id)
            if not lots:
                continue
            acquired = sum(lot.quantity for lot in lots)
            on_hand = sum(lot.remaining_qty for lot in lots)
            current_mv = item.market_value_cents or 0
            cost = sum(lot.remaining_qty * lot.unit_cost_cents for lot in lots)
            market_at_purchase = sum(
                lot.remaining_qty * lot.acquired_market_value_cents for lot in lots
            )
            market = sum(
                0 if lot.intent is Intent.RIPPING else lot.remaining_qty * current_mv
                for lot in lots
            )
            avg_cost = cost // on_hand if on_hand else 0

            if on_hand == 0:
                sold_status = "sold"
            elif on_hand == acquired:
                sold_status = "unsold"
            else:
                sold_status = "partial"

            intents = {lot.intent for lot in lots}
            intent = next(iter(intents)).value if len(intents) == 1 else "mixed"
            skus = tuple(sorted({lot.sku for lot in lots if lot.sku}))

            current_unit = latest_prices.get(item.id)
            if current_unit is None:
                current_value = None
            else:
                current_value = sum(
                    0
                    if lot.intent is Intent.RIPPING
                    else lot.remaining_qty * current_unit
                    for lot in lots
                )

            rows.append(
                InventoryRow(
                    item=item,
                    on_hand=on_hand,
                    acquired_qty=acquired,
                    cost_basis_cents=cost,
                    market_at_purchase_cents=market_at_purchase,
                    market_value_cents=market,
                    unrealized_pl_cents=market - cost,
                    avg_cost_cents=avg_cost,
                    sold_status=sold_status,
                    intent=intent,
                    current_value_cents=current_value,
                    skus=skus,
                )
            )
        return rows
