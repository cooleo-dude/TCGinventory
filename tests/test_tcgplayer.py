"""TCGplayer product-link builder: card fields → query params."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from tcginventory.domain.enums import (
    Category,
    Condition,
    Edition,
    GradingCompany,
    HoloPattern,
    Language,
)
from tcginventory.domain.models import Item
from tcginventory.services.tcgplayer import product_url

_BASE = "https://www.tcgplayer.com/product/84586/pokemon-team-rocket-dark-dragonite-22"


def _params(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def _item(url: str = _BASE, **kw) -> Item:
    return Item(name="X", category=Category.SINGLE, tcgplayer_url=url, **kw)


def test_wotc_unlimited_matches_the_example():
    url = product_url(
        _item(condition=Condition.LP, edition=Edition.UNLIMITED,
              holo_pattern=HoloPattern.NON_HOLO, language=Language.EN)
    )
    assert url.startswith(_BASE + "?")
    assert _params(url) == {
        "page": "1",
        "Language": "English",
        "Printing": "Unlimited",
        "Condition": "Lightly Played",
    }


def test_modern_reverse_holo_heavily_played():
    url = product_url(
        _item(condition=Condition.HP, holo_pattern=HoloPattern.REVERSE_HOLO,
              edition=Edition.NA, language=Language.EN)
    )
    assert _params(url) == {
        "page": "1",
        "Language": "English",
        "Printing": "Reverse Holofoil",
        "Condition": "Heavily Played",
    }


def test_first_edition_holo_combines():
    url = product_url(
        _item(
            edition=Edition.FIRST,
            holo_pattern=HoloPattern.HOLO,
            language=Language.JP,
        )
    )
    p = _params(url)
    assert p["Printing"] == "1st Edition Holofoil"
    assert p["Language"] == "Japanese"


def test_graded_omits_condition():
    url = product_url(
        _item(is_graded=True, grading_company=GradingCompany.PSA, grade="10",
              condition=None, holo_pattern=HoloPattern.HOLO)
    )
    assert "Condition" not in _params(url)
    assert _params(url)["Printing"] == "Holofoil"


def test_unknown_printing_is_omitted():
    url = product_url(_item(condition=Condition.NM))  # no edition/holo set
    p = _params(url)
    assert "Printing" not in p
    assert p["Condition"] == "Near Mint"


def test_no_stored_url_returns_none():
    assert product_url(Item(name="X", category=Category.SINGLE)) is None


def test_accepts_raw_string_fields_like_an_auction_card():
    # AuctionCard stores fields as .value strings, not enums.
    card = SimpleNamespace(
        tcgplayer_url=_BASE, condition="MP", holo_pattern="RH",
        edition="N/A", language="EN", is_graded=False,
    )
    p = _params(product_url(card))
    assert p["Condition"] == "Moderately Played"
    assert p["Printing"] == "Reverse Holofoil"


def test_existing_query_is_replaced_not_appended():
    url = product_url(_item(url=_BASE + "?page=1&x=y", condition=Condition.NM))
    assert url.count("?") == 1 and "x=y" not in url
