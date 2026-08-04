"""Build a TCGplayer product link that reflects a card's fields (Qt-free).

The stored ``tcgplayer_url`` is a bare product link. TCGplayer accepts query
params that narrow a product to a specific listing — ``Language``, ``Printing``
(edition/foil variant), and ``Condition`` — e.g.::

    /product/84586/…?page=1&Language=English&Printing=Unlimited&Condition=Lightly+Played

:func:`product_url` layers those params on from the item's own fields so an
opened link lands on the most specific listing. Unknown fields are omitted
rather than guessed (over-constraining would show an empty result). Works for
any card-like object — the ledger ``Item`` (enum fields) or an ``AuctionCard``
(``.value`` strings) — by normalising each field to its string value.
"""

from __future__ import annotations

import re
import urllib.parse

# Keyed by each enum's ``.value`` string, so both enum and raw-string fields work.
_CONDITION = {
    "NM": "Near Mint",
    "LP": "Lightly Played",
    "MP": "Moderately Played",
    "HP": "Heavily Played",
    "DMG": "Damaged",
}
_LANGUAGE = {
    "EN": "English",
    "JP": "Japanese",
    "KO": "Korean",
    "FR": "French",
    "DE": "German",
    "IT": "Italian",
    "ES": "Spanish",
    "PT": "Portuguese",
    "RU": "Russian",
    "TH": "Thai",
    "ID": "Indonesian",
    "CN": "Chinese (Simplified)",
}


def _val(field) -> str | None:
    """The field's string value (enum ``.value`` or a plain string)."""
    if field is None:
        return None
    return field.value if hasattr(field, "value") else str(field)


def _printing(item) -> str | None:
    """TCGplayer's Printing value — a single field conflating edition (WOTC-era)
    and foil variant (modern)."""
    edition = _val(getattr(item, "edition", None))
    holo = _val(getattr(item, "holo_pattern", None))
    if edition in ("1st Edition", "Unlimited"):
        if holo == "H":
            return f"{edition} Holofoil"
        if holo == "RH":
            return "Reverse Holofoil"
        return edition
    if holo == "RH":
        return "Reverse Holofoil"
    if holo == "H":
        return "Holofoil"
    if holo == "NH":
        return "Normal"
    return None


def _condition(item) -> str | None:
    if getattr(item, "is_graded", False):
        return None  # graded slabs aren't filtered by a raw condition
    return _CONDITION.get(_val(getattr(item, "condition", None)) or "")


def product_url(item) -> str | None:
    """The item's TCGplayer link with condition/printing/language params layered
    on. Returns None if the item has no stored TCGplayer URL."""
    base = getattr(item, "tcgplayer_url", None)
    if not base:
        return None
    base = base.split("?", 1)[0]  # keep /product/<id>/…, drop any existing query
    params: list[tuple[str, str]] = [("page", "1")]
    for key, value in (
        ("Language", _LANGUAGE.get(_val(getattr(item, "language", None)) or "")),
        ("Printing", _printing(item)),
        ("Condition", _condition(item)),
    ):
        if value:
            params.append((key, value))
    return f"{base}?{urllib.parse.urlencode(params)}"


_SEARCH = "https://www.tcgplayer.com/search/pokemon/product"


def search_url(item) -> str | None:
    """A Pokémon-scoped TCGplayer search for the card (name + set + number).
    Used when there's no stored product link to point at."""
    name = (getattr(item, "name", None) or "").strip()
    if not name:
        return None
    parts = [name]
    for extra in (getattr(item, "set_name", None), getattr(item, "card_number", None)):
        if extra:
            parts.append(str(extra))
    query = urllib.parse.urlencode({"q": " ".join(parts), "view": "grid"})
    return f"{_SEARCH}?{query}"


def link_for(item) -> str | None:
    """The best TCGplayer link for a card: the specific product (its fields
    layered on) when a product URL is stored, otherwise a search."""
    if getattr(item, "tcgplayer_url", None):
        return product_url(item)
    return search_url(item)


_PRODUCT_ID = re.compile(r"/product/(\d+)")
_IMAGE_CDN = "https://tcgplayer-cdn.tcgplayer.com/product"


def product_id(url: str | None) -> str | None:
    """The numeric product id inside a TCGplayer product URL, or None."""
    match = _PRODUCT_ID.search(url or "")
    return match.group(1) if match else None


def product_image_url(product_id: str, width: int = 400) -> str:
    """Product-photo URL on TCGplayer's CDN.

    A product id names one exact printing — including the alt arts and promo
    variants the card catalog doesn't carry — so this is the reliable art for a
    card we've priced but can't pin in the catalog. (The Japanese catalog already
    stores its images in this form.)
    """
    return f"{_IMAGE_CDN}/{product_id}_{width}w.jpg"
