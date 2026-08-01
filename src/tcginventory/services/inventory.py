"""InventoryService — on-hand positions and per-lot valuation (Spec 02).

Valuation is computed **per lot** so intent can override it: ``ripping`` lots are
valued at $0 (their realizable value is nil), so they always show an unrealized
loss equal to their cost basis (invariant 8). Everything else uses the item's
current ``market_value_cents``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from tcginventory.data.repositories import (
    AuditLogRepository,
    ItemRepository,
    LotRepository,
    PriceSnapshotRepository,
)
from tcginventory.domain.enums import AuditAction, Intent
from tcginventory.domain.models import AuditLogEntry, Item


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
    acquired_at: str  # date this lot was received (ISO date)
    on_hand: int  # lot.remaining_qty
    acquired_qty: int  # lot.quantity
    purchase_price_cents: int  # per-unit price paid
    market_at_purchase_cents: int  # per-unit market value at acquisition
    # Per-unit current market: None if unpriced; $0 for ripping; the manual
    # value for reviewed items; otherwise the latest polled price.
    current_price_cents: int | None
    review: bool  # item marked for manual review (poll-protected, highlighted)
    sold_status: str  # "unsold" | "partial" | "sold" | "Missing"
    # Per-unit price this lot's disposed units actually sold for (proceeds ÷
    # units sold); None if none of the lot has been sold.
    sold_price_cents: int | None = None
    missing: bool = False  # stolen/lost write-down (valued at $0, not on hand)

    @property
    def in_stock(self) -> bool:
        return self.on_hand > 0 and not self.missing

    @property
    def unrealized_cents(self) -> int | None:
        if self.current_price_cents is None:
            return None
        return self.current_price_cents - self.purchase_price_cents


class InventoryService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.items = ItemRepository(conn)
        self.lots = LotRepository(conn)
        self.prices = PriceSnapshotRepository(conn)
        self.audit = AuditLogRepository(conn)

    def _sold_proceeds_by_lot(self) -> dict[int, tuple[int, int]]:
        """``lot_id -> (total proceeds cents, units sold)`` across posted sales,
        from the disposal lines that drew each lot down."""
        rows = self.conn.execute(
            "SELECT lc.lot_id AS lot_id, "
            "SUM(tl.unit_price_cents * lc.quantity) AS proceeds, "
            "SUM(lc.quantity) AS qty "
            "FROM lot_consumption lc "
            "JOIN transaction_line tl ON tl.id = lc.transaction_line_id "
            'JOIN "transaction" t ON t.id = tl.transaction_id '
            "WHERE t.status = 'posted' AND tl.direction = 'out' "
            "GROUP BY lc.lot_id"
        ).fetchall()
        return {r["lot_id"]: (r["proceeds"], r["qty"]) for r in rows}

    def on_hand(self, item_id: int, intent: Intent | None = None) -> int:
        """Units currently on hand for an item (optionally filtered by intent).

        Missing (stolen/lost) lots are excluded — those units are gone."""
        return sum(
            lot.remaining_qty
            for lot in self.lots.list_for_item(item_id)
            if lot.remaining_qty > 0
            and not lot.missing
            and (intent is None or lot.intent is intent)
        )

    def item_position(self, item_id: int, intent: Intent | None = None) -> Position:
        item = self.items.get(item_id)
        current_mv = (item.market_value_cents or 0) if item else 0
        remaining = cost = market = 0
        for lot in self.lots.list_for_item(item_id):
            if lot.remaining_qty <= 0 or lot.missing:
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
        """Items with sellable on-hand units, for the give picker.

        Returns ``(item, available_qty)`` pairs. Only **resale** lots count:
        ripping lots are never sold (invariant 8), and personal lots are held for
        the owner's collection, not for giving away in a deal.
        """
        rows = self.conn.execute(
            "SELECT item_id, SUM(remaining_qty) AS available FROM lot "
            "WHERE remaining_qty > 0 AND missing = 0 AND intent = ? "
            "GROUP BY item_id",
            (Intent.RESALE.value,),
        ).fetchall()
        available_by_item = {r["item_id"]: r["available"] for r in rows}
        return [
            (item, available_by_item[item.id])
            for item in self.items.list_all()
            if available_by_item.get(item.id, 0) > 0
        ]

    def inventory_rows(self) -> list[InventoryRow]:
        """One row per lot (per SKU / acquired copy), including sold-out lots.

        Powers the Inventory grid; per-unit money. A sold-status filter still has
        content because fully-consumed lots remain as rows.
        """
        rows: list[InventoryRow] = []
        latest_prices = self.prices.latest_prices()
        sold_by_lot = self._sold_proceeds_by_lot()
        items_by_id = {item.id: item for item in self.items.list_all()}
        for lot in self.lots.list_all():
            item = items_by_id.get(lot.item_id)
            if item is None:
                continue

            # Per-unit current market: missing (stolen/lost) and ripping are
            # nil; reviewed items use their manual value; others the latest poll.
            if lot.missing or lot.intent is Intent.RIPPING:
                current = 0
            elif item.review:
                current = item.market_value_cents
            else:
                current = latest_prices.get(item.id)

            if lot.missing:
                sold_status = "Missing"
            elif lot.remaining_qty == 0:
                sold_status = "sold"
            elif lot.remaining_qty == lot.quantity:
                sold_status = "unsold"
            else:
                sold_status = "partial"

            proceeds_qty = sold_by_lot.get(lot.id)
            sold_price = (
                round(proceeds_qty[0] / proceeds_qty[1])
                if proceeds_qty and proceeds_qty[1]
                else None
            )

            rows.append(
                InventoryRow(
                    item=item,
                    lot_id=lot.id,
                    sku=lot.sku or "",
                    intent=lot.intent.value,
                    acquired_at=lot.acquired_at,
                    on_hand=lot.remaining_qty,
                    acquired_qty=lot.quantity,
                    purchase_price_cents=lot.unit_cost_cents,
                    market_at_purchase_cents=lot.acquired_market_value_cents,
                    current_price_cents=current,
                    review=item.review,
                    sold_status=sold_status,
                    sold_price_cents=sold_price,
                    missing=lot.missing,
                )
            )
        return rows

    def set_lot_missing(
        self, lot_id: int, missing: bool, note: str | None = None
    ) -> bool:
        """Flag (or unflag) a lot as stolen/lost — a reversible impairment that
        values the lot at $0 while keeping its cost basis. Writes an append-only
        ``audit_log`` entry since it moves money. Caller commits. Returns True if
        the flag changed."""
        lot = self.lots.get(lot_id)
        if lot is None or lot.missing == missing:
            return False
        before = {"missing": lot.missing}
        lot.missing = missing
        self.lots.update(lot)
        self.audit.add(
            AuditLogEntry(
                entity="lot",
                entity_id=lot_id,
                action=AuditAction.EDIT,
                before_json=json.dumps(before),
                after_json=json.dumps({"missing": missing}),
                note=note or ("marked missing" if missing else "unmarked missing"),
            )
        )
        return True
