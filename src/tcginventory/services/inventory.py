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
    LotConsumptionRepository,
    LotRepository,
    PriceSnapshotRepository,
)
from tcginventory.domain.enums import AuditAction, Intent
from tcginventory.domain.models import AuditLogEntry, Item, Lot
from tcginventory.services.exceptions import TransactionStateError


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
        self.consumptions = LotConsumptionRepository(conn)
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

    def set_lot_intent(
        self, lot_id: int, intent: Intent, note: str | None = None
    ) -> bool:
        """Change one lot's intent, with an ``audit_log`` row. Caller commits.

        Intent is **per lot** (spec 02) and is a *filter* dimension rather than a
        valuation input — ``reporting`` takes it as an optional narrowing, and
        only ``ripping`` changes a number (to $0). So this moves the lot between
        slices of the Dashboard filter and the per-intent
        ``collection_value_snapshot`` series, and changes no total.

        It is audited for the same reason :meth:`set_lot_missing` is: it changes
        what the lot *means* — personal holding versus resale stock is the
        personal/business split this app exists to keep. The Inventory grid used
        to make this change by calling ``LotRepository.update()`` straight from
        the view, with no audit row at all; both that edit and the Personal
        Collection import now come through here.

        Returns True if the intent actually changed.
        """
        lot = self.lots.get(lot_id)
        if lot is None or lot.intent is intent:
            return False
        before = {"intent": lot.intent.value}
        lot.intent = intent
        self.lots.update(lot)
        self.audit.add(
            AuditLogEntry(
                entity="lot",
                entity_id=lot_id,
                action=AuditAction.EDIT,
                before_json=json.dumps(before),
                after_json=json.dumps({"intent": intent.value}),
                note=note or f"intent -> {intent.value}",
            )
        )
        return True

    def split_lot_for_intent(
        self, lot_id: int, quantity: int, intent: Intent, note: str | None = None
    ) -> Lot | None:
        """Move ``quantity`` units of a lot onto a new lot with a new intent.

        "Keep one of these three for myself." The Personal Collection import
        needs exactly this: a resale lot of 3 becomes 2 resale + 1 personal,
        rather than flipping all three onto the personal side.

        Note this is **not** :meth:`TransactionService.split_lot_to_new_item`,
        which moves a whole lot onto a *different item* and keeps its quantity
        and intent. Here the item is the same and the quantity divides.

        Migration 0016 already established the shape: one acquisition line
        legitimately carries several lots **sharing one SKU**, because a purchase
        whose cost does not divide evenly splits into ``(qty - r)`` and ``r``
        lots so the basis reconciles to the cent. The new lot therefore keeps the
        source's ``unit_cost_cents``, ``acquired_market_value_cents``,
        ``acquired_txn_line_id``, SKU and acquisition date — **no money is
        re-divided**, so unlike a cost allocation there is no rounding residual
        to place: ``2 x unit + 1 x unit == 3 x unit`` exactly.

        Restricted to a **fully unsold** lot, exactly as ``split_lot_to_new_item``
        is: a partially sold lot has ``lot_consumption`` rows pointing at it, and
        re-pointing those is the money path and belongs behind its own design.

        Returns the new lot, or ``None`` when no split was needed (the whole lot
        moved, so its intent was simply changed). Caller commits.
        """
        lot = self.lots.get(lot_id)
        if lot is None:
            raise TransactionStateError(f"no lot {lot_id}")
        if quantity < 1 or quantity > lot.quantity:
            raise TransactionStateError(
                f"cannot move {quantity} of lot {lot_id}'s {lot.quantity} units"
            )
        if lot.remaining_qty != lot.quantity or self.consumptions.list_for_lot(lot.id):
            raise TransactionStateError(
                f"lot {lot_id} has sold units; only a fully unsold lot can be "
                "split (its disposals reference the quantity it is leaving)"
            )
        if quantity == lot.quantity:
            self.set_lot_intent(lot_id, intent, note=note)
            return None

        before = {"quantity": lot.quantity, "intent": lot.intent.value}
        remainder = lot.quantity - quantity
        lot.quantity = remainder
        lot.remaining_qty = remainder
        self.lots.update(lot)
        moved = self.lots.add(
            Lot(
                item_id=lot.item_id,
                acquired_at=lot.acquired_at,
                quantity=quantity,
                remaining_qty=quantity,
                unit_cost_cents=lot.unit_cost_cents,
                acquired_market_value_cents=lot.acquired_market_value_cents,
                intent=intent,
                sku=lot.sku,  # same acquisition — 0016 permits the shared SKU
                acquired_txn_line_id=lot.acquired_txn_line_id,
                source=lot.source,
                notes=lot.notes,
            )
        )
        self.audit.add(
            AuditLogEntry(
                entity="lot",
                entity_id=lot_id,
                action=AuditAction.EDIT,
                before_json=json.dumps(before),
                after_json=json.dumps(
                    {
                        "quantity": remainder,
                        "intent": lot.intent.value,
                        "split_to_lot_id": moved.id,
                        "split_quantity": quantity,
                        "split_intent": intent.value,
                    }
                ),
                note=note or f"split {quantity} unit(s) to {intent.value}",
            )
        )
        return moved

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
