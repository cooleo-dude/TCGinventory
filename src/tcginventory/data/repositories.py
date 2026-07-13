"""Repositories — the only place that issues SQL (see specs/01-architecture.md).

Each repository wraps a :class:`sqlite3.Connection` and maps between domain
dataclasses and rows. Repositories do **no** money math or business rules; that
belongs to the service layer (M2). Writes here do not commit — the caller (a
service) owns the transaction boundary — except where a single ``add`` is the
whole operation in tests, callers may commit explicitly.

Enum columns are stored as their ``.value`` strings and rehydrated on read;
booleans are stored as 0/1.
"""

from __future__ import annotations

import sqlite3

from tcginventory.data.db import now_iso
from tcginventory.domain.enums import (
    AuditAction,
    Category,
    Condition,
    Direction,
    GradingCompany,
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
    PriceSnapshot,
    Transaction,
    TransactionLine,
)

# Columns that together define an item's identity for auto match-or-create
# (specs/02-data-model.md — Core concepts / F-IT-1).
_ITEM_IDENTITY_COLUMNS = (
    "name",
    "set_name",
    "set_code",
    "card_number",
    "language",
    "condition",
    "is_graded",
    "grading_company",
    "grade",
)


def _opt_enum(enum_cls, value):
    """Rehydrate an optional enum column (``None`` stays ``None``)."""
    return enum_cls(value) if value is not None else None


class ItemRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _to_model(row: sqlite3.Row) -> Item:
        return Item(
            id=row["id"],
            name=row["name"],
            game=row["game"],
            set_name=row["set_name"],
            set_code=row["set_code"],
            card_number=row["card_number"],
            language=Language(row["language"]),
            category=Category(row["category"]),
            condition=_opt_enum(Condition, row["condition"]),
            is_graded=bool(row["is_graded"]),
            grading_company=_opt_enum(GradingCompany, row["grading_company"]),
            grade=row["grade"],
            cert_number=row["cert_number"],
            location=row["location"],
            tcgplayer_url=row["tcgplayer_url"],
            ebay_url=row["ebay_url"],
            market_value_cents=row["market_value_cents"],
            market_value_asof=row["market_value_asof"],
            review=bool(row["review"]),
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def add(self, item: Item) -> Item:
        ts = now_iso()
        item.created_at = item.created_at or ts
        item.updated_at = item.updated_at or ts
        cur = self.conn.execute(
            """
            INSERT INTO item (
                name, game, set_name, set_code, card_number, language, category,
                condition, is_graded, grading_company, grade, cert_number,
                location, tcgplayer_url, ebay_url, market_value_cents,
                market_value_asof, review, notes, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item.name,
                item.game,
                item.set_name,
                item.set_code,
                item.card_number,
                item.language.value,
                item.category.value,
                item.condition.value if item.condition else None,
                int(item.is_graded),
                item.grading_company.value if item.grading_company else None,
                item.grade,
                item.cert_number,
                item.location,
                item.tcgplayer_url,
                item.ebay_url,
                item.market_value_cents,
                item.market_value_asof,
                int(item.review),
                item.notes,
                item.created_at,
                item.updated_at,
            ),
        )
        item.id = cur.lastrowid
        return item

    def get(self, item_id: int) -> Item | None:
        row = self.conn.execute(
            "SELECT * FROM item WHERE id = ?", (item_id,)
        ).fetchone()
        return self._to_model(row) if row else None

    def list_all(self) -> list[Item]:
        rows = self.conn.execute("SELECT * FROM item ORDER BY id").fetchall()
        return [self._to_model(r) for r in rows]

    def find_by_identity(self, item: Item) -> Item | None:
        """Return the existing item matching ``item``'s identity key, or None.

        Uses SQLite's NULL-safe ``IS`` so unset identity fields compare equal.
        """
        where = " AND ".join(f"{col} IS ?" for col in _ITEM_IDENTITY_COLUMNS)
        params = (
            item.name,
            item.set_name,
            item.set_code,
            item.card_number,
            item.language.value,
            item.condition.value if item.condition else None,
            int(item.is_graded),
            item.grading_company.value if item.grading_company else None,
            item.grade,
        )
        row = self.conn.execute(
            f"SELECT * FROM item WHERE {where} ORDER BY id LIMIT 1", params
        ).fetchone()
        return self._to_model(row) if row else None

    def update(self, item: Item) -> Item:
        if item.id is None:
            raise ValueError("cannot update an item without an id")
        item.updated_at = now_iso()
        self.conn.execute(
            """
            UPDATE item SET
                name = ?, game = ?, set_name = ?, set_code = ?, card_number = ?,
                language = ?, category = ?, condition = ?, is_graded = ?,
                grading_company = ?, grade = ?, cert_number = ?,
                location = ?, tcgplayer_url = ?, ebay_url = ?,
                market_value_cents = ?, market_value_asof = ?, review = ?,
                notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                item.name,
                item.game,
                item.set_name,
                item.set_code,
                item.card_number,
                item.language.value,
                item.category.value,
                item.condition.value if item.condition else None,
                int(item.is_graded),
                item.grading_company.value if item.grading_company else None,
                item.grade,
                item.cert_number,
                item.location,
                item.tcgplayer_url,
                item.ebay_url,
                item.market_value_cents,
                item.market_value_asof,
                int(item.review),
                item.notes,
                item.updated_at,
                item.id,
            ),
        )
        return item

    def delete(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM item WHERE id = ?", (item_id,))


class LotRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _to_model(row: sqlite3.Row) -> Lot:
        return Lot(
            id=row["id"],
            item_id=row["item_id"],
            acquired_txn_line_id=row["acquired_txn_line_id"],
            acquired_at=row["acquired_at"],
            quantity=row["quantity"],
            remaining_qty=row["remaining_qty"],
            unit_cost_cents=row["unit_cost_cents"],
            acquired_market_value_cents=row["acquired_market_value_cents"],
            intent=Intent(row["intent"]),
            sku=row["sku"],
            source=row["source"],
            notes=row["notes"],
            created_at=row["created_at"],
        )

    def add(self, lot: Lot) -> Lot:
        lot.created_at = lot.created_at or now_iso()
        cur = self.conn.execute(
            """
            INSERT INTO lot (
                item_id, acquired_txn_line_id, acquired_at, quantity,
                remaining_qty, unit_cost_cents, acquired_market_value_cents,
                intent, sku, source, notes, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                lot.item_id,
                lot.acquired_txn_line_id,
                lot.acquired_at,
                lot.quantity,
                lot.remaining_qty,
                lot.unit_cost_cents,
                lot.acquired_market_value_cents,
                lot.intent.value,
                lot.sku,
                lot.source,
                lot.notes,
                lot.created_at,
            ),
        )
        lot.id = cur.lastrowid
        return lot

    def get(self, lot_id: int) -> Lot | None:
        row = self.conn.execute("SELECT * FROM lot WHERE id = ?", (lot_id,)).fetchone()
        return self._to_model(row) if row else None

    def list_for_item(self, item_id: int) -> list[Lot]:
        rows = self.conn.execute(
            "SELECT * FROM lot WHERE item_id = ? ORDER BY acquired_at, id",
            (item_id,),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    def open_lots_for_item(self, item_id: int) -> list[Lot]:
        """Open (unsold) lots for an item in FIFO order — oldest acquisition first."""
        rows = self.conn.execute(
            """
            SELECT * FROM lot
            WHERE item_id = ? AND remaining_qty > 0
            ORDER BY acquired_at, id
            """,
            (item_id,),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    def list_for_acquired_line(self, transaction_line_id: int) -> list[Lot]:
        """Lots created by a given incoming transaction line (for void/edit)."""
        rows = self.conn.execute(
            "SELECT * FROM lot WHERE acquired_txn_line_id = ? ORDER BY id",
            (transaction_line_id,),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    def list_all(self) -> list[Lot]:
        rows = self.conn.execute("SELECT * FROM lot ORDER BY id").fetchall()
        return [self._to_model(r) for r in rows]

    def find_by_sku(self, sku: str) -> Lot | None:
        row = self.conn.execute(
            "SELECT * FROM lot WHERE sku = ? LIMIT 1", (sku,)
        ).fetchone()
        return self._to_model(row) if row else None

    def exists_sku(self, sku: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM lot WHERE sku = ? LIMIT 1", (sku,)
            ).fetchone()
            is not None
        )

    def distinct_sku_count(self, item_id: int) -> int:
        """How many distinct SKUs already exist for an item (its copy count)."""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT sku) AS n FROM lot "
            "WHERE item_id = ? AND sku IS NOT NULL",
            (item_id,),
        ).fetchone()
        return row["n"]

    def skus_for_item(self, item_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT sku FROM lot WHERE item_id = ? AND sku IS NOT NULL",
            (item_id,),
        ).fetchall()
        return [r["sku"] for r in rows]

    def update(self, lot: Lot) -> Lot:
        if lot.id is None:
            raise ValueError("cannot update a lot without an id")
        self.conn.execute(
            """
            UPDATE lot SET
                item_id = ?, acquired_txn_line_id = ?, acquired_at = ?,
                quantity = ?, remaining_qty = ?, unit_cost_cents = ?,
                acquired_market_value_cents = ?, intent = ?, sku = ?,
                source = ?, notes = ?
            WHERE id = ?
            """,
            (
                lot.item_id,
                lot.acquired_txn_line_id,
                lot.acquired_at,
                lot.quantity,
                lot.remaining_qty,
                lot.unit_cost_cents,
                lot.acquired_market_value_cents,
                lot.intent.value,
                lot.sku,
                lot.source,
                lot.notes,
                lot.id,
            ),
        )
        return lot

    def delete(self, lot_id: int) -> None:
        self.conn.execute("DELETE FROM lot WHERE id = ?", (lot_id,))


class TransactionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _to_model(row: sqlite3.Row) -> Transaction:
        return Transaction(
            id=row["id"],
            type=TransactionType(row["type"]),
            occurred_at=row["occurred_at"],
            counterparty=row["counterparty"],
            reference=row["reference"],
            cash_delta_cents=row["cash_delta_cents"],
            status=TransactionStatus(row["status"]),
            voided_at=row["voided_at"],
            notes=row["notes"],
            created_at=row["created_at"],
        )

    def add(self, txn: Transaction) -> Transaction:
        txn.created_at = txn.created_at or now_iso()
        cur = self.conn.execute(
            """
            INSERT INTO "transaction" (
                type, occurred_at, counterparty, reference, cash_delta_cents,
                status, voided_at, notes, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                txn.type.value,
                txn.occurred_at,
                txn.counterparty,
                txn.reference,
                txn.cash_delta_cents,
                txn.status.value,
                txn.voided_at,
                txn.notes,
                txn.created_at,
            ),
        )
        txn.id = cur.lastrowid
        return txn

    def get(self, txn_id: int) -> Transaction | None:
        row = self.conn.execute(
            'SELECT * FROM "transaction" WHERE id = ?', (txn_id,)
        ).fetchone()
        return self._to_model(row) if row else None

    def list_all(self) -> list[Transaction]:
        rows = self.conn.execute(
            'SELECT * FROM "transaction" ORDER BY occurred_at, id'
        ).fetchall()
        return [self._to_model(r) for r in rows]

    def update(self, txn: Transaction) -> Transaction:
        if txn.id is None:
            raise ValueError("cannot update a transaction without an id")
        self.conn.execute(
            """
            UPDATE "transaction" SET
                type = ?, occurred_at = ?, counterparty = ?, reference = ?,
                cash_delta_cents = ?, status = ?, voided_at = ?, notes = ?
            WHERE id = ?
            """,
            (
                txn.type.value,
                txn.occurred_at,
                txn.counterparty,
                txn.reference,
                txn.cash_delta_cents,
                txn.status.value,
                txn.voided_at,
                txn.notes,
                txn.id,
            ),
        )
        return txn

    def delete(self, txn_id: int) -> None:
        self.conn.execute('DELETE FROM "transaction" WHERE id = ?', (txn_id,))


class TransactionLineRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _to_model(row: sqlite3.Row) -> TransactionLine:
        return TransactionLine(
            id=row["id"],
            transaction_id=row["transaction_id"],
            item_id=row["item_id"],
            direction=Direction(row["direction"]),
            quantity=row["quantity"],
            unit_price_cents=row["unit_price_cents"],
            realized_pl_cents=row["realized_pl_cents"],
            notes=row["notes"],
        )

    def add(self, line: TransactionLine) -> TransactionLine:
        cur = self.conn.execute(
            """
            INSERT INTO transaction_line (
                transaction_id, item_id, direction, quantity,
                unit_price_cents, realized_pl_cents, notes
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                line.transaction_id,
                line.item_id,
                line.direction.value,
                line.quantity,
                line.unit_price_cents,
                line.realized_pl_cents,
                line.notes,
            ),
        )
        line.id = cur.lastrowid
        return line

    def get(self, line_id: int) -> TransactionLine | None:
        row = self.conn.execute(
            "SELECT * FROM transaction_line WHERE id = ?", (line_id,)
        ).fetchone()
        return self._to_model(row) if row else None

    def list_for_transaction(self, transaction_id: int) -> list[TransactionLine]:
        rows = self.conn.execute(
            "SELECT * FROM transaction_line WHERE transaction_id = ? ORDER BY id",
            (transaction_id,),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    def list_for_item(self, item_id: int) -> list[TransactionLine]:
        rows = self.conn.execute(
            "SELECT * FROM transaction_line WHERE item_id = ? ORDER BY id",
            (item_id,),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    def update(self, line: TransactionLine) -> TransactionLine:
        if line.id is None:
            raise ValueError("cannot update a transaction line without an id")
        self.conn.execute(
            """
            UPDATE transaction_line SET
                transaction_id = ?, item_id = ?, direction = ?, quantity = ?,
                unit_price_cents = ?, realized_pl_cents = ?, notes = ?
            WHERE id = ?
            """,
            (
                line.transaction_id,
                line.item_id,
                line.direction.value,
                line.quantity,
                line.unit_price_cents,
                line.realized_pl_cents,
                line.notes,
                line.id,
            ),
        )
        return line

    def delete(self, line_id: int) -> None:
        self.conn.execute("DELETE FROM transaction_line WHERE id = ?", (line_id,))


class LotConsumptionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _to_model(row: sqlite3.Row) -> LotConsumption:
        return LotConsumption(
            id=row["id"],
            transaction_line_id=row["transaction_line_id"],
            lot_id=row["lot_id"],
            quantity=row["quantity"],
            unit_cost_cents=row["unit_cost_cents"],
        )

    def add(self, consumption: LotConsumption) -> LotConsumption:
        cur = self.conn.execute(
            """
            INSERT INTO lot_consumption (
                transaction_line_id, lot_id, quantity, unit_cost_cents
            ) VALUES (?,?,?,?)
            """,
            (
                consumption.transaction_line_id,
                consumption.lot_id,
                consumption.quantity,
                consumption.unit_cost_cents,
            ),
        )
        consumption.id = cur.lastrowid
        return consumption

    def list_for_line(self, transaction_line_id: int) -> list[LotConsumption]:
        rows = self.conn.execute(
            "SELECT * FROM lot_consumption WHERE transaction_line_id = ? ORDER BY id",
            (transaction_line_id,),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    def list_for_lot(self, lot_id: int) -> list[LotConsumption]:
        rows = self.conn.execute(
            "SELECT * FROM lot_consumption WHERE lot_id = ? ORDER BY id",
            (lot_id,),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    def delete(self, consumption_id: int) -> None:
        self.conn.execute("DELETE FROM lot_consumption WHERE id = ?", (consumption_id,))


class AuditLogRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _to_model(row: sqlite3.Row) -> AuditLogEntry:
        return AuditLogEntry(
            id=row["id"],
            entity=row["entity"],
            entity_id=row["entity_id"],
            action=AuditAction(row["action"]),
            at=row["at"],
            before_json=row["before_json"],
            after_json=row["after_json"],
            note=row["note"],
        )

    def add(self, entry: AuditLogEntry) -> AuditLogEntry:
        entry.at = entry.at or now_iso()
        cur = self.conn.execute(
            """
            INSERT INTO audit_log (
                entity, entity_id, action, at, before_json, after_json, note
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                entry.entity,
                entry.entity_id,
                entry.action.value,
                entry.at,
                entry.before_json,
                entry.after_json,
                entry.note,
            ),
        )
        entry.id = cur.lastrowid
        return entry

    def list_for_entity(self, entity: str, entity_id: int) -> list[AuditLogEntry]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log WHERE entity = ? AND entity_id = ? ORDER BY id",
            (entity, entity_id),
        ).fetchall()
        return [self._to_model(r) for r in rows]


class PriceSnapshotRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _to_model(row: sqlite3.Row) -> PriceSnapshot:
        return PriceSnapshot(
            id=row["id"],
            item_id=row["item_id"],
            source=row["source"],
            price_cents=row["price_cents"],
            currency=row["currency"],
            as_of=row["as_of"],
            note=row["note"],
        )

    def add(self, snapshot: PriceSnapshot) -> PriceSnapshot:
        snapshot.as_of = snapshot.as_of or now_iso()
        cur = self.conn.execute(
            """
            INSERT INTO price_snapshot (
                item_id, source, price_cents, currency, as_of, note
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                snapshot.item_id,
                snapshot.source,
                snapshot.price_cents,
                snapshot.currency,
                snapshot.as_of,
                snapshot.note,
            ),
        )
        snapshot.id = cur.lastrowid
        return snapshot

    def history_for_item(
        self, item_id: int, since: str | None = None
    ) -> list[PriceSnapshot]:
        """Snapshots for an item (oldest first), optionally from ``since`` onward."""
        if since is None:
            rows = self.conn.execute(
                "SELECT * FROM price_snapshot WHERE item_id = ? ORDER BY as_of, id",
                (item_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM price_snapshot WHERE item_id = ? AND as_of >= ? "
                "ORDER BY as_of, id",
                (item_id, since),
            ).fetchall()
        return [self._to_model(r) for r in rows]

    def delete_for_item(self, item_id: int) -> None:
        """Remove all price snapshots for an item (used when deleting the item)."""
        self.conn.execute("DELETE FROM price_snapshot WHERE item_id = ?", (item_id,))

    def latest_as_of(self) -> str | None:
        """The most recent snapshot timestamp across all items, or None if never
        refreshed — i.e. when prices were last known to update."""
        row = self.conn.execute(
            "SELECT MAX(as_of) AS latest FROM price_snapshot"
        ).fetchone()
        return row["latest"] if row and row["latest"] else None

    def latest_prices(self) -> dict[int, int]:
        """Map item_id → most-recent per-unit price across all polled items."""
        rows = self.conn.execute(
            "SELECT item_id, price_cents, as_of FROM price_snapshot "
            "ORDER BY item_id, as_of, id"
        ).fetchall()
        latest: dict[int, int] = {}
        for r in rows:  # ascending order → last write per item wins
            latest[r["item_id"]] = r["price_cents"]
        return latest
