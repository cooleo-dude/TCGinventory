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
