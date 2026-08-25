"""CSV import/export (Qt-free).

Two families of export:
- **Human-readable** flat CSVs of items / inventory / transactions (F-IE-1),
  money rendered in dollars — for opening in a spreadsheet, not re-import.
- **Full dump/restore** (``export_all`` / ``import_all``) — every table written
  verbatim so the dataset round-trips exactly (F-IE-4, NFR-5).

Import:
- ``preview_import`` parses + validates a CSV (header-based column mapping) and
  returns parsed rows plus per-row errors so the UI can preview before writing.
- ``import_items`` (F-IE-2) match-or-creates items; ``import_purchases``
  (F-IE-3) posts one single-card purchase per row.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from tcginventory.data.repositories import ItemRepository
from tcginventory.domain.enums import (
    Category,
    Condition,
    GradingCompany,
    Intent,
    Language,
)
from tcginventory.domain.models import Item
from tcginventory.services.inventory import InventoryService
from tcginventory.services.reporting import ReportingService
from tcginventory.services.transactions import (
    IncomingCard,
    TransactionRequest,
    TransactionService,
)

# Every table, in FK-dependency order (parents before children).
_ALL_TABLES = [
    "item",
    "transaction",
    "transaction_line",
    "lot",
    "lot_consumption",
    "audit_log",
    "price_snapshot",
    "schema_version",
]

# CSV header (lower-cased) → item/purchase field.
_FIELD_ALIASES = {
    "name": "name",
    "card name": "name",
    "set": "set_name",
    "set name": "set_name",
    "set code": "set_code",
    "number": "card_number",
    "card number": "card_number",
    "card #": "card_number",
    "#": "card_number",
    "collector number": "card_number",
    "variant": "variant",
    "printing": "variant",
    "language": "language",
    "lang": "language",
    "condition": "condition",
    "cond": "condition",
    "graded": "is_graded",
    "is graded": "is_graded",
    "grading company": "grading_company",
    "company": "grading_company",
    "grade": "grade",
    "cert": "cert_number",
    "cert #": "cert_number",
    "cert number": "cert_number",
    "location": "location",
    "market value": "market_value",
    "market": "market_value",
    "value": "market_value",
    "tcgplayer": "tcgplayer_url",
    "tcgplayer url": "tcgplayer_url",
    "ebay": "ebay_url",
    "ebay url": "ebay_url",
    "notes": "notes",
    "category": "category",
    "price": "price",
    "total price": "price",
    "cost": "price",
    "intent": "intent",
    "date": "date",
    "purchase date": "date",
    "acquired": "date",
}


def _dollars(cents: int | None) -> str:
    if cents is None:
        return ""
    negative = cents < 0
    whole, frac = divmod(abs(cents), 100)
    return f"{'-' if negative else ''}{whole}.{frac:02d}"


# ---------------------------------------------------------------------------
# Human-readable exports (F-IE-1)
# ---------------------------------------------------------------------------


def _write_csv(path: Path, headers: list[str], rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def export_items_csv(conn, path) -> Path:
    path = Path(path)
    headers = [
        "name",
        "set_name",
        "set_code",
        "card_number",
        "variant",
        "language",
        "condition",
        "holo_pattern",
        "edition",
        "is_graded",
        "grading_company",
        "grade",
        "cert_number",
        "location",
        "market_value",
        "market_value_asof",
        "tcgplayer_url",
        "ebay_url",
        "notes",
    ]
    rows = []
    for item in ItemRepository(conn).list_all():
        rows.append(
            [
                item.name,
                item.set_name or "",
                item.set_code or "",
                item.card_number or "",
                item.variant or "",
                item.language.value,
                item.condition.value if item.condition else "",
                item.holo_pattern.value if item.holo_pattern else "",
                item.edition.value if item.edition else "",
                "yes" if item.is_graded else "no",
                item.grading_company.value if item.grading_company else "",
                item.grade or "",
                item.cert_number or "",
                item.location or "",
                _dollars(item.market_value_cents),
                item.market_value_asof or "",
                item.tcgplayer_url or "",
                item.ebay_url or "",
                item.notes or "",
            ]
        )
    _write_csv(path, headers, rows)
    return path


def export_inventory_csv(conn, path) -> Path:
    """One row per lot (per SKU); per-unit money."""
    path = Path(path)
    headers = [
        "sku",
        "name",
        "set_name",
        "card_number",
        "variant",
        "language",
        "cond_grade",
        "intent",
        "acquired_at",
        "on_hand",
        "purchase_price",
        "market_at_purchase",
        "current_market",
        "unrealized",
        "sold_status",
    ]
    rows = []
    for row in InventoryService(conn).inventory_rows():
        item = row.item
        if item.is_graded:
            company = item.grading_company.value if item.grading_company else "?"
            cond_grade = f"{company} {item.grade or ''}".strip()
        else:
            cond_grade = item.condition.value if item.condition else ""
        rows.append(
            [
                row.sku,
                item.name,
                item.set_name or "",
                item.card_number or "",
                item.variant or "",
                item.language.value,
                cond_grade,
                row.intent,
                row.acquired_at,
                row.on_hand,
                _dollars(row.purchase_price_cents),
                _dollars(row.market_at_purchase_cents),
                _dollars(row.current_price_cents),
                _dollars(row.unrealized_cents),
                row.sold_status,
            ]
        )
    _write_csv(path, headers, rows)
    return path


def export_transactions_csv(conn, path) -> Path:
    from tcginventory.data.repositories import TransactionRepository

    path = Path(path)
    reporting = ReportingService(conn)
    headers = [
        "id",
        "occurred_at",
        "type",
        "counterparty",
        "reference",
        "cash_delta",
        "margin",
        "status",
    ]
    rows = []
    for txn in TransactionRepository(conn).list_all():
        proceeds = reporting.transaction_proceeds_cents(txn.id)
        margin = reporting.transaction_margin_cents(txn.id) if proceeds else None
        rows.append(
            [
                txn.id,
                txn.occurred_at,
                txn.type.value,
                txn.counterparty or "",
                txn.reference or "",
                _dollars(txn.cash_delta_cents),
                _dollars(margin),
                txn.status.value,
            ]
        )
    _write_csv(path, headers, rows)
    return path


# ---------------------------------------------------------------------------
# Full dump / restore (F-IE-4, NFR-5) — exact round-trip
# ---------------------------------------------------------------------------


def _columns(conn, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def export_all(conn, directory) -> Path:
    """Dump every table to ``<table>.csv`` verbatim (portable full backup)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for table in _ALL_TABLES:
        cols = _columns(conn, table)
        select = ", ".join(f'"{c}"' for c in cols)
        db_rows = conn.execute(f'SELECT {select} FROM "{table}"').fetchall()
        rows = [["" if r[c] is None else r[c] for c in cols] for r in db_rows]
        _write_csv(directory / f"{table}.csv", cols, rows)
    return directory


