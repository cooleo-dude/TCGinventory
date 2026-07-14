"""Live market pricing via tcgcsv.com (Qt-free).

tcgcsv mirrors TCGplayer's own pricing API as free JSON, keyed by TCGplayer
**product ID** — which we parse from the item's saved ``tcgplayer_url``. Prices
are product-level market prices (≈ Near Mint); condition fidelity is out of scope
for this source. English and Japanese cards are both supported, resolved against
tcgcsv category 3 ("Pokemon") or 85 ("Pokemon Japan") by the item's language.

A refresh resolves each item's (category, set) → tcgcsv group, downloads that
group's price file once, joins by product ID, then writes a ``price_snapshot``
per item and updates the item's current market value. Network I/O is injected
(``fetch_json``) so the service is testable offline.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import ItemRepository, PriceSnapshotRepository
from tcginventory.domain.enums import Language
from tcginventory.domain.models import Item, PriceSnapshot

_CATEGORY_POKEMON = 3  # tcgcsv English "Pokemon"
_CATEGORY_POKEMON_JP = 85  # tcgcsv "Pokemon Japan"
_BASE_URL = "https://tcgcsv.com/tcgplayer"
_PRODUCT_RE = re.compile(r"/product/(\d+)")
# Minimum similarity for the closest-match set fallback. Kept modest because
# the price join is by product ID: a wrong group simply yields no price (never
# a wrong one), so leaning in favours resolving real sets over false misses.
_FUZZY_THRESHOLD = 0.6


def _httpx_get_json(url: str):  # pragma: no cover - real network
    import httpx

    resp = httpx.get(url, timeout=20.0, headers={"User-Agent": "TCGinventory/1.0"})
    resp.raise_for_status()
    return resp.json()


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _norm_number(number: str | None) -> str:
    """Collector number for matching: head before '/', leading zeros stripped.

    e.g. '163/159' -> '163', '074/073' -> '74', '5' -> '5'. tcgcsv products
    store 'NNN/TTT'; the catalog stores the bare head.
    """
    head = (number or "").split("/", 1)[0].strip().lower()
    return head.lstrip("0") or head


# pokemontcg.io names promo sets systematically differently than tcgcsv
# ("<era> Black Star Promos" vs tcgcsv's era-specific promo group), and the
# strings are too far apart to fuzzy-match. Bridge them by normalized set name.
_PROMO_SET_ALIASES = {
    "swshblackstarpromos": "Sword & Shield Promo Cards",
    "smblackstarpromos": "SM Promos",
    "xyblackstarpromos": "XY Promos",
    "scarletvioletblackstarpromos": "Scarlet & Violet Promo Cards",
    "nintendoblackstarpromos": "Nintendo Promos",
    "wizardsblackstarpromos": "WoTC Promo",
    "hgssblackstarpromos": "HGSS Promos",
    "dpblackstarpromos": "Diamond and Pearl Promos",
    "bwblackstarpromos": "Black and White Promos",
}


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
        # Caches are keyed by tcgcsv category (3 = English, 85 = Japanese) so a
        # single provider serves both. group_ids are globally unique, but the
        # per-category price/product files live at different URLs.
        self._group_cache: dict[int, tuple[dict[str, int], list[tuple[str, int]]]] = {}
        self._price_cache: dict[tuple[int, int], dict[int, int]] = {}
        self._product_cache: dict[tuple[int, int], list[tuple[int, str, str]]] = {}

    @staticmethod
    def _category_for(item: Item) -> int:
        if item.language is Language.JP:
            return _CATEGORY_POKEMON_JP
        return _CATEGORY_POKEMON

    def price_for_card(
        self,
        set_name: str,
        number: str,
        name: str | None = None,
        category: int = _CATEGORY_POKEMON,
    ) -> tuple[int, str] | None:
        """Current market price for a card identified by set + collector number.

        Resolves the set's tcgcsv group (in the given category — 3 English, 85
        Japanese), finds the product whose number matches, and returns
        ``(price_cents, tcgplayer_url)``. Returns None if the set/product/price
        can't be resolved. The URL lets the caller store it so future refreshes
        join by product id.
        """
        group_id = self._resolve_group(set_name, None, category)
        if group_id is None:
            return None
        want = _norm_number(number)
        prices = self._prices_for_group(category, group_id)
        for product_id, prod_number, url in self._products_for_group(
            category, group_id
        ):
            if _norm_number(prod_number) == want:
                cents = prices.get(product_id)
                if cents is not None:
                    return cents, url
        return None

    def _products_for_group(
        self, category: int, group_id: int
    ) -> list[tuple[int, str, str]]:
        key = (category, group_id)
        if key not in self._product_cache:
            data = self._fetch(f"{_BASE_URL}/{category}/{group_id}/products")
            products: list[tuple[int, str, str]] = []
            for row in _results(data):
                pid = row.get("productId")
                if pid is None:
                    continue
                number = ""
                for ext in row.get("extendedData") or []:
                    if ext.get("name") == "Number":
                        number = ext.get("value") or ""
                products.append((pid, number, row.get("url") or ""))
            self._product_cache[key] = products
        return self._product_cache[key]

    def quotes_for(self, items: list[Item]) -> list[PriceQuote]:
        # Group items by their (category, resolved group) so each price file is
        # downloaded at most once.
        by_group: dict[tuple[int, int], list[tuple[Item, int]]] = {}
        for item in items:
            product_id = self._product_id(item)
            category = self._category_for(item)
            group_id = self._group_id(item)
            if product_id is None or group_id is None:
                continue
            by_group.setdefault((category, group_id), []).append((item, product_id))

        quotes: list[PriceQuote] = []
        for (category, group_id), entries in by_group.items():
            prices = self._prices_for_group(category, group_id)
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
        return self._resolve_group(
            item.set_name, item.set_code, self._category_for(item)
        )

    def _resolve_group(
        self, set_name: str | None, set_code: str | None, category: int
    ) -> int | None:
        index, candidates = self._groups(category)
        # 1. Exact (normalized) match on set_code then set_name.
        for key in (set_code, set_name):
            if key:
                group_id = index.get(_norm(key))
                if group_id is not None:
                    return group_id
        # 1b. Known promo-set aliases (pokemontcg.io vs tcgcsv naming).
        alias = _PROMO_SET_ALIASES.get(_norm(set_name))
        if alias:
            group_id = index.get(_norm(alias))
            if group_id is not None:
                return group_id
        # 2. Closest-match fallback. Set names are recorded inconsistently
        #    ("Journey Together" vs tcgcsv's "SV09: Journey Together"), so pick
        #    the best fuzzy match. A substring hit is treated as a strong match.
        best_gid, best_score = None, 0.0
        for key in (set_name, set_code):
            nkey = _norm(key)
            if len(nkey) < 3:
                continue
            for cand_norm, gid in candidates:
                score = difflib.SequenceMatcher(None, nkey, cand_norm).ratio()
                if len(nkey) >= 4 and (nkey in cand_norm or cand_norm in nkey):
                    score = max(score, 0.9)
                if score > best_score:
                    best_gid, best_score = gid, score
        return best_gid if best_score >= _FUZZY_THRESHOLD else None

    def _groups(
        self, category: int
    ) -> tuple[dict[str, int], list[tuple[str, int]]]:
        if category not in self._group_cache:
            data = self._fetch(f"{_BASE_URL}/{category}/groups")
            index: dict[str, int] = {}
            candidates: list[tuple[str, int]] = []
            for grp in _results(data):
                gid = grp.get("groupId")
                if gid is None:
                    continue
                name = grp.get("name")
                keys = [grp.get("abbreviation"), name]
                # tcgcsv prefixes set names with the code, e.g.
                # "SV09: Journey Together" — also index the bare name so a
                # plain "Journey Together" (no set_code) still resolves.
                if name and ":" in name:
                    keys.append(name.split(":", 1)[1])
                for key in keys:
                    norm = _norm(key)
                    if norm:
                        index.setdefault(norm, gid)
                        candidates.append((norm, gid))
            self._group_cache[category] = (index, candidates)
        return self._group_cache[category]

    def _prices_for_group(self, category: int, group_id: int) -> dict[int, int]:
        key = (category, group_id)
        if key not in self._price_cache:
            data = self._fetch(f"{_BASE_URL}/{category}/{group_id}/prices")
            prices: dict[int, int] = {}
            for row in _results(data):
                pid = row.get("productId")
                market = row.get("marketPrice")
                if pid is None or market is None:
                    continue
                cents = round(float(market) * 100)
                # A product may list several sub-types; keep the highest market.
                prices[pid] = max(prices.get(pid, 0), cents)
            self._price_cache[key] = prices
        return self._price_cache[key]


def _results(data) -> list:
    if isinstance(data, dict):
        return data.get("results") or []
    return data or []


# ---------------------------------------------------------------------------
# Alternate provider (viability experiment): pokemontcg.io
# ---------------------------------------------------------------------------
# Same PriceQuote/quotes_for contract as TcgcsvProvider, so it can be injected
# into PricingService as a drop-in override for testing/comparison — tcgcsv
# stays the default. pokemontcg.io is Pokémon-native and carries TCGplayer
# prices per card, matched on set name + collector number. Network I/O is
# injected so it's testable offline.

_POKETCG_URL = "https://api.pokemontcg.io/v2/cards"
# Preference order for which TCGplayer printing's price to use.
_POKETCG_VARIANTS = (
    "holofoil",
    "normal",
    "reverseHolofoil",
    "1stEditionHolofoil",
    "unlimitedHolofoil",
    "1stEditionNormal",
)


def _poketcg_get(url: str):  # pragma: no cover - real network
    import time

    import httpx

    headers = {"User-Agent": "TCGinventory/1.0", "Accept": "application/json"}
    last: Exception | None = None
    for _ in range(4):  # the endpoint (Cloudflare) is flaky: 504s / timeouts
        try:
            resp = httpx.get(url, timeout=40.0, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            last = RuntimeError(f"HTTP {resp.status_code}")
        except Exception as exc:
            last = exc
        time.sleep(1.5)
    raise last or RuntimeError("pokemontcg.io fetch failed")


def _poketcg_number(card_number: str | None) -> str:
    """'074/073' -> '74' (pokemontcg.io stores the bare collector number)."""
    if not card_number:
        return ""
    head = card_number.split("/", 1)[0].strip()
    return head.lstrip("0") or head


class PokemonTcgProvider:
    """Resolve TCGplayer prices for items via pokemontcg.io (Pokémon-native)."""

    source = "pokemontcg.io"

    def __init__(
        self,
        fetch_json: Callable[[str], object] | None = None,
        api_key: str | None = None,
    ) -> None:
        self._fetch = fetch_json or _poketcg_get
        self._api_key = api_key

    def quotes_for(self, items: list[Item]) -> list[PriceQuote]:
        quotes: list[PriceQuote] = []
        for item in items:
            query = self._query(item)
            if not query:
                continue
            try:
                data = self._fetch(f"{_POKETCG_URL}?q={query}&pageSize=8")
            except Exception:
                continue  # per-item failure never aborts the batch
            card = self._best_match(_poketcg_cards(data), item)
            cents = self._price_cents(card) if card else None
            if cents is not None:
                quotes.append(
                    PriceQuote(item_id=item.id, price_cents=cents, source=self.source)
                )
        return quotes

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _query(item: Item) -> str:
        """Lucene query — set name + collector number is unique within a set;
        fall back to the name when identity is thin."""
        from urllib.parse import quote

        parts = []
        number = _poketcg_number(item.card_number)
        if number:
            parts.append(f"number:{number}")
        if item.set_name:
            parts.append(f'set.name:"{item.set_name}"')
        if not parts and item.name:
            parts.append(f'name:"{item.name}"')
        return quote(" ".join(parts)) if parts else ""

    @staticmethod
    def _best_match(cards: list, item: Item):
        if not cards:
            return None
        # Prefer a card whose name contains the item's name (handles owner
        # prefixes like "Iono's Kilowattrel" vs stored "Kilowattrel").
        wanted = _norm(item.name)
        for card in cards:
            if wanted and wanted in _norm(card.get("name")):
                return card
        return cards[0]

    @staticmethod
    def _price_cents(card) -> int | None:
        prices = ((card or {}).get("tcgplayer") or {}).get("prices") or {}
        for variant in _POKETCG_VARIANTS:
            p = prices.get(variant) or {}
            if p.get("market") is not None:
                return round(float(p["market"]) * 100)
        # Fallback: any variant, any of market/mid/low.
        for p in prices.values():
            for key in ("market", "mid", "low"):
                if p.get(key) is not None:
                    return round(float(p[key]) * 100)
        return None


def _poketcg_cards(data) -> list:
    if isinstance(data, dict):
        return data.get("data") or []
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
