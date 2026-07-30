"""Derive an eBay **sold-listings** search URL for a card (Qt-free, no network).

Every card gets an eBay link for free: rather than storing a URL, we build an
eBay search from the card's identity — ``Name Set # Condition/Grade`` — with the
Sold Items filter on. Deriving it (instead of storing) means the link always
tracks the current identity and needs no backfill; a manually-entered
``item.ebay_url`` still takes precedence at the call site.
"""

from __future__ import annotations

import urllib.parse

_EBAY_SEARCH = "https://www.ebay.com/sch/i.html"


def _cond_or_grade(item) -> str:
    """"PSA 10" for a slab, else the raw condition ("NM"); "" if unknown."""
    if getattr(item, "is_graded", False):
        company = item.grading_company.value if item.grading_company else ""
        return f"{company} {item.grade or ''}".strip()
    return item.condition.value if getattr(item, "condition", None) else ""


def search_url(
    name: str,
    set_name: str | None = None,
    number: str | None = None,
    cond_or_grade: str | None = None,
) -> str:
    """An eBay sold-listings search keyed on ``Name Set # Condition/Grade``
    (blank parts dropped). The low-level builder — see :func:`sold_search_url`
    for the item-based wrapper."""
    query = " ".join(p for p in (name, set_name, number, cond_or_grade) if p)
    params = urllib.parse.urlencode({"_nkw": query, "_sacat": "0", "LH_Sold": "1"})
    return f"{_EBAY_SEARCH}?{params}"


def sold_search_url(item) -> str:
    """An eBay sold-listings search for a catalog/inventory ``item``."""
    return search_url(
        item.name, item.set_name, item.card_number, _cond_or_grade(item)
    )
