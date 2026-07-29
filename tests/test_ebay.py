"""Derived eBay sold-listings search URLs."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from tcginventory.domain.enums import Category, Condition, GradingCompany
from tcginventory.domain.models import Item
from tcginventory.services.ebay import sold_search_url


def test_sold_search_url_raw_card():
    item = Item(
        name="Raticate", category=Category.SINGLE, set_name="Perfect Order",
        card_number="99/88", condition=Condition.NM,
    )
    url = sold_search_url(item)
    assert url.startswith("https://www.ebay.com/sch/i.html?")
    # Encoded exactly like eBay: spaces → '+', '/' → '%2F'.
    assert "_nkw=Raticate+Perfect+Order+99%2F88+NM" in url
    q = parse_qs(urlparse(url).query)
    assert q["_nkw"] == ["Raticate Perfect Order 99/88 NM"]
    assert q["LH_Sold"] == ["1"]  # Sold Items filter on


def test_sold_search_url_graded_uses_company_and_grade():
    item = Item(
        name="Charizard", category=Category.SINGLE, set_name="Base",
        card_number="4/102", is_graded=True,
        grading_company=GradingCompany.PSA, grade="10",
    )
    q = parse_qs(urlparse(sold_search_url(item)).query)
    assert q["_nkw"] == ["Charizard Base 4/102 PSA 10"]


def test_sold_search_url_drops_missing_parts():
    item = Item(name="Pikachu", category=Category.SINGLE)  # no set / number / cond
    q = parse_qs(urlparse(sold_search_url(item)).query)
    assert q["_nkw"] == ["Pikachu"]
