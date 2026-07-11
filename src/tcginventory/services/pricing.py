"""Live market pricing via tcgcsv.com (Qt-free).

tcgcsv mirrors TCGplayer's own pricing API as free JSON, keyed by TCGplayer
**product ID** — which we parse from the item's saved ``tcgplayer_url``. Prices
are product-level market prices (≈ Near Mint, English); condition/JP fidelity is
out of scope for this source.

A refresh resolves each item's set → tcgcsv group, downloads that group's price
file once, joins by product ID, then writes a ``price_snapshot`` per item and
updates the item's current market value. Network I/O is injected (``fetch_json``)
so the service is testable offline.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import ItemRepository, PriceSnapshotRepository
from tcginventory.domain.models import Item, PriceSnapshot

_CATEGORY_POKEMON = 3
_BASE_URL = "https://tcgcsv.com/tcgplayer"
_PRODUCT_RE = re.compile(r"/product/(\d+)")


def _httpx_get_json(url: str):  # pragma: no cover - real network
    import httpx

    resp = httpx.get(url, timeout=20.0, headers={"User-Agent": "TCGinventory/1.0"})
    resp.raise_for_status()
    return resp.json()


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


@dataclass
class PriceQuote:
    item_id: int
    price_cents: int
    source: str = "tcgcsv"


@dataclass
class RefreshSummary:
    priced: int = 0
    unmapped: list[str] = field(default_factory=list)  # item names with no price
    errors: list[str] = field(default_factory=list)
    as_of: str = ""


class TcgcsvProvider:
    """Resolve TCGplayer market prices for items from tcgcsv.com."""

    source = "tcgcsv"

    def __init__(self, fetch_json: Callable[[str], object] | None = None) -> None:
        self._fetch = fetch_json or _httpx_get_json
        self._group_index: dict[str, int] | None = None
        self._price_cache: dict[int, dict[int, int]] = {}

    def quotes_for(self, items: list[Item]) -> list[PriceQuote]:
        # Group items by their resolved tcgcsv group so each price file is
        # downloaded at most once.
        by_group: dict[int, list[tuple[Item, int]]] = {}
        for item in items:
            product_id = self._product_id(item)
            group_id = self._group_id(item)
            if product_id is None or group_id is None:
                continue
            by_group.setdefault(group_id, []).append((item, product_id))

        quotes: list[PriceQuote] = []
        for group_id, entries in by_group.items():
            prices = self._prices_for_group(group_id)
            for item, product_id in entries:
                cents = prices.get(product_id)
                if cents is not None:
                    quotes.append(PriceQuote(item_id=item.id, price_cents=cents))
        return quotes

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _product_id(item: Item) -> int | None:
        if not item.tcgplayer_url:
            return None
        match = _PRODUCT_RE.search(item.tcgplayer_url)
        return int(match.group(1)) if match else None

    def _group_id(self, item: Item) -> int | None:
        index = self._groups()
        for key in (item.set_code, item.set_name):
            if key:
                group_id = index.get(_norm(key))
                if group_id is not None:
                    return group_id
        return None

    def _groups(self) -> dict[str, int]:
        if self._group_index is None:
            data = self._fetch(f"{_BASE_URL}/{_CATEGORY_POKEMON}/groups")
            index: dict[str, int] = {}
            for grp in _results(data):
                gid = grp.get("groupId")
                if gid is None:
                    continue
                for key in (grp.get("abbreviation"), grp.get("name")):
                    norm = _norm(key)
                    if norm:
                        index.setdefault(norm, gid)
            self._group_index = index
        return self._group_index

    def _prices_for_group(self, group_id: int) -> dict[int, int]:
        if group_id not in self._price_cache:
            data = self._fetch(f"{_BASE_URL}/{_CATEGORY_POKEMON}/{group_id}/prices")
            prices: dict[int, int] = {}
            for row in _results(data):
                pid = row.get("productId")
                market = row.get("marketPrice")
                if pid is None or market is None:
                    continue
                cents = round(float(market) * 100)
                # A product may list several sub-types; keep the highest market.
                prices[pid] = max(prices.get(pid, 0), cents)
            self._price_cache[group_id] = prices
        return self._price_cache[group_id]


def _results(data) -> list:
    if isinstance(data, dict):
        return data.get("results") or []
    return data or []


class PricingService:
    def __init__(self, conn: sqlite3.Connection, provider=None) -> None:
        self.conn = conn
        self.items = ItemRepository(conn)
        self.snapshots = PriceSnapshotRepository(conn)
        self._provider = provider or TcgcsvProvider()

    def refresh(
        self, items: list[Item] | None = None, as_of: str | None = None
    ) -> RefreshSummary:
        """Fetch prices and persist snapshots + current values. Atomic write."""
        items = items if items is not None else self.items.list_all()
        as_of = as_of or now_iso()
        summary = RefreshSummary(as_of=as_of)

        try:
            quotes = self._provider.quotes_for(items)
        except Exception as exc:  # network / parse failures never crash the app
            summary.errors.append(str(exc))
            return summary

        priced_ids = {q.item_id for q in quotes}
        summary.unmapped = [i.name for i in items if i.id not in priced_ids]

        try:
            for quote in quotes:
                self.snapshots.add(
                    PriceSnapshot(
                        item_id=quote.item_id,
                        price_cents=quote.price_cents,
                        source=quote.source,
                        as_of=as_of,
                    )
                )
                # History is always recorded; a reviewed item's shown current
                # value is user-maintained, so don't overwrite it.
                item = self.items.get(quote.item_id)
                if item is not None and not item.review:
                    item.market_value_cents = quote.price_cents
                    item.market_value_asof = as_of[:10]
                    self.items.update(item)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        summary.priced = len(quotes)
        return summary
