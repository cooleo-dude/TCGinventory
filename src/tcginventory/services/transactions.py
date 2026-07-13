"""TransactionService — the one unified posting algorithm (Spec 02).

Purchases, sales, and trades are posted by the *same* procedure; ``type`` is
inferred from which sides carry cards. Void and Edit (void + re-post) reverse
effects atomically and write to the append-only ``audit_log``. Every public
method runs its writes inside a single DB transaction (commit on success,
rollback on any error), so inventory and the ledger can never diverge.

Qt-free and SQL-free except through repositories.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import (
    AuditLogRepository,
    ItemRepository,
    LotConsumptionRepository,
    LotRepository,
    PriceSnapshotRepository,
    TransactionLineRepository,
    TransactionRepository,
)
from tcginventory.domain.enums import (
    AuditAction,
    Direction,
    Intent,
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
from tcginventory.services.allocation import AllocationInput, allocate_cost
from tcginventory.services.exceptions import (
    EmptyTransactionError,
    InsufficientQuantityError,
    InvalidTransactionError,
    RippingNotSellableError,
    TransactionStateError,
)
from tcginventory.services.review import (  # noqa: F401 - REVIEW_NOTE re-exported
    REVIEW_NOTE,
    with_review_note,
)
from tcginventory.services.sku import generate_sku

# ---------------------------------------------------------------------------
# Request / result DTOs
# ---------------------------------------------------------------------------


@dataclass
class OutgoingCard:
    """A card you give up. Its value is derived from the deal (no manual entry):
    the cards received + cash form the proceeds; see ``_deal_values``."""

    item_id: int
    quantity: int
    notes: str | None = None


@dataclass
class IncomingCard:
    """A card you receive. ``item`` carries identity/attributes for match-or-create.

    ``market_value_cents`` (per unit, required, > 0) is the acquisition snapshot
    used to allocate cost and to refresh the item's current market value.
    """

    item: Item
    quantity: int
    market_value_cents: int
    intent: Intent
    review: bool = False  # mark the item for manual price review
    source: str | None = None
    notes: str | None = None


@dataclass
class TransactionRequest:
    occurred_at: str
    outgoing: list[OutgoingCard] = field(default_factory=list)
    incoming: list[IncomingCard] = field(default_factory=list)
    cash_in_cents: int = 0  # cash you received
    cash_out_cents: int = 0  # cash you paid
    counterparty: str | None = None
    reference: str | None = None
    notes: str | None = None


@dataclass
class PostResult:
    transaction: Transaction
    net_consideration_in_cents: int
    realized_pl_cents: int  # total realized on the outgoing side
    lines: list[TransactionLine]
    lots: list[Lot]  # incoming lots created


@dataclass
class TransactionPreview:
    """Read-only preview of what a request would post — powers the live footer.

    Computed without writing anything; ``feasible`` is False (with per-item
    ``shortfalls``) when an outgoing line would oversell.
    """

    inferred_type: TransactionType | None
    proceeds_cents: int  # Σ agreed value of cards out
    consideration_given_cents: int  # cash_out + proceeds
    net_consideration_in_cents: int  # consideration_given − cash_in
    value_received_cents: int  # Σ market value of cards in
    net_cash_cents: int  # cash_in − cash_out
    realized_pl_cents: int  # estimated margin on cards out (FIFO)
    feasible: bool
    shortfalls: dict[int, int]  # item_id → missing units


def _as_date(iso: str) -> str:
    """Reduce an ISO date/datetime to its date part (drives FIFO order / as-of)."""
    return iso[:10] if len(iso) >= 10 else iso


def _allocate_amount(total: int, weights: list[int]) -> list[int]:
    """Split ``total`` (which may be negative) across lines by ``weights``.

    Largest-remainder so the shares sum to ``total`` exactly; falls back to an
    even split when all weights are zero. Used to spread a transaction's proceeds
    across the cards given up.
    """
    n = len(weights)
    if n == 0:
        return []
    if sum(weights) <= 0:
        weights = [1] * n
    weight_sum = sum(weights)
    shares = [total * w // weight_sum for w in weights]  # floor (handles negatives)
    residual = total - sum(shares)  # always >= 0 since floor underestimates
    order = sorted(range(n), key=lambda i: (weights[i], -i), reverse=True)
    for k in range(residual):
        shares[order[k % n]] += 1
    return shares


@dataclass
class DealValues:
    """Values derived from a whole transaction (no per-card agreed value)."""

    given_value: int  # market value of the cards given up
    value_received: int  # market value of the cards received
    net_consideration_in: int  # → incoming cost basis
    net_consideration_out: int  # → proceeds on the cards given up


class TransactionService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.items = ItemRepository(conn)
        self.lots = LotRepository(conn)
        self.txns = TransactionRepository(conn)
        self.lines = TransactionLineRepository(conn)
        self.consumptions = LotConsumptionRepository(conn)
        self.prices = PriceSnapshotRepository(conn)
        self.audit = AuditLogRepository(conn)

    # -- public API ---------------------------------------------------------

    def post(self, request: TransactionRequest) -> PostResult:
        """Post a purchase / sale / trade (type inferred). Atomic."""
        self._validate(request)
        try:
            result = self._post(request)
            self._write_audit(
                result.transaction.id,
                AuditAction.CREATE,
                after=self._snapshot(result.transaction.id),
            )
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def void(self, transaction_id: int) -> Transaction:
        """Soft-void a transaction, reversing all its effects. Atomic."""
        txn = self.txns.get(transaction_id)
        if txn is None:
            raise TransactionStateError(f"no transaction {transaction_id}")
        if txn.status is TransactionStatus.VOIDED:
            raise TransactionStateError(f"transaction {transaction_id} already voided")
        try:
            before = self._snapshot(transaction_id)
            voided = self._void(txn)
            self._write_audit(transaction_id, AuditAction.VOID, before=before)
            self.conn.commit()
            return voided
        except Exception:
            self.conn.rollback()
            raise

    def delete_inventory_entry(self, lot_id: int) -> None:
        """Permanently delete one inventory entry — a lot — for cleanup.

        This is an unconditional **hard delete** (not a void): the lot is gone,
        for erasing mistaken/junk entries. It always succeeds (short of a
        missing lot); the only work is keeping the database internally
        consistent:

          * disposals that drew *from* this lot have their consumption records
            removed (the sale itself is left intact, it simply loses its
            cost-basis link);
          * on a plain cash-out purchase, recorded cash-out is reduced by this
            lot's basis so the remaining lines still reconcile;
          * the acquiring line is removed once it has no lots left, and its
            transaction once it has no lines left;
          * the item is removed if nothing references it anymore.

        There are deliberately no business guardrails — the owner is trusted to
        clean up their own data. The deletion is recorded in ``audit_log``.
        """
        lot = self.lots.get(lot_id)
        if lot is None:
            raise TransactionStateError(f"no lot {lot_id}")

        item_id = lot.item_id
        line_id = lot.acquired_txn_line_id
        basis = lot.quantity * lot.unit_cost_cents
        before = json.dumps(
            {
                "lot_id": lot.id,
                "item_id": item_id,
                "sku": lot.sku,
                "quantity": lot.quantity,
                "unit_cost_cents": lot.unit_cost_cents,
                "acquired_txn_line_id": line_id,
            }
        )
        try:
            # 1. Drop disposals that drew from this lot (keeps FKs valid).
            for cons in self.consumptions.list_for_lot(lot.id):
                self.consumptions.delete(cons.id)

            # 2. Delete the lot itself.
            self.lots.delete(lot.id)

            # 3. Tidy the acquiring purchase.
            if line_id is not None:
                line = self.lines.get(line_id)
                if line is not None:
                    txn = self.txns.get(line.transaction_id)
                    other_lines = self.lines.list_for_transaction(txn.id)
                    pure_cash_out = txn.cash_delta_cents < 0 and not any(
                        ln.direction is Direction.OUT for ln in other_lines
                    )
                    if pure_cash_out:
                        txn.cash_delta_cents = min(0, txn.cash_delta_cents + basis)
                        self.txns.update(txn)
                    # Remove the line once it has no lots left, and its
                    # transaction once it has no lines left.
                    if not self.lots.list_for_acquired_line(line_id):
                        self.lines.delete(line_id)
                        if not self.lines.list_for_transaction(txn.id):
                            self.txns.delete(txn.id)

            # 4. Orphan cleanup: drop the item if nothing references it anymore.
            #    Its price snapshots must go first — they FK-reference the item.
            if not self.lots.list_for_item(item_id) and not self.lines.list_for_item(
                item_id
            ):
                self.prices.delete_for_item(item_id)
                self.items.delete(item_id)

            self.audit.add(
                AuditLogEntry(
                    entity="lot",
                    entity_id=lot_id,
                    action=AuditAction.DELETE,
                    before_json=before,
                    note="inventory entry permanently deleted",
                )
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def edit(self, transaction_id: int, request: TransactionRequest) -> PostResult:
        """Edit = void the original + re-post the new data, in one transaction."""
        old = self.txns.get(transaction_id)
        if old is None:
            raise TransactionStateError(f"no transaction {transaction_id}")
        if old.status is TransactionStatus.VOIDED:
            raise TransactionStateError(f"transaction {transaction_id} is voided")
        self._validate(request)
        try:
            before = self._snapshot(transaction_id)
            self._void(old)
            result = self._post(request)
            self._write_audit(
                result.transaction.id,
                AuditAction.EDIT,
                before=before,
                after=self._snapshot(result.transaction.id),
                note=f"edit of transaction {transaction_id}",
            )
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def _outgoing_market(self, request: TransactionRequest) -> int:
        """Total current market value of the cards being given up."""
        total = 0
        for out in request.outgoing:
            item = self.items.get(out.item_id)
            total += ((item.market_value_cents or 0) if item else 0) * out.quantity
        return total

    def _deal_values(self, request: TransactionRequest) -> DealValues:
        """Derive the transaction's values from the cards + cash involved.

        Cards given are valued at their current market value; cards received at
        their entered market value. Incoming cost basis comes from
        ``cash_out + given − cash_in``; proceeds on the cards given come from
        ``cash_in + received − cash_out`` (so a pure sale's proceeds = the cash).
        """
        given = self._outgoing_market(request)
        received = sum(i.market_value_cents * i.quantity for i in request.incoming)
        return DealValues(
            given_value=given,
            value_received=received,
            net_consideration_in=request.cash_out_cents + given - request.cash_in_cents,
            net_consideration_out=request.cash_in_cents
            + received
            - request.cash_out_cents,
        )

    def preview(self, request: TransactionRequest) -> TransactionPreview:
        """Compute the live summary for a request without writing anything."""
        has_out, has_in = bool(request.outgoing), bool(request.incoming)
        if has_out and has_in:
            inferred = TransactionType.TRADE
        elif has_out:
            inferred = TransactionType.SALE
        elif has_in:
            inferred = TransactionType.PURCHASE
        else:
            inferred = None

        deal = self._deal_values(request)
        consideration_given = request.cash_out_cents + deal.given_value
        net_cash = request.cash_in_cents - request.cash_out_cents

        # Simulate FIFO draw-down against a local copy of open non-ripping lots.
        queues: dict[int, list[list[int]]] = {}

        def queue_for(item_id: int) -> list[list[int]]:
            if item_id not in queues:
                queues[item_id] = [
                    [lot.remaining_qty, lot.unit_cost_cents]
                    for lot in self.lots.open_lots_for_item(item_id)
                    if lot.intent is not Intent.RIPPING
                ]
            return queues[item_id]

        feasible = True
        shortfalls: dict[int, int] = {}
        costs: list[int] = []
        weights: list[int] = []
        for out in request.outgoing:
            remaining = out.quantity
            drawn = 0
            cost = 0
            for slot in queue_for(out.item_id):
                if remaining == 0:
                    break
                take = min(slot[0], remaining)
                slot[0] -= take
                cost += take * slot[1]
                drawn += take
                remaining -= take
            if remaining > 0:
                feasible = False
                shortfalls[out.item_id] = shortfalls.get(out.item_id, 0) + remaining
            item = self.items.get(out.item_id)
            mv = (item.market_value_cents or 0) if item else 0
            costs.append(cost)
            weights.append(mv * drawn)

        # Proceeds on the cards given are the deal's net_consideration_out,
        # split across them by market weight; realized P/L = proceeds − cost.
        shares = _allocate_amount(deal.net_consideration_out, weights)
        realized = sum(s - c for s, c in zip(shares, costs, strict=True))
        proceeds = deal.net_consideration_out if request.outgoing else 0

        return TransactionPreview(
            inferred_type=inferred,
            proceeds_cents=proceeds,
            consideration_given_cents=consideration_given,
            net_consideration_in_cents=deal.net_consideration_in,
            value_received_cents=deal.value_received,
            net_cash_cents=net_cash,
            realized_pl_cents=realized,
            feasible=feasible,
            shortfalls=shortfalls,
        )

    def adjust_found(
        self,
        item_id: int,
        quantity: int,
        occurred_at: str,
        unit_cost_cents: int = 0,
        intent: Intent = Intent.PERSONAL,
        notes: str | None = None,
    ) -> Transaction:
        """Adjustment-in: record found/miscounted inventory as a new lot. Atomic."""
        if quantity < 1:
            raise InvalidTransactionError("adjustment quantity must be >= 1")
        if unit_cost_cents < 0:
            raise InvalidTransactionError("unit cost cannot be negative")
        try:
            txn = self.txns.add(
                Transaction(
                    type=TransactionType.ADJUSTMENT,
                    occurred_at=occurred_at,
                    notes=notes,
                )
            )
            line = self.lines.add(
                TransactionLine(
                    transaction_id=txn.id,
                    item_id=item_id,
                    direction=Direction.IN,
                    quantity=quantity,
                    unit_price_cents=unit_cost_cents,
                    notes=notes,
                )
            )
            item = self.items.get(item_id)
            self.lots.add(
                Lot(
                    item_id=item_id,
                    acquired_txn_line_id=line.id,
                    acquired_at=_as_date(occurred_at),
                    quantity=quantity,
                    remaining_qty=quantity,
                    unit_cost_cents=unit_cost_cents,
                    acquired_market_value_cents=unit_cost_cents,
                    intent=intent,
                    sku=generate_sku(self.conn, item) if item else None,
                    source="adjustment",
                    notes=notes,
                )
            )
            self._write_audit(txn.id, AuditAction.CREATE, after=self._snapshot(txn.id))
            self.conn.commit()
            return txn
        except Exception:
            self.conn.rollback()
            raise

    def adjust_writeoff(
        self,
        item_id: int,
        quantity: int,
        occurred_at: str,
        notes: str | None = None,
    ) -> Transaction:
        """Adjustment-out: write off inventory (damage/shrinkage) at $0 proceeds.

        Realized P/L is the loss (``−consumed cost``). Atomic.
        """
        if quantity < 1:
            raise InvalidTransactionError("adjustment quantity must be >= 1")
        try:
            txn = self.txns.add(
                Transaction(
                    type=TransactionType.ADJUSTMENT,
                    occurred_at=occurred_at,
                    notes=notes,
                )
            )
            line = self.lines.add(
                TransactionLine(
                    transaction_id=txn.id,
                    item_id=item_id,
                    direction=Direction.OUT,
                    quantity=quantity,
                    unit_price_cents=0,
                    notes=notes,
                )
            )
            consumed_cost = self._consume_fifo(item_id, quantity, line.id)
            line.realized_pl_cents = -consumed_cost
            self.lines.update(line)
            self._write_audit(txn.id, AuditAction.CREATE, after=self._snapshot(txn.id))
            self.conn.commit()
            return txn
        except Exception:
            self.conn.rollback()
            raise

    # -- internal (no commit) ----------------------------------------------

    def _validate(self, request: TransactionRequest) -> None:
        if not request.outgoing and not request.incoming:
            raise EmptyTransactionError("a transaction needs at least one card")
        if request.cash_in_cents < 0 or request.cash_out_cents < 0:
            raise InvalidTransactionError("cash amounts cannot be negative")
        for out in request.outgoing:
            if out.quantity < 1:
                raise InvalidTransactionError("outgoing quantity must be >= 1")
        for inc in request.incoming:
            if inc.quantity < 1:
                raise InvalidTransactionError("incoming quantity must be >= 1")
            if inc.market_value_cents <= 0:
                raise InvalidTransactionError(
                    "every incoming card needs a positive market value"
                )

    @staticmethod
    def _infer_type(request: TransactionRequest) -> TransactionType:
        has_out = bool(request.outgoing)
        has_in = bool(request.incoming)
        if has_out and has_in:
            return TransactionType.TRADE
        if has_out:
            return TransactionType.SALE
        return TransactionType.PURCHASE  # has_in only (empty handled by _validate)

    def _post(self, request: TransactionRequest) -> PostResult:
        txn_type = self._infer_type(request)
        txn = self.txns.add(
            Transaction(
                type=txn_type,
                occurred_at=request.occurred_at,
                counterparty=request.counterparty,
                reference=request.reference,
                cash_delta_cents=request.cash_in_cents - request.cash_out_cents,
                status=TransactionStatus.POSTED,
                notes=request.notes,
            )
        )

        lines: list[TransactionLine] = []
        total_realized = 0
        deal = self._deal_values(request)

        # Outgoing side: FIFO draw-down; proceeds come from the deal (cash you
        # received + market value of cards you got − cash you paid), split across
        # the cards given by their market weight. unit_price = per-unit proceeds.
        out_infos: list[tuple[TransactionLine, int, int, int]] = []
        for out in request.outgoing:
            line = self.lines.add(
                TransactionLine(
                    transaction_id=txn.id,
                    item_id=out.item_id,
                    direction=Direction.OUT,
                    quantity=out.quantity,
                    unit_price_cents=0,
                    notes=out.notes,
                )
            )
            consumed_cost = self._consume_fifo(out.item_id, out.quantity, line.id)
            item = self.items.get(out.item_id)
            mv = (item.market_value_cents or 0) if item else 0
            out_infos.append((line, out.quantity, consumed_cost, mv * out.quantity))
            lines.append(line)

        shares = _allocate_amount(
            deal.net_consideration_out, [w for *_, w in out_infos]
        )
        for (line, qty, cost, _), share in zip(out_infos, shares, strict=True):
            line.unit_price_cents = share // qty if qty else 0
            line.realized_pl_cents = line.unit_price_cents * qty - cost
            self.lines.update(line)
            total_realized += line.realized_pl_cents

        net_consideration_in = deal.net_consideration_in

        # Incoming side: match-or-create items, allocate cost, create lots.
        created_lots: list[Lot] = []
        if request.incoming:
            allocations = allocate_cost(
                net_consideration_in,
                [
                    AllocationInput(inc.market_value_cents, inc.quantity)
                    for inc in request.incoming
                ],
            )
            occurred_date = _as_date(request.occurred_at)
            for inc, alloc in zip(request.incoming, allocations, strict=True):
                item = self._match_or_create_item(inc, occurred_date)
                base_unit_cost = alloc.total_cents // inc.quantity
                line = self.lines.add(
                    TransactionLine(
                        transaction_id=txn.id,
                        item_id=item.id,
                        direction=Direction.IN,
                        quantity=inc.quantity,
                        unit_price_cents=base_unit_cost,
                        notes=inc.notes,
                    )
                )
                lines.append(line)
                # One SKU per acquired copy (line); shared by any split lots.
                sku = generate_sku(self.conn, item)
                for lot_cost in alloc.lots:
                    lot = self.lots.add(
                        Lot(
                            item_id=item.id,
                            acquired_txn_line_id=line.id,
                            acquired_at=occurred_date,
                            quantity=lot_cost.quantity,
                            remaining_qty=lot_cost.quantity,
                            unit_cost_cents=lot_cost.unit_cost_cents,
                            acquired_market_value_cents=inc.market_value_cents,
                            intent=inc.intent,
                            sku=sku,
                            source=inc.source,
                            notes=inc.notes,
                        )
                    )
                    created_lots.append(lot)

        return PostResult(
            transaction=txn,
            net_consideration_in_cents=net_consideration_in,
            realized_pl_cents=total_realized,
            lines=lines,
            lots=created_lots,
        )

    def _consume_fifo(self, item_id: int, quantity: int, line_id: int) -> int:
        """Consume ``quantity`` units FIFO from non-ripping open lots.

        Records ``lot_consumption`` rows, decrements ``remaining_qty``, and
        returns the total consumed cost in cents. Raises before any write if
        there is not enough sellable inventory.
        """
        open_lots = self.lots.open_lots_for_item(item_id)
        sellable = [lot for lot in open_lots if lot.intent is not Intent.RIPPING]
        available = sum(lot.remaining_qty for lot in sellable)
        if available < quantity:
            ripping_qty = sum(
                lot.remaining_qty for lot in open_lots if lot.intent is Intent.RIPPING
            )
            if ripping_qty > 0 and available + ripping_qty >= quantity:
                raise RippingNotSellableError(
                    f"item {item_id}: {quantity} requested but only {available} "
                    f"sellable ({ripping_qty} held as ripping, never sold)"
                )
            raise InsufficientQuantityError(
                f"item {item_id}: {quantity} requested but only {available} on hand"
            )

        remaining = quantity
        consumed_cost = 0
        for lot in sellable:
            if remaining == 0:
                break
            take = min(lot.remaining_qty, remaining)
            self.consumptions.add(
                LotConsumption(
                    transaction_line_id=line_id,
                    lot_id=lot.id,
                    quantity=take,
                    unit_cost_cents=lot.unit_cost_cents,
                )
            )
            lot.remaining_qty -= take
            self.lots.update(lot)
            consumed_cost += take * lot.unit_cost_cents
            remaining -= take
        return consumed_cost

    def _match_or_create_item(self, inc: IncomingCard, occurred_date: str) -> Item:
        """Match the incoming card to an existing item or create one; refresh value."""
        existing = self.items.find_by_identity(inc.item)
        if existing is not None:
            existing.market_value_cents = inc.market_value_cents
            existing.market_value_asof = occurred_date
            existing.review = inc.review
            if inc.review:
                existing.notes = with_review_note(existing.notes)
            if inc.item.tcgplayer_url:
                existing.tcgplayer_url = inc.item.tcgplayer_url
            if inc.item.ebay_url:
                existing.ebay_url = inc.item.ebay_url
            self.items.update(existing)
            return existing
        # Create a fresh item from the request, seeded with the entered value.
        new_item = replace(
            inc.item,
            id=None,
            market_value_cents=inc.market_value_cents,
            market_value_asof=occurred_date,
            review=inc.review,
            notes=with_review_note(inc.item.notes) if inc.review else inc.item.notes,
        )
        return self.items.add(new_item)

    def _void(self, txn: Transaction) -> Transaction:
        lines = self.lines.list_for_transaction(txn.id)

        # 1. Restore lots consumed by outgoing lines.
        for line in lines:
            if line.direction is Direction.OUT:
                for cons in self.consumptions.list_for_line(line.id):
                    lot = self.lots.get(cons.lot_id)
                    lot.remaining_qty += cons.quantity
                    self.lots.update(lot)
                    self.consumptions.delete(cons.id)

        # 2. Delete lots this transaction created — only if still fully intact.
        for line in lines:
            if line.direction is Direction.IN:
                for lot in self.lots.list_for_acquired_line(line.id):
                    if lot.remaining_qty != lot.quantity:
                        raise TransactionStateError(
                            f"lot {lot.id} created by transaction {txn.id} has been "
                            "partially sold; void the dependent transaction first"
                        )
                    self.lots.delete(lot.id)

        txn.status = TransactionStatus.VOIDED
        txn.voided_at = now_iso()
        self.txns.update(txn)
        return txn

    # -- audit --------------------------------------------------------------

    def _snapshot(self, transaction_id: int) -> str:
        """JSON snapshot of a transaction and its lines for the audit log."""
        txn = self.txns.get(transaction_id)
        lines = self.lines.list_for_transaction(transaction_id)
        payload = {
            "transaction": {
                "id": txn.id,
                "type": txn.type.value,
                "occurred_at": txn.occurred_at,
                "counterparty": txn.counterparty,
                "reference": txn.reference,
                "cash_delta_cents": txn.cash_delta_cents,
                "status": txn.status.value,
                "notes": txn.notes,
            },
            "lines": [
                {
                    "id": ln.id,
                    "item_id": ln.item_id,
                    "direction": ln.direction.value,
                    "quantity": ln.quantity,
                    "unit_price_cents": ln.unit_price_cents,
                    "realized_pl_cents": ln.realized_pl_cents,
                }
                for ln in lines
            ],
        }
        return json.dumps(payload, sort_keys=True)

    def _write_audit(
        self,
        entity_id: int,
        action: AuditAction,
        *,
        before: str | None = None,
        after: str | None = None,
        note: str | None = None,
    ) -> None:
        self.audit.add(
            AuditLogEntry(
                entity="transaction",
                entity_id=entity_id,
                action=action,
                before_json=before,
                after_json=after,
                note=note,
            )
        )
