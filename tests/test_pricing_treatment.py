"""Pricing by foil treatment rather than by the dearest printing (ticket 56).

Everything here is offline: tcgcsv responses are injected as fixtures. The
numbers in the docstrings come from a measured sweep of all 218 English groups
and are recorded on the ticket.
"""

from __future__ import annotations

import pytest

from tcginventory.domain.enums import Category, Condition, Edition, HoloPattern
from tcginventory.domain.models import Item
from tcginventory.services import pricing
from tcginventory.services.pricing import (
    PICK_CHEAPEST,
    PICK_EXACT,
    PICK_FALLBACK,
    PICK_ONLY,
    TcgcsvProvider,
    cheapest_treatment,
    select_treatment,
    treatment_for,
)


def _item(
    item_id: int = 1,
    holo: HoloPattern | None = HoloPattern.NON_HOLO,
    edition: Edition | None = None,
    number: str = "1/102",
    product: int | None = 900,
) -> Item:
    return Item(
        id=item_id,
        name="Testmon",
        set_name="Test Set",
        card_number=number,
        category=Category.SINGLE,
        condition=Condition.NM,
        holo_pattern=holo,
        edition=edition,
        tcgplayer_url=(f"https://tcgplayer.com/product/{product}" if product else None),
    )


# ---------------------------------------------------------------------------
# the identity → subTypeName map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "holo,edition,expected",
    [
        (HoloPattern.NON_HOLO, None, "Normal"),
        (HoloPattern.HOLO, None, "Holofoil"),
        (HoloPattern.REVERSE_HOLO, None, "Reverse Holofoil"),
        (HoloPattern.NON_HOLO, Edition.NA, "Normal"),
        (HoloPattern.NON_HOLO, Edition.FIRST, "1st Edition"),
        (HoloPattern.HOLO, Edition.FIRST, "1st Edition Holofoil"),
        (HoloPattern.NON_HOLO, Edition.UNLIMITED, "Unlimited"),
        (HoloPattern.HOLO, Edition.UNLIMITED, "Unlimited Holofoil"),
    ],
)
def test_every_tcgcsv_treatment_has_an_identity_that_maps_to_it(
    holo, edition, expected
):
    """All seven subTypeName values tcgcsv uses decompose onto the two identity
    columns migration 0011 already added."""
    assert treatment_for(_item(holo=holo, edition=edition)) == expected


def test_an_item_that_does_not_say_which_printing_maps_to_nothing():
    assert treatment_for(_item(holo=None)) is None


def test_a_combination_tcgcsv_has_no_name_for_maps_to_nothing():
    """Reverse holos have no 1st-edition printing; the pair must not invent one."""
    assert (
        treatment_for(_item(holo=HoloPattern.REVERSE_HOLO, edition=Edition.FIRST))
        is None
    )


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def test_the_declared_treatment_wins_over_a_dearer_one():
    """The bug this ticket exists for: Charmander in Legendary Collection is
    $2.24 Normal and $616.33 Reverse Holofoil. A Normal copy is worth $2.24."""
    subs = {"Normal": 224, "Reverse Holofoil": 61633}
    assert select_treatment(subs, _item(holo=HoloPattern.NON_HOLO)) == (
        224,
        "Normal",
        PICK_EXACT,
    )


def test_the_declared_treatment_wins_when_it_is_the_dearer_one_too():
    """Not merely 'always cheapest' — a reverse holo really is worth the more."""
    subs = {"Normal": 224, "Reverse Holofoil": 61633}
    assert select_treatment(subs, _item(holo=HoloPattern.REVERSE_HOLO)) == (
        61633,
        "Reverse Holofoil",
        PICK_EXACT,
    )


def test_a_single_treatment_product_overrides_what_the_item_claims():
    """682 of 759 real items are here. holo_pattern is ~93% the untrustworthy NH
    default, so a product with one printing must not be left unpriced over it."""
    subs = {"Holofoil": 9683}
    assert select_treatment(subs, _item(holo=HoloPattern.NON_HOLO)) == (
        9683,
        "Holofoil",
        PICK_ONLY,
    )


def test_an_undeclared_item_takes_the_cheapest_not_the_dearest():
    """Overstating a portfolio inflates unrealized gain and collection value, so
    the unknown case errs downward. Reverting to max() turns this red."""
    subs = {"Normal": 224, "Reverse Holofoil": 61633}
    assert select_treatment(subs, _item(holo=None)) == (224, "Normal", PICK_CHEAPEST)


def test_an_item_naming_an_absent_printing_falls_back_rather_than_losing_its_price():
    """This refused to price at first, on "a missing price beats a wrong one".
    The real ledger disproved it: refusing dropped 53 items whose product match
    was fine — vintage cards that exist only as 1st Edition/Unlimited, and
    holo-only cards recorded NH because NH is the default. The card is still one
    of the printings offered, so take the cheapest and mark it."""
    subs = {"1st Edition": 5000, "Unlimited": 1200}
    assert select_treatment(subs, _item(holo=HoloPattern.NON_HOLO)) == (
        1200,
        "Unlimited",
        PICK_FALLBACK,
    )


