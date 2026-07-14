"""Domain dataclasses mirroring the SQLite schema (see specs/02-data-model.md).

Pure data holders — no Qt, no SQL. Money is always integer **cents**; dates and
datetimes are ISO-8601 strings. ``id`` is ``None`` until a row is persisted, and
DB-managed timestamps (``created_at`` / ``updated_at``) are filled in by the
repositories on insert.
"""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass
class Item:
    """A distinct sellable thing — a card (condition/grade) or a sealed product."""

    name: str
    category: Category
    game: str = "pokemon"
    set_name: str | None = None
    set_code: str | None = None
    card_number: str | None = None  # "60/107", "GG09/GG12"
    language: Language = Language.EN
    condition: Condition | None = None  # NULL when graded
    is_graded: bool = False
    grading_company: GradingCompany | None = None
    grade: str | None = None
    cert_number: str | None = None
    location: str | None = None
    tcgplayer_url: str | None = None
    ebay_url: str | None = None
    market_value_cents: int | None = None
    market_value_asof: str | None = None
    review: bool = False  # marked for review: current price is manual, poll-protected
    notes: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    id: int | None = None


@dataclass
class Lot:
    """A batch of one item acquired in one event at one unit cost. Basis lives here."""

    item_id: int
    acquired_at: str
    quantity: int
    remaining_qty: int
    unit_cost_cents: int
    acquired_market_value_cents: int
    intent: Intent
    sku: str | None = None  # auto-generated per-copy lookup SKU
    acquired_txn_line_id: int | None = None
    source: str | None = None
    notes: str | None = None
    created_at: str | None = None
    id: int | None = None


@dataclass
class Transaction:
    """A bidirectional exchange (purchase / sale / trade / adjustment)."""

    type: TransactionType
    occurred_at: str
    counterparty: str | None = None
    reference: str | None = None
    cash_delta_cents: int = 0
    status: TransactionStatus = TransactionStatus.POSTED
    voided_at: str | None = None
    notes: str | None = None
    created_at: str | None = None
    id: int | None = None


@dataclass
class TransactionLine:
    """Per-item detail of a transaction; ``direction`` is in (received) or out."""

    transaction_id: int
    item_id: int
    direction: Direction
    quantity: int
    unit_price_cents: int
    realized_pl_cents: int | None = None  # set for 'out' lines at post time
    notes: str | None = None
    id: int | None = None


@dataclass
class LotConsumption:
    """Records which lot an 'out' line drew down (FIFO), for auditable cost basis."""

    transaction_line_id: int
    lot_id: int
    quantity: int
    unit_cost_cents: int  # copied from the lot at disposal time
    id: int | None = None


@dataclass
class PriceSnapshot:
    """A polled market price for an item at a point in time (per unit)."""

    item_id: int
    price_cents: int
    source: str  # e.g. "tcgcsv"
    as_of: str  # ISO datetime of the poll
    currency: str = "USD"
    note: str | None = None
    id: int | None = None


@dataclass
class CardShow:
    """An upcoming card/TCG show scraped from tcdb.com (Dashboard calendar).

    ``source_id`` is tcdb's own show ID (the upsert key). ``starts_on`` is an ISO
    date; times/venue are free text as tcdb presents them.
    """

    source_id: int
    name: str
    state: str  # 2-letter code
    starts_on: str  # ISO date YYYY-MM-DD
    venue: str | None = None
    city: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    detail_url: str | None = None
    fetched_at: str | None = None
    # user attendance status: denied | pending | attending | walking
    status: str | None = None
    id: int | None = None


@dataclass
class CatalogCard:
    """A reference card from the local catalog (pokemontcg.io dataset).

    Read-only identity data used for autocomplete / set-number auto-fill.
    """

    card_id: str
    name: str
    set_id: str
    set_name: str | None = None
    set_code: str | None = None
    number: str | None = None
    rarity: str | None = None
    supertype: str | None = None
    image_small: str | None = None
    printed_total: int | None = None

    @property
    def full_number(self) -> str | None:
        """Collector number with the set's denominator, matching the numerator's
        prefix — e.g. "SV103" + total 122 → "SV103/SV122"; "124" + 142 →
        "124/142". Falls back to the bare number when the total is unknown."""
        if not self.number:
            return None
        if not self.printed_total:
            return self.number
        prefix = ""
        for ch in self.number:
            if ch.isdigit():
                break
            prefix += ch
        return f"{self.number}/{prefix}{self.printed_total}"


@dataclass
class AuditLogEntry:
    """An append-only record of a create / edit / void on some entity."""

    entity: str
    entity_id: int
    action: AuditAction
    at: str | None = None
    before_json: str | None = None
    after_json: str | None = None
    note: str | None = None
    id: int | None = None