def import_all(conn, directory) -> None:
    """Restore a full dump: wipe every table, then load verbatim. Atomic.

    Empty CSV cells load as SQL NULL (repos never store empty strings, so this
    round-trips our data faithfully). Integer columns rely on SQLite affinity to
    convert the textual CSV values back to integers.
    """
    directory = Path(directory)
    try:
        conn.execute("BEGIN")
        for table in reversed(_ALL_TABLES):
            conn.execute(f'DELETE FROM "{table}"')
        for table in _ALL_TABLES:
            path = directory / f"{table}.csv"
            if not path.exists():
                continue
            with open(path, newline="", encoding="utf-8") as handle:
                reader = csv.reader(handle)
                headers = next(reader, None)
                if not headers:
                    continue
                collist = ", ".join(f'"{h}"' for h in headers)
                placeholders = ", ".join("?" * len(headers))
                sql = f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})'
                for row in reader:
                    conn.execute(sql, [None if v == "" else v for v in row])
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Import (F-IE-2 / F-IE-3)
# ---------------------------------------------------------------------------


@dataclass
class ParsedRow:
    row_num: int
    item: Item
    market_value_cents: int | None
    price_cents: int | None
    intent: Intent
    date: str | None


@dataclass
class ImportPreview:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return len(self.rows)

    @property
    def error_count(self) -> int:
        return len(self.errors)