def test_the_fallback_is_distinguishable_from_an_item_that_said_nothing():
    """Both take the cheapest, but only one of them ignored a stated claim —
    ticket 57 needs to tell them apart to know what to ask about."""
    subs = {"Holofoil": 9000, "Reverse Holofoil": 500}
    said_nothing = select_treatment(subs, _item(holo=None))
    said_wrong = select_treatment(subs, _item(holo=HoloPattern.NON_HOLO))
    assert said_nothing[2] == PICK_CHEAPEST
    assert said_wrong[2] == PICK_FALLBACK
    assert said_nothing[0] == said_wrong[0] == 500


def test_a_product_with_no_priced_treatment_yields_nothing():
    assert select_treatment({}, _item()) is None
    assert cheapest_treatment({}) is None


# ---------------------------------------------------------------------------
# through the provider
# ---------------------------------------------------------------------------

_GROUPS = {"results": [{"groupId": 7, "name": "Test Set", "abbreviation": "TS"}]}
_PRODUCTS = {
    "results": [
        {
            "productId": 900,
            "name": "Testmon",
            "url": "https://tcgplayer.com/product/900",
            "extendedData": [{"name": "Number", "value": "1/102"}],
        }
    ]
}
_PRICES = {
    "results": [
        {"productId": 900, "subTypeName": "Normal", "marketPrice": 2.24},
        {"productId": 900, "subTypeName": "Reverse Holofoil", "marketPrice": 616.33},
    ]
}


def _fake_fetch(url: str):
    if url.endswith("/groups"):
        return _GROUPS
    if url.endswith("/products"):
        return _PRODUCTS
    if url.endswith("/prices"):
        return _PRICES
    raise AssertionError(f"unexpected url {url}")


def test_two_items_on_one_product_get_different_prices():
    """The end-to-end shape of the fix, from one price file. Under max() both
    came back at 61633."""
    provider = TcgcsvProvider(fetch_json=_fake_fetch)
    normal = _item(item_id=1, holo=HoloPattern.NON_HOLO)
    reverse = _item(item_id=2, holo=HoloPattern.REVERSE_HOLO)
    quotes = {q.item_id: q for q in provider.quotes_for([normal, reverse])}
    assert quotes[1].price_cents == 224
    assert quotes[1].treatment == "Normal"
    assert quotes[2].price_cents == 61633
    assert quotes[2].treatment == "Reverse Holofoil"


def test_a_product_with_no_prices_produces_no_quote():
    """The only remaining reason to return nothing."""

    def fetch(url: str):
        if url.endswith("/prices"):
            return {"results": [{"productId": 900, "marketPrice": None}]}
        return _fake_fetch(url)

    assert TcgcsvProvider(fetch_json=fetch).quotes_for([_item()]) == []


def test_an_item_claiming_an_absent_printing_still_gets_a_price():
    """Regression guard: this used to yield no quote, silently removing the
    market value of 53 real items."""
    provider = TcgcsvProvider(fetch_json=_fake_fetch)
    quotes = provider.quotes_for([_item(holo=HoloPattern.HOLO)])
    assert len(quotes) == 1
    assert quotes[0].price_cents == 224  # cheapest of Normal / Reverse Holofoil
    assert quotes[0].picked == PICK_FALLBACK


def test_market_prices_is_unchanged_by_this_ticket():
    """The catalog-wide sweep behind pricehistory.db has no treatment to select
    by, so it still collapses to the dearest. Guards a year of series — and the
    Inventory sparkline, trends and Market signals — against silent drift."""
    provider = TcgcsvProvider(fetch_json=_fake_fetch)
    assert provider.market_prices(3, 7) == {900: 61633}


def test_card_entry_autofill_takes_the_cheapest_printing():
    """price_for_card runs before an item exists, so it uses the same
    unknown-treatment rule rather than the dearest."""
    provider = TcgcsvProvider(fetch_json=_fake_fetch)
    result = provider.price_for_card("Test Set", "1/102", "Testmon")
    assert result is not None
    assert result[0] == 224


def test_a_repeated_subtype_row_does_not_multiply_into_two_treatments():
    """Source anomaly guard: the shape stays one price per treatment."""

    def fetch(url: str):
        if url.endswith("/prices"):
            return {
                "results": [
                    {"productId": 900, "subTypeName": "Normal", "marketPrice": 2.00},
                    {"productId": 900, "subTypeName": "Normal", "marketPrice": 3.00},
                ]
            }
        return _fake_fetch(url)

    provider = TcgcsvProvider(fetch_json=fetch)
    assert provider._prices_for_group(3, 7) == {900: {"Normal": 300}}


def test_graded_items_are_still_skipped_by_the_service(tmp_path):
    """Existing behaviour must survive: a slab is not priced from the raw single."""
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.services.pricing import PricingService

    conn = connect(":memory:")
    migrate(conn)
    calls: list[list[Item]] = []

    class _Provider:
        def quotes_for(self, items):
            calls.append(list(items))
            return []

    graded = _item()
    graded.is_graded = True
    PricingService(conn, provider=_Provider()).refresh([graded])
    assert calls == [[]]
    conn.close()


def test_pricing_module_stays_qt_free():
    """QT-FREE-CORE: services never import PySide6."""
    import inspect

    assert "PySide6" not in inspect.getsource(pricing)
