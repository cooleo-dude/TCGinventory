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
    """One inventory-grid row per **lot** (per SKU / acquired copy).

    Each acquired card is its own row, keyed by its SKU. All money values are
    **per unit**; the UI formats them for display.
    """

    item: Item
    lot_id: int
    sku: str
    intent: str  # this lot's intent value
    on_hand: int  # lot.remaining_qty
    acquired_qty: int  # lot.quantity
    purchase_price_cents: int  # per-unit price paid
    market_at_purchase_cents: int  # per-unit market value at acquisition
    # Per-unit current market: None if unpriced; $0 for ripping; the manual
    # value for reviewed items; otherwise the latest polled price.
    current_price_cents: int | None
    review: bool  # item marked for manual review (poll-protected, highlighted)
    sold_status: str  # "unsold" | "partial" | "sold"

    @property
    def in_stock(self) -> bool:
        return self.on_hand > 0

    @property
    def unrealized_cents(self) -> int | None:
        if self.current_price_cents is None:
            return None
        return self.current_price_cents - self.purchase_price_cents


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
        """One row per lot (per SKU / acquired copy), including sold-out lots.

        Powers the Inventory grid; per-unit money. A sold-status filter still has
        content because fully-consumed lots remain as rows.
        """
        rows: list[InventoryRow] = []
        latest_prices = self.prices.latest_prices()
        items_by_id = {item.id: item for item in self.items.list_all()}
        for lot in self.lots.list_all():
            item = items_by_id.get(lot.item_id)
            if item is None:
                continue

            # Per-unit current market: ripping is nil; reviewed items use their
            # manual value; others use the latest polled price.
            if lot.intent is Intent.RIPPING:
                current = 0
            elif item.review:
                current = item.market_value_cents
            else:
                current = latest_prices.get(item.id)

            if lot.remaining_qty == 0:
                sold_status = "sold"
            elif lot.remaining_qty == lot.quantity:
                sold_status = "unsold"
            else:
                sold_status = "partial"

            rows.append(
                InventoryRow(
                    item=item,
                    lot_id=lot.id,
                    sku=lot.sku or "",
                    intent=lot.intent.value,
                    on_hand=lot.remaining_qty,
                    acquired_qty=lot.quantity,
                    purchase_price_cents=lot.unit_cost_cents,
                    market_at_purchase_cents=lot.acquired_market_value_cents,
                    current_price_cents=current,
                    review=item.review,
                    sold_status=sold_status,
                )
            )
        return rows
