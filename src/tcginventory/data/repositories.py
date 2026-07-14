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
    CardShow,
    CatalogCard,
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


class CardShowRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _to_model(row: sqlite3.Row) -> CardShow:
        return CardShow(
            id=row["id"],
            source_id=row["source_id"],
            name=row["name"],
            venue=row["venue"],
            city=row["city"],
            state=row["state"],
            starts_on=row["starts_on"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            detail_url=row["detail_url"],
            fetched_at=row["fetched_at"],
        )

    def upsert(self, show: CardShow) -> None:
        """Insert or update a show by its tcdb ``source_id``."""
        self.conn.execute(
            """
            INSERT INTO card_show (
                source_id, name, venue, city, state, starts_on,
                start_time, end_time, detail_url, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
                name = excluded.name, venue = excluded.venue,
                city = excluded.city, state = excluded.state,
                starts_on = excluded.starts_on, start_time = excluded.start_time,
                end_time = excluded.end_time, detail_url = excluded.detail_url,
                fetched_at = excluded.fetched_at
            """,
            (
                show.source_id,
                show.name,
                show.venue,
                show.city,
                show.state,
                show.starts_on,
                show.start_time,
                show.end_time,
                show.detail_url,
                show.fetched_at,
            ),
        )

    def _to_model_with_status(self, row: sqlite3.Row) -> CardShow:
        show = self._to_model(row)
        show.status = row["status"]
        return show

    def list_between(self, start_date: str, end_date: str) -> list[CardShow]:
        """Shows with ``starts_on`` in [start_date, end_date] (inclusive)."""
        rows = self.conn.execute(
            "SELECT c.*, s.status AS status FROM card_show c "
            "LEFT JOIN card_show_status s ON s.source_id = c.source_id "
            "WHERE c.starts_on >= ? AND c.starts_on <= ? "
            "ORDER BY c.starts_on, c.start_time, c.name",
            (start_date, end_date),
        ).fetchall()
        return [self._to_model_with_status(r) for r in rows]

    def list_for_states(self, states: list[str]) -> list[CardShow]:
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        rows = self.conn.execute(
            f"SELECT c.*, s.status AS status FROM card_show c "
            f"LEFT JOIN card_show_status s ON s.source_id = c.source_id "
            f"WHERE c.state IN ({placeholders}) "
            "ORDER BY c.starts_on, c.start_time, c.name",
            tuple(states),
        ).fetchall()
        return [self._to_model_with_status(r) for r in rows]

    def set_status(self, source_id: int, status: str | None) -> None:
        """Set (or clear, when ``status`` is None) a show's attendance status.

        Keyed by tcdb ``source_id`` so it persists across show refreshes.
        """
        if status is None:
            self.conn.execute(
                "DELETE FROM card_show_status WHERE source_id = ?", (source_id,)
            )
        else:
            self.conn.execute(
                "INSERT INTO card_show_status (source_id, status) VALUES (?, ?) "
                "ON CONFLICT(source_id) DO UPDATE SET status = excluded.status",
                (source_id, status),
            )

    def delete_before(self, date: str) -> int:
        """Drop shows that start before ``date`` (past-show pruning). Returns count."""
        cur = self.conn.execute("DELETE FROM card_show WHERE starts_on < ?", (date,))
        return cur.rowcount

    def delete_for_state(self, state: str) -> None:
        """Remove all shows for a state (e.g. when it's toggled off / re-fetched)."""
        self.conn.execute("DELETE FROM card_show WHERE state = ?", (state,))


class CardCatalogRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _to_model(row: sqlite3.Row) -> CatalogCard:
        return CatalogCard(
            card_id=row["card_id"],
            name=row["name"],
            set_id=row["set_id"],
            set_name=row["set_name"],
            set_code=row["set_code"],
            number=row["number"],
            rarity=row["rarity"],
            supertype=row["supertype"],
            image_small=row["image_small"],
            printed_total=row["printed_total"],
        )

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM card_catalog").fetchone()[0]

    def replace_all(self, cards: list[CatalogCard], updated_at: str) -> None:
        """Swap the whole catalog for a freshly-pulled set (single transaction)."""
        self.conn.execute("DELETE FROM card_catalog")
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO card_catalog (
                card_id, name, set_id, set_name, set_code, number, rarity,
                supertype, image_small, printed_total, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    c.card_id, c.name, c.set_id, c.set_name, c.set_code,
                    c.number, c.rarity, c.supertype, c.image_small,
                    c.printed_total, updated_at,
                )
                for c in cards
            ],
        )

    def all_names(self) -> list[str]:
        """Every distinct card name (for a name-autocomplete model)."""
        rows = self.conn.execute(
            "SELECT DISTINCT name FROM card_catalog ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def distinct_names(self, prefix: str, limit: int = 20) -> list[str]:
        """Distinct card names starting with ``prefix`` (case-insensitive)."""
        rows = self.conn.execute(
            "SELECT DISTINCT name FROM card_catalog WHERE name LIKE ? "
            "ORDER BY name LIMIT ?",
            (prefix.replace("%", "") + "%", limit),
        ).fetchall()
        return [r["name"] for r in rows]

    def find_by_name(self, name: str) -> list[CatalogCard]:
        """All printings of an exact card name, newest-set first-ish."""
        rows = self.conn.execute(
            "SELECT * FROM card_catalog WHERE name = ? ORDER BY set_name, number",
            (name,),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    def search(self, query: str, limit: int = 25) -> list[CatalogCard]:
        """Free-text card search that enriches with set + collector number.

        Every whitespace-separated token must match *somewhere* — the name, the
        set name, or (if it contains a digit) the collector number. So the
        tokens can be given in any order and the number may be bare or full:
        ``Pikachu Celebrations 5``, ``Pikachu 5 Celebrations``,
        ``Wooloo SV103 Shiny Vault`` and ``Wooloo Shiny Vault SV103/SV122`` all
        narrow to the right printing.
        """
        query = query.strip()
        if not query:
            return []

        clauses, params = [], []
        for tok in query.split():
            like = f"%{tok}%"
            parts = ["name LIKE ?", "set_name LIKE ?"]
            tok_params = [like, like]
            if any(ch.isdigit() for ch in tok):
                # A digit-bearing token might be the collector number, in any of
                # its forms: full ("SV103/SV122"), head ("SV103"), or
                # leading-zeros stripped ("5" for "005").
                head = tok.split("/", 1)[0].strip()
                for form in dict.fromkeys([tok, head, head.lstrip("0") or head]):
                    parts.append("number = ? COLLATE NOCASE")
                    tok_params.append(form)
            clauses.append("(" + " OR ".join(parts) + ")")
            params += tok_params

        rows = self.conn.execute(
            "SELECT * FROM card_catalog WHERE "
            + " AND ".join(clauses)
            + " ORDER BY name, set_name, number LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    def image_for(
        self,
        name: str | None,
        set_name: str | None = None,
        number: str | None = None,
    ) -> tuple[str, str] | None:
        """Best-effort ``(card_id, image_small)`` for a card by printed identity.

        Used where we hold only the printed identity (inventory rows, given
        cards) rather than a catalog id. Matching rules:

        * If a **set** is given, only cards *in that set* are eligible (set names
          are compared with containment so "Trading Card Game Classic" matches a
          catalog "Pokémon Trading Card Game Classic"), and the match must also
          agree on the collector number or the name. If the set isn't in the
          catalog at all — e.g. a special product the pokemontcg.io dataset
          lacks — this returns ``None`` rather than a same-number card from some
          *other* set.
        * If no set is given, the collector **number and name** must both agree.

        A weaker match returns ``None``: better no image than the wrong card.
        """

        def norm_num(value: str | None) -> str:
            head = (value or "").split("/", 1)[0].strip().lower()
            return head.lstrip("0") or head

        want_num = norm_num(number)
        want_set = (set_name or "").strip().lower()
        want_name = (name or "").strip().lower()

        cols = "SELECT card_id, name, set_name, number, image_small FROM card_catalog"
        pool: dict[str, sqlite3.Row] = {}

        def gather(where: str, params: tuple) -> None:
            for row in self.conn.execute(f"{cols} WHERE {where}", params).fetchall():
                pool.setdefault(row["card_id"], row)

        # Candidate pool: cards in the same set, same name, and/or same number.
        if want_set:
            gather(
                "set_name LIKE ? COLLATE NOCASE AND image_small IS NOT NULL",
                (f"%{set_name.strip()}%",),
            )
        if name:
            gather("name = ? COLLATE NOCASE AND image_small IS NOT NULL", (name,))
        if want_num:
            gather(
                "(number = ? OR number = ? OR number = ?) "
                "AND image_small IS NOT NULL LIMIT 400",
                ((number or "").strip(), norm_num(number), want_num),
            )
        if not pool and name:  # loose fallback when nothing else hit
            gather(
                "name LIKE ? AND image_small IS NOT NULL LIMIT 50",
                (f"%{name.strip()}%",),
            )
        if not pool:
            return None

        def set_ok(row: sqlite3.Row) -> bool:
            other = (row["set_name"] or "").strip().lower()
            return bool(want_set) and bool(other) and (
                want_set in other or other in want_set
            )

        def num_ok(row: sqlite3.Row) -> bool:
            return bool(want_num) and norm_num(row["number"]) == want_num

        def name_ok(row: sqlite3.Row) -> bool:
            other = (row["name"] or "").strip().lower()
            return bool(want_name) and (want_name in other or other in want_name)

        if want_set:
            # A set was named → restrict to that set, then require number or
            # name agreement. An unknown set has no eligible rows → None.
            eligible = [r for r in pool.values() if set_ok(r)]
            eligible = [r for r in eligible if num_ok(r) or name_ok(r)]
            if not eligible:
                return None
            best = max(eligible, key=lambda r: (num_ok(r), name_ok(r)))
        else:
            # No set → number and name must both agree.
            eligible = [r for r in pool.values() if num_ok(r) and name_ok(r)]
            if not eligible:
                return None
            best = eligible[0]
        return best["card_id"], best["image_small"]
