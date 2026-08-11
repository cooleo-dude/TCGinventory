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
from collections.abc import Sequence

from tcginventory.data.db import now_iso
from tcginventory.domain.enums import (
    AuditAction,
    Category,
    Condition,
    Direction,
    Edition,
    GradingCompany,
    HoloPattern,
    Intent,
    Language,
    TransactionStatus,
    TransactionType,
)
from tcginventory.domain.models import (
    AuditLogEntry,
    CalendarEntry,
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
    "holo_pattern",
    "edition",
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
            holo_pattern=_opt_enum(HoloPattern, row["holo_pattern"]),
            edition=_opt_enum(Edition, row["edition"]),
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
                condition, holo_pattern, edition, is_graded, grading_company,
                grade, cert_number, location, tcgplayer_url, ebay_url,
                market_value_cents, market_value_asof, review, notes,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                item.holo_pattern.value if item.holo_pattern else None,
                item.edition.value if item.edition else None,
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

    def count(self) -> int:
        """How many distinct cards exist (the Operations fact strip)."""
        row = self.conn.execute("SELECT COUNT(*) AS n FROM item").fetchone()
        return int(row["n"]) if row else 0

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
            item.holo_pattern.value if item.holo_pattern else None,
            item.edition.value if item.edition else None,
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
                language = ?, category = ?, condition = ?, holo_pattern = ?,
                edition = ?, is_graded = ?,
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
                item.holo_pattern.value if item.holo_pattern else None,
                item.edition.value if item.edition else None,
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
            missing=bool(row["missing"]),
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
                intent, sku, missing, source, notes, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                int(lot.missing),
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
        """Open (unsold) lots for an item in FIFO order — oldest acquisition first.

        Missing (stolen/lost) lots are excluded: they are no longer on hand, so a
        disposal can never draw from them (mirrors ripping's exclusion)."""
        rows = self.conn.execute(
            """
            SELECT * FROM lot
            WHERE item_id = ? AND remaining_qty > 0 AND missing = 0
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

    def on_hand_total(self) -> int:
        """Total copies still held across every lot (the Operations fact strip)."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(remaining_qty), 0) AS n FROM lot"
        ).fetchone()
        return int(row["n"]) if row else 0

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

    def update(self, lot: Lot) -> Lot:
        if lot.id is None:
            raise ValueError("cannot update a lot without an id")
        self.conn.execute(
            """
            UPDATE lot SET
                item_id = ?, acquired_txn_line_id = ?, acquired_at = ?,
                quantity = ?, remaining_qty = ?, unit_cost_cents = ?,
                acquired_market_value_cents = ?, intent = ?, sku = ?,
                missing = ?, source = ?, notes = ?
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
                int(lot.missing),
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
        """Map item_id → most-recent per-unit price across all polled items.

        Reads only each item's latest snapshot (via the ``(item_id, as_of)``
        index) rather than materialising the whole history in Python; ties on
        ``as_of`` resolve to the highest ``id`` (last write wins)."""
        rows = self.conn.execute(
            "SELECT ps.item_id AS item_id, ps.price_cents AS price "
            "FROM price_snapshot ps JOIN ("
            "  SELECT item_id, MAX(as_of) AS m FROM price_snapshot GROUP BY item_id"
            ") t ON t.item_id = ps.item_id AND t.m = ps.as_of "
            "ORDER BY ps.item_id, ps.id"
        ).fetchall()
        return {r["item_id"]: r["price"] for r in rows}

    def prices_by_item_since(self, since: str | None = None) -> dict[int, list[int]]:
        """Map item_id → its per-unit price series (oldest first) since ``since``
        — one query for the whole set (for batch volatility), vs. per-item."""
        sql = "SELECT item_id, price_cents FROM price_snapshot"
        params: tuple = ()
        if since is not None:
            sql += " WHERE as_of >= ?"
            params = (since,)
        sql += " ORDER BY item_id, as_of, id"
        out: dict[int, list[int]] = {}
        for r in self.conn.execute(sql, params):
            out.setdefault(r["item_id"], []).append(r["price_cents"])
        return out


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

    def upcoming(self, today: str, limit: int | None) -> list[CardShow]:
        """The next ``limit`` shows on/after ``today`` the user plans to be at
        (``limit=None`` → all). Only **attending**/**walking** shows — the
        dashboard box is a "where am I going" list, not the full calendar."""
        rows = self.conn.execute(
            "SELECT c.*, s.status AS status FROM card_show c "
            "JOIN card_show_status s ON s.source_id = c.source_id "
            "WHERE c.starts_on >= ? AND s.status IN ('attending', 'walking') "
            "ORDER BY c.starts_on, c.start_time, c.name LIMIT ?",
            (today, limit if limit is not None else -1),  # SQLite LIMIT -1 = all
        ).fetchall()
        return [self._to_model_with_status(r) for r in rows]

    def count_from(self, today: str) -> int:
        """How many scraped shows are still ahead — what a refresh actually
        produced, as opposed to the handful you've marked attending."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM card_show WHERE starts_on >= ?", (today,)
        ).fetchone()
        return int(row["n"]) if row else 0

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
            language=row["language"],
        )

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM card_catalog").fetchone()[0]

    def display_number(self, card: CatalogCard) -> str | None:
        """Collector number with the correct denominator.

        For a **prefixed subset** number (e.g. Radiant Collection "RC19"), the
        parent set's ``printed_total`` is the wrong denominator — the subset has
        its own size. Derive it from the count of same-prefixed cards in the set,
        yielding "RC19/RC32". Numeric numbers use the plain ``full_number``
        (numerator + parent total)."""
        if not card.number:
            return None
        prefix = self._subset_prefix(card)
        if not prefix:  # purely numeric → parent total is correct
            return card.full_number
        return self._subset_number(card, prefix, self._subset_total(card, prefix))

    @staticmethod
    def _subset_prefix(card: CatalogCard) -> str:
        """The non-numeric head of a collector number ("RC" of "RC19"); "" when
        the number is purely numeric, and the parent total is then correct."""
        prefix = ""
        for ch in card.number or "":
            if ch.isdigit():
                break
            prefix += ch
        return prefix

    @staticmethod
    def _subset_number(card: CatalogCard, prefix: str, total: int) -> str | None:
        return f"{card.number}/{prefix}{total}" if total else card.number

    def _subset_total(self, card: CatalogCard, prefix: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT number) AS n FROM card_catalog "
            "WHERE set_id = ? AND language = ? AND number LIKE ?",
            (card.set_id, card.language, f"{prefix}%"),
        ).fetchone()
        return row["n"] if row else 0

    def display_numbers(self, cards: Sequence[CatalogCard]) -> dict[str, str | None]:
        """:meth:`display_number` for a whole result set, by ``card_id``.

        Called per row this was the second half of the autocomplete lag: one
        ``COUNT(DISTINCT number)`` for each of 25–50 matches, 0–20 ms a popup.
        The count only depends on ``(set_id, language, prefix)``, and a popup
        holds a handful of those at most, so ask once per distinct group — with
        0020's ``(set_id, language, number)`` index turning each one from a scan
        into a range seek.
        """
        totals: dict[tuple[str, str, str], int] = {}
        out: dict[str, str | None] = {}
        for card in cards:
            if not card.number:
                out[card.card_id] = None
                continue
            prefix = self._subset_prefix(card)
            if not prefix:
                out[card.card_id] = card.full_number
                continue
            key = (card.set_id, card.language, prefix)
            if key not in totals:
                totals[key] = self._subset_total(card, prefix)
            out[card.card_id] = self._subset_number(card, prefix, totals[key])
        return out

    def search_labelled(
        self, query: str, limit: int = 25
    ) -> list[tuple[CatalogCard, str | None]]:
        """:meth:`search` paired with each card's display number.

        The autocomplete popups all want the same "name · set #number" label, and
        building it a row at a time is what made :meth:`display_number` an N+1.
        """
        cards = self.search(query, limit=limit)
        numbers = self.display_numbers(cards)
        return [(c, numbers[c.card_id]) for c in cards]

    def get(self, card_id: str) -> CatalogCard | None:
        row = self.conn.execute(
            "SELECT * FROM card_catalog WHERE card_id = ?", (card_id,)
        ).fetchone()
        return self._to_model(row) if row else None

    def card_id_for(
        self, name: str | None, set_name: str | None = None, number: str | None = None
    ) -> str | None:
        """The catalog id for a printed identity, artwork or not.

        The ledger and the catalog share no foreign key — an ``item`` carries only a
        printed identity — so anything that needs to cross between them resolves it
        here. ``image_for`` applies exactly the right matching rules and has always
        returned the id alongside the picture, which is why callers borrowed it; but it
        required the card to *have* a picture, so the crossing silently failed for
        imageless entries. Same matcher, without that condition."""
        hit = self.image_for(name, set_name, number, require_image=False)
        return hit[0] if hit else None

    def _insert_many(self, cards: list[CatalogCard], updated_at: str) -> None:
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO card_catalog (
                card_id, name, set_id, set_name, set_code, number, rarity,
                supertype, image_small, printed_total, language, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    c.card_id,
                    c.name,
                    c.set_id,
                    c.set_name,
                    c.set_code,
                    c.number,
                    c.rarity,
                    c.supertype,
                    c.image_small,
                    c.printed_total,
                    c.language,
                    updated_at,
                )
                for c in cards
            ],
        )
        self._rebuild_fts()

    def _rebuild_fts(self) -> None:
        """Re-derive the search index from card_catalog.

        ``card_catalog_fts`` is an FTS5 *external-content* table (migration 0020):
        it holds the index but reads the columns back from ``card_catalog``, so
        SQLite does not maintain it on writes. Every write goes through
        :meth:`_insert_many`, so rebuilding here covers ``replace_all`` and
        ``replace_for_language`` both — one rebuild per catalog refresh (~250 ms
        for 53k rows) rather than a trigger firing 53k times.
        """
        self.conn.execute(
            "INSERT INTO card_catalog_fts(card_catalog_fts) VALUES ('rebuild')"
        )

    def replace_all(self, cards: list[CatalogCard], updated_at: str) -> None:
        """Swap the whole catalog for a freshly-pulled set (single transaction)."""
        self.conn.execute("DELETE FROM card_catalog")
        self._insert_many(cards, updated_at)

    def replace_for_language(
        self, cards: list[CatalogCard], language: str, updated_at: str
    ) -> None:
        """Replace only one language's rows, leaving the other language intact
        (so an English re-pull doesn't wipe the Japanese catalog, or vice versa).
        """
        self.conn.execute("DELETE FROM card_catalog WHERE language = ?", (language,))
        self._insert_many(cards, updated_at)

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
            parts = ["name LIKE ?", "set_name LIKE ?", "set_code LIKE ?"]
            tok_params = [like, like, like]
            if "/" in tok and any(ch.isdigit() for ch in tok):
                # A full collector number, in any of its forms: full
                # ("SV103/SV122"), head ("SV103"), or leading-zeros stripped.
                head = tok.split("/", 1)[0].strip()
                for form in dict.fromkeys([tok, head, head.lstrip("0") or head]):
                    parts.append("number = ? COLLATE NOCASE")
                    tok_params.append(form)
            elif any(ch.isalpha() for ch in tok):
                # A lettered token may be a collector-number *prefix* — "SV" and
                # "SV074" both match "SV074" — so the number can be typed
                # partially. (A pure word like "Vault" simply matches nothing.)
                parts.append("number LIKE ? COLLATE NOCASE")
                tok_params.append(f"{tok}%")
            elif tok.isdigit():
                # A bare number matches exactly (not a prefix, so "1" ≠ "10").
                for form in dict.fromkeys([tok, tok.lstrip("0") or tok]):
                    parts.append("number = ? COLLATE NOCASE")
                    tok_params.append(form)
            clauses.append("(" + " OR ".join(parts) + ")")
            params += tok_params

        # The predicate above stays the authority on what matches. The FTS index
        # only *prunes* the rows it is evaluated against — see _fts_prune.
        prune = self._fts_prune(query)
        # A rowid subquery rather than a join, so the predicate needs no table
        # qualifying: card_catalog and its index share four column names, and
        # every unqualified one would be ambiguous.
        narrow = (
            "rowid IN (SELECT rowid FROM card_catalog_fts "
            "WHERE card_catalog_fts MATCH ?) AND "
            if prune
            else ""
        )

        rows = self.conn.execute(
            f"SELECT * FROM card_catalog WHERE {narrow}"
            + " AND ".join(clauses)
            + " ORDER BY name, set_name, number LIMIT ?",
            ((prune,) if prune else ()) + (*params, limit),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    # Trigram indexes every 3-character window, so a shorter needle matches
    # nothing at all — silently, with no error. Anything below this length has to
    # be left to the SQL predicate.
    _TRIGRAM_MIN = 3

    @staticmethod
    def _fts_core(token: str) -> str | None:
        """The substring of ``token`` safe to prune on, or None.

        Safe means: **every row the SQL predicate could match for this token also
        contains this substring.** Prune on anything narrower and real results
        disappear.

        A collector number given in full is the case that bites. For
        ``SV103/SV122`` the predicate also accepts the row numbered plain
        ``SV103``, which does not contain the full string — so the head is the
        most that can be pruned on. (Verified against the catalog: the full form
        matches 0 rows, the head matches the Wooloo the search is for.)
        """
        core = token.split("/", 1)[0].strip() if "/" in token else token
        return core if len(core) >= CardCatalogRepository._TRIGRAM_MIN else None

    def _fts_prune(self, query: str) -> str:
        """An FTS5 MATCH expression narrowing the candidate rows, or ``""``.

        Empty when no token is long enough to be indexed (e.g. ``"ex 5"``), in
        which case the search falls back to the scan it has always done — correct,
        just not fast, and rare now the callers require three characters.
        """
        cores = [c for c in map(self._fts_core, query.split()) if c]
        # A quoted phrase is a run of adjacent trigrams, which is how trigram FTS
        # expresses "contains". Double quotes are the one character that could end
        # the phrase early; FTS5 escapes them by doubling.
        return " AND ".join(f'"{c.replace(chr(34), chr(34) * 2)}"' for c in cores)

    def image_for(
        self,
        name: str | None,
        set_name: str | None = None,
        number: str | None = None,
        *,
        require_image: bool = True,
    ) -> tuple[str, str] | None:
        """Best-effort ``(card_id, image_small)`` for a card by printed identity.

        ``require_image=False`` runs the same matching without insisting the card have
        artwork, which is how callers that want the **identity** rather than the image
        use it — see ``CardCatalogRepository.card_id_for``. Every branch below filtered
        on ``image_small IS NOT NULL``, so resolving a card id through this silently
        failed for any imageless catalog entry.

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

        A weaker match returns ``None``: better no image than the wrong card. In
        particular, when the number agrees with nothing in the set, only a *sole*
        same-name printing is trusted — picking one of several by name alone is a
        guess, and a wrong art is worse than a blank. A variant the catalog simply
        doesn't carry (the alt-art "195a/214") therefore resolves to nothing here;
        :func:`tcginventory.services.cardimages.art_for` covers it from the
        item's own TCGplayer product instead.
        """

        def norm_num(value: str | None) -> str:
            head = (value or "").split("/", 1)[0].strip().lower()
            return head.lstrip("0") or head

        # Reconcile a legacy set name ("Scarlet & Violet Black Star Promos") to
        # the catalog's current nomenclature ("SVP Black Star Promos") so items
        # saved before the catalog switch still resolve.
        from tcginventory.data.setnames import canonical_set_name

        set_name = canonical_set_name(set_name) or set_name

        want_num = norm_num(number)
        want_set = (set_name or "").strip().lower()

        cols = (
            "SELECT card_id, name, set_name, number, image_small, printed_total "
            "FROM card_catalog"
        )
        pool: dict[str, sqlite3.Row] = {}
        has_art = " AND image_small IS NOT NULL" if require_image else ""

        def gather(where: str, params: tuple) -> None:
            for row in self.conn.execute(f"{cols} WHERE {where}", params).fetchall():
                pool.setdefault(row["card_id"], row)

        # Candidate pool: cards in the same set, same name, and/or same number.
        # Exact set match (index-backed); containment / fuzzy-named sets still
        # land in the pool via the name/number gathers below and are handled in
        # scoring, so this stays correct while avoiding a full-table LIKE scan.
        if want_set:
            gather(
                f"set_name = ? COLLATE NOCASE{has_art}",
                (set_name.strip(),),
            )
        if name:
            gather(f"name = ? COLLATE NOCASE{has_art}", (name,))
        if want_num:
            gather(
                f"(number = ? OR number = ?){has_art} LIMIT 400",
                ((number or "").strip(), want_num),
            )
        if not pool and name:  # loose fallback when nothing else hit
            gather(
                f"name LIKE ?{has_art} LIMIT 50",
                (f"%{name.strip()}%",),
            )
        if not pool:
            return None

        def flat(value: str | None) -> str:
            # Ignore case, spaces and punctuation so "Jirachi GX" == "Jirachi-GX".
            return "".join(ch for ch in (value or "").lower() if ch.isalnum())

        flat_name = flat(name)

        def set_exact(row: sqlite3.Row) -> bool:
            other = (row["set_name"] or "").strip().lower()
            return bool(want_set) and other == want_set

        def set_ok(row: sqlite3.Row) -> bool:
            other = (row["set_name"] or "").strip().lower()
            return (
                bool(want_set)
                and bool(other)
                and (want_set in other or other in want_set)
            )

        def num_ok(row: sqlite3.Row) -> bool:
            return bool(want_num) and norm_num(row["number"]) == want_num

        def name_ok(row: sqlite3.Row) -> bool:
            other = flat(row["name"])
            return (
                bool(flat_name)
                and bool(other)
                and (flat_name in other or other in flat_name)
            )

        def name_exact(row: sqlite3.Row) -> bool:
            """Same card name, ignoring case/punctuation. Containment alone is
            far too loose to pick a printing by — "Giovanni" is a substring of
            "Giovanni's Machamp" — so an exact hit is preferred over it."""
            return bool(flat_name) and flat(row["name"]) == flat_name

        # 1) A set was named. An *exact* set match is trustworthy on its own, so
        #    the best card in that set wins — by number first, then by name. A
        #    looser containment match ("Mega Evolution" ⊂ "Mega Evolution Promo")
        #    is NOT trustworthy alone — it must also agree on name AND number, or
        #    we'd borrow a different set's same-numbered card.
        if want_set:
            in_set = [r for r in pool.values() if set_exact(r)]
            numbered = [r for r in in_set if num_ok(r)]
            if numbered:
                best = max(numbered, key=lambda r: (num_ok(r), name_ok(r)))
                return best["card_id"], best["image_small"]
            # No number agreement. A sole printing of this card in the set is a
            # safe read; several are a coin flip, so show nothing rather than an
            # arbitrary one. Exact names are tried before loose containment.
            for candidates in (
                [r for r in in_set if name_exact(r)],
                [r for r in in_set if name_ok(r)],
            ):
                if len(candidates) == 1:
                    return candidates[0]["card_id"], candidates[0]["image_small"]
                if candidates:
                    return None
            fuzzy = [r for r in pool.values() if set_ok(r) and name_ok(r) and num_ok(r)]
            if fuzzy:
                return fuzzy[0]["card_id"], fuzzy[0]["image_small"]
            # Set given but unknown to the catalog (alt-art / special naming) →
            # fall through to a number+name match.

        # 2) Number and name must both agree. This is the alt-art / unknown-set
        #    path, so lean on the denominator when the number carries one: a
        #    "79a/236" whose 236 matches the card's set total is confident, while
        #    a "014/034" that matches no candidate's total stays blank (its
        #    denominator proves it is *not* the same-numbered card in some other
        #    set). Without a denominator, require a single unambiguous hit.
        want_total = None
        if number and "/" in number:
            digits = "".join(ch for ch in number.split("/", 1)[1] if ch.isdigit())
            want_total = int(digits) if digits else None

        strong = [r for r in pool.values() if num_ok(r) and name_ok(r)]
        if want_total is not None:
            strong = [r for r in strong if r["printed_total"] == want_total]
        if len(strong) == 1:
            return strong[0]["card_id"], strong[0]["image_small"]
        return None

    def stats(self, language: str | None = None) -> tuple[int, str | None]:
        """``(cards, last_updated)`` for one language, or the whole catalog.

        ``updated_at`` is stamped by :meth:`replace_all` / ``replace_for_language``,
        so its maximum is when that catalog was last pulled.
        """
        sql = "SELECT COUNT(*) AS n, MAX(updated_at) AS at FROM card_catalog"
        params: tuple = ()
        if language:
            sql += " WHERE language = ?"
            params = (language,)
        row = self.conn.execute(sql, params).fetchone()
        return (row["n"] or 0, row["at"]) if row else (0, None)


class OperationRunRepository:
    """The maintenance run log (operation_run) behind the Operations window.

    Every import, export, backup, price or catalog run appends a row here — from
    the UI, the daily background task, or the startup backup — so each tile can
    say when it last ran and the Activity list survives a restart.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(
        self,
        op: str,
        *,
        ok: bool = True,
        summary: str | None = None,
        source: str = "ui",
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> int:
        """Append one run; returns its id. ``started_at`` defaults to now, so a
        caller that didn't time the work still gets an ordered entry."""
        now = now_iso()
        cur = self.conn.execute(
            "INSERT INTO operation_run (op, started_at, finished_at, ok, summary, "
            "source) VALUES (?,?,?,?,?,?)",
            (op, started_at or now, finished_at or now, int(ok), summary, source),
        )
        return int(cur.lastrowid)

    def last_run(self, op: str) -> sqlite3.Row | None:
        """The most recent run of ``op``, or None if it has never run."""
        return self.conn.execute(
            "SELECT * FROM operation_run WHERE op = ? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (op,),
        ).fetchone()

    def last_runs(self) -> dict[str, sqlite3.Row]:
        """Latest run per operation, in one pass (the window needs them all)."""
        rows = self.conn.execute(
            "SELECT r.* FROM operation_run r JOIN ("
            "  SELECT op, MAX(started_at) AS at, MAX(id) AS rid"
            "  FROM operation_run GROUP BY op"
            ") m ON r.op = m.op AND r.id = m.rid"
        ).fetchall()
        return {row["op"]: row for row in rows}

    def recent(self, limit: int = 25) -> list[sqlite3.Row]:
        """Newest runs first — the Activity list."""
        return self.conn.execute(
            "SELECT * FROM operation_run ORDER BY started_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def prune(self, keep: int = 500) -> int:
        """Drop all but the ``keep`` newest rows; returns how many were removed.

        Mirrors the keep-10 backup rotation: the log is a convenience, not a
        record to retain forever.
        """
        cur = self.conn.execute(
            "DELETE FROM operation_run WHERE id NOT IN ("
            "  SELECT id FROM operation_run ORDER BY started_at DESC, id DESC LIMIT ?"
            ")",
            (keep,),
        )
        return cur.rowcount or 0


class CalendarEntryRepository:
    """CRUD for user-created personal calendar entries (calendar_entry)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _to_model(row: sqlite3.Row) -> CalendarEntry:
        return CalendarEntry(
            id=row["id"],
            title=row["title"],
            kind=row["kind"],
            starts_on=row["starts_on"],
            ends_on=row["ends_on"],
            all_day=bool(row["all_day"]),
            start_time=row["start_time"],
            color=row["color"],
            notes=row["notes"],
            created_at=row["created_at"],
        )

    def add(self, entry: CalendarEntry) -> CalendarEntry:
        entry.created_at = entry.created_at or now_iso()
        cur = self.conn.execute(
            "INSERT INTO calendar_entry (title, kind, starts_on, ends_on, all_day, "
            "start_time, color, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                entry.title,
                entry.kind,
                entry.starts_on,
                entry.ends_on,
                int(entry.all_day),
                entry.start_time,
                entry.color,
                entry.notes,
                entry.created_at,
            ),
        )
        entry.id = cur.lastrowid
        return entry

    def update(self, entry: CalendarEntry) -> CalendarEntry:
        self.conn.execute(
            "UPDATE calendar_entry SET title=?, kind=?, starts_on=?, ends_on=?, "
            "all_day=?, start_time=?, color=?, notes=? WHERE id=?",
            (
                entry.title,
                entry.kind,
                entry.starts_on,
                entry.ends_on,
                int(entry.all_day),
                entry.start_time,
                entry.color,
                entry.notes,
                entry.id,
            ),
        )
        return entry

    def delete(self, entry_id: int) -> None:
        self.conn.execute("DELETE FROM calendar_entry WHERE id = ?", (entry_id,))

    def get(self, entry_id: int) -> CalendarEntry | None:
        row = self.conn.execute(
            "SELECT * FROM calendar_entry WHERE id = ?", (entry_id,)
        ).fetchone()
        return self._to_model(row) if row else None

    def list_overlapping(self, start: str, end: str) -> list[CalendarEntry]:
        """Entries that intersect the inclusive [start, end] window."""
        rows = self.conn.execute(
            "SELECT * FROM calendar_entry WHERE ends_on >= ? AND starts_on <= ? "
            "ORDER BY starts_on, ends_on, id",
            (start, end),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    def list_all(self) -> list[CalendarEntry]:
        rows = self.conn.execute(
            "SELECT * FROM calendar_entry ORDER BY starts_on, id"
        ).fetchall()
        return [self._to_model(r) for r in rows]