@dataclass
class ImportResult:
    created: int = 0
    matched: int = 0
    posted: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_money(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    return round(float(raw.replace("$", "").replace(",", "").strip()) * 100)


def _match_enum(enum_cls, raw: str | None):
    if raw is None or not raw.strip():
        return None, None
    for member in enum_cls:
        if member.value.lower() == raw.strip().lower():
            return member, None
    return None, f"invalid {enum_cls.__name__.lower()} '{raw.strip()}'"


def _truthy(raw: str | None) -> bool:
    return bool(raw) and raw.strip().lower() in {"yes", "y", "true", "1"}


def preview_import(path, mode: str = "items") -> ImportPreview:
    """Parse and validate a CSV. ``mode`` is 'items' or 'purchases'."""
    preview = ImportPreview()
    path = Path(path)
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        raw_headers = next(reader, None)
        if not raw_headers:
            preview.errors.append("file is empty")
            return preview
        mapping = {
            i: _FIELD_ALIASES.get(h.strip().lower()) for i, h in enumerate(raw_headers)
        }
        if "name" not in mapping.values():
            preview.errors.append("no recognised 'name' column in header")
            return preview

        for line_no, raw in enumerate(reader, start=2):
            fields = {
                mapping[i]: raw[i]
                for i in range(len(raw))
                if i in mapping and mapping[i] is not None
            }
            parsed, row_errors = _parse_row(line_no, fields, mode)
            if row_errors:
                preview.errors.extend(row_errors)
            elif parsed is not None:
                preview.rows.append(parsed)
    return preview


def _parse_row(line_no, fields, mode):
    errors = []
    name = (fields.get("name") or "").strip()
    if not name:
        return None, [f"row {line_no}: missing name"]

    def note(err):
        if err:
            errors.append(f"row {line_no}: {err}")

    category, err = _match_enum(Category, fields.get("category"))
    note(err)
    language, err = _match_enum(Language, fields.get("language"))
    note(err)
    graded = _truthy(fields.get("is_graded"))
    if graded:
        condition = None
        company, err = _match_enum(GradingCompany, fields.get("grading_company"))
        note(err)
    else:
        company = None
        condition, err = _match_enum(Condition, fields.get("condition"))
        note(err)
    intent, err = _match_enum(Intent, fields.get("intent"))
    note(err)

    try:
        market = _parse_money(fields.get("market_value"))
    except ValueError:
        market = None
        errors.append(f"row {line_no}: bad market value '{fields.get('market_value')}'")
    try:
        price = _parse_money(fields.get("price"))
    except ValueError:
        price = None
        errors.append(f"row {line_no}: bad price '{fields.get('price')}'")

    if mode == "purchases":
        if not price:
            errors.append(f"row {line_no}: purchase needs a positive price")
        if not market:
            errors.append(f"row {line_no}: purchase needs a positive market value")
        if not (fields.get("date") or "").strip():
            errors.append(f"row {line_no}: purchase needs a date")

    if errors:
        return None, errors

    grade_val = (fields.get("grade") or "").strip() or None
    cert_val = (fields.get("cert_number") or "").strip() or None
    item = Item(
        name=name,
        category=category or Category.SINGLE,
        set_name=(fields.get("set_name") or "").strip() or None,
        set_code=(fields.get("set_code") or "").strip() or None,
        card_number=(fields.get("card_number") or "").strip() or None,
        variant=(fields.get("variant") or "").strip() or None,
        language=language or Language.EN,
        condition=condition,
        is_graded=graded,
        grading_company=company,
        grade=grade_val if graded else None,
        cert_number=cert_val if graded else None,
        location=(fields.get("location") or "").strip() or None,
        tcgplayer_url=(fields.get("tcgplayer_url") or "").strip() or None,
        ebay_url=(fields.get("ebay_url") or "").strip() or None,
        market_value_cents=market,
        notes=(fields.get("notes") or "").strip() or None,
    )
    return (
        ParsedRow(
            row_num=line_no,
            item=item,
            market_value_cents=market,
            price_cents=price,
            intent=intent or Intent.RESALE,
            date=(fields.get("date") or "").strip() or None,
        ),
        [],
    )


def import_items(conn, path) -> ImportResult:
    """Match-or-create items from a CSV (F-IE-2). Atomic."""
    preview = preview_import(path, mode="items")
    result = ImportResult(errors=list(preview.errors))
    items = ItemRepository(conn)
    try:
        conn.execute("BEGIN")
        for row in preview.rows:
            existing = items.find_by_identity(row.item)
            if existing is not None:
                result.matched += 1
            else:
                items.add(row.item)
                result.created += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


def import_purchases(conn, path) -> ImportResult:
    """Post one single-card purchase per CSV row (F-IE-3). Each posts atomically."""
    preview = preview_import(path, mode="purchases")
    result = ImportResult(errors=list(preview.errors))
    service = TransactionService(conn)
    for row in preview.rows:
        service.post(
            TransactionRequest(
                occurred_at=row.date,
                cash_out_cents=row.price_cents,
                incoming=[
                    IncomingCard(
                        item=row.item,
                        quantity=1,
                        market_value_cents=row.market_value_cents,
                        intent=row.intent,
                    )
                ],
            )
        )
        result.posted += 1
    return result
