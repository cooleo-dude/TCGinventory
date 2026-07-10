"""InventoryService — on-hand positions and per-lot valuation (Spec 02).

Valuation is computed **per lot** so intent can override it: ``ripping`` lots are
valued at $0 (their realizable value is nil), so they always show an unrealized
loss equal to their cost basis (invariant 8). Everything else uses the item's
current ``market_value_cents``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from tcginventory.data.repositories import ItemRepository, LotRepository
from tcginventory.domain.enums import Intent


@dataclass
class Position:
    """An item's aggregate on-hand position."""

    item_id: int
    remaining_qty: int
    cost_basis_cents: int
    market_value_cents: int
    unrealized_pl_cents: int


class InventoryService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.items = ItemRepository(conn)
        self.lots = LotRepository(conn)

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
