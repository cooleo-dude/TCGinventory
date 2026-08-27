"""Card entry offers only the printings a card actually comes in (ticket 57).

Offscreen per TESTS-HEADLESS. No network: variants are constructed directly.
"""

from __future__ import annotations

import pytest

from tcginventory.domain.enums import Category, Edition, HoloPattern, Intent
from tcginventory.domain.models import Item
from tcginventory.services.pricing import TREATMENT_IDENTITY, CardVariant
from tcginventory.services.transactions import IncomingCard


@pytest.fixture()
def row(app, conn):
    """A receive-side card row — the surface every card-entry screen shares."""
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    view.add_incoming(
        IncomingCard(
            Item(
                name="Testmon",
                set_name="Test Set",
                card_number="1",
                category=Category.SINGLE,
            ),
            1,
            100,
            Intent.RESALE,
        )
    )
    yield view._receive_rows[-1]
    view.deleteLater()


# ---------------------------------------------------------------------------
# the map
# ---------------------------------------------------------------------------


def test_every_treatment_maps_back_to_identity_columns():
    """The reverse map must cover all seven, or entry cannot record what was
    priced."""
    assert TREATMENT_IDENTITY["Normal"] == ("NH", None)
    assert TREATMENT_IDENTITY["Holofoil"] == ("H", None)
    assert TREATMENT_IDENTITY["Reverse Holofoil"] == ("RH", None)
    assert TREATMENT_IDENTITY["1st Edition"] == ("NH", "1st Edition")
    assert TREATMENT_IDENTITY["1st Edition Holofoil"] == ("H", "1st Edition")
    assert TREATMENT_IDENTITY["Unlimited"] == ("NH", "Unlimited")
    assert TREATMENT_IDENTITY["Unlimited Holofoil"] == ("H", "Unlimited")


def test_the_reverse_map_is_derived_from_the_forward_one():
    """Written twice, they would drift. Every entry must round-trip."""
    from tcginventory.services.pricing import _TREATMENT

    for treatment, (holo, edition) in TREATMENT_IDENTITY.items():
        assert _TREATMENT[(holo, edition)] == treatment


# ---------------------------------------------------------------------------
# narrowing the combo
# ---------------------------------------------------------------------------


def test_a_two_printing_card_offers_exactly_those_two(row):
    row.set_variants(
        [
            CardVariant(
                "Base",
                "u",
                224,
                True,
                treatments={"Normal": 224, "Reverse Holofoil": 61633},
            )
        ]
    )
    row.offer_treatments()
    labels = [row.holo.itemText(i) for i in range(row.holo.count())]
    assert labels == ["Normal", "Reverse Holofoil"]


def test_a_holo_only_card_offers_only_holofoil(row):
    """Base Set Charizard has no non-holo printing. Offering NON_HOLO — and
    defaulting to it — is how 1,040 of 1,115 real items got the wrong foil."""
    row.set_variants(
        [CardVariant("Base", "u", 9683, True, treatments={"Holofoil": 9683})]
    )
    row.offer_treatments()
    labels = [row.holo.itemText(i) for i in range(row.holo.count())]
    assert labels == ["Holofoil"]
    assert row.holo.currentData() == HoloPattern.HOLO.value


def test_an_unresolved_card_keeps_the_full_choice(row):
    """Empty treatments means "could not tell", not "one printing". Narrowing on
    a guess would be worse than the default it replaces."""
    row.set_variants([CardVariant("Base", "u", 100, True, treatments={})])
    row.offer_treatments()
    labels = [row.holo.itemText(i) for i in range(row.holo.count())]
    assert labels == [v.value for v in HoloPattern]


def test_a_card_looked_up_with_no_variants_at_all_keeps_the_full_choice(row):
    row.set_variants([])
    row.offer_treatments()
    labels = [row.holo.itemText(i) for i in range(row.holo.count())]
    assert labels == [v.value for v in HoloPattern]


# ---------------------------------------------------------------------------
# the choice drives price and identity
# ---------------------------------------------------------------------------


def test_choosing_a_printing_applies_that_printings_price(row):
    """The point of the whole chain: a reverse holo is not priced as a normal."""
    row.set_variants(
        [
            CardVariant(
                "Base",
                "u",
                224,
                True,
                treatments={"Normal": 224, "Reverse Holofoil": 61633},
            )
        ]
    )
    row.offer_treatments()
    assert row.market.value() == pytest.approx(2.24)  # cheapest first
    row.holo.setCurrentIndex(1)  # Reverse Holofoil
    row.apply_treatment_choice()
    assert row.market.value() == pytest.approx(616.33)
    assert row.holo.currentData() == HoloPattern.REVERSE_HOLO.value


def test_an_edition_bearing_printing_sets_the_edition_column_too(row):
    """1st Edition is half of the identity; recording only the foil would make a
    1st-edition and an unlimited copy the same item."""
    row.set_variants(
        [
            CardVariant(
                "Base",
                "u",
                1200,
                True,
                treatments={"1st Edition": 5000, "Unlimited": 1200},
            )
        ]
    )
    row.offer_treatments()
    labels = [row.holo.itemText(i) for i in range(row.holo.count())]
    assert labels == ["1st Edition", "Unlimited"]
    row.holo.setCurrentIndex(0)  # 1st Edition
    row.apply_treatment_choice()
    assert row.edition.currentData() == Edition.FIRST.value
    assert row.market.value() == pytest.approx(50.00)


def test_switching_back_to_a_plain_printing_clears_the_edition(row):
    row.set_variants(
        [
            CardVariant(
                "Base",
                "u",
                1200,
                True,
                treatments={"1st Edition": 5000, "Unlimited": 1200},
            )
        ]
    )
    row.offer_treatments()
    row.holo.setCurrentIndex(0)
    row.apply_treatment_choice()
    assert row.edition.currentData() == Edition.FIRST.value
    row.holo.setCurrentIndex(1)  # Unlimited
    row.apply_treatment_choice()
    assert row.edition.currentData() == Edition.UNLIMITED.value


def test_the_row_still_builds_an_item_after_narrowing(row):
    """Guard the seam: narrowing rebuilds the combo, and item building reads
    currentData() off it."""
    row.set_variants(
        [CardVariant("Base", "u", 9683, True, treatments={"Holofoil": 9683})]
    )
    row.offer_treatments()
    card = row.to_card()
    assert card.item.holo_pattern is HoloPattern.HOLO


# ---------------------------------------------------------------------------
# the same rule on the other card-entry screen
# ---------------------------------------------------------------------------


def test_auctions_rows_narrow_the_same_way(app, conn):
    """CARD-ENTRY-STANDARD: one implementation, so eBay Auctions gets it too."""
    from tcginventory.data.auctions import open_auctions
    from tcginventory.ui.views.auctions import AuctionsView

    view = AuctionsView(conn, auctions_conn=open_auctions(":memory:"))
    try:
        row = view._add_card_row()
        row.set_variants(
            [CardVariant("Base", "u", 9683, True, treatments={"Holofoil": 9683})]
        )
        row.offer_treatments()
        labels = [row.holo.itemText(i) for i in range(row.holo.count())]
        assert labels == ["Holofoil"]
    finally:
        view.deleteLater()


def test_a_treatment_price_does_not_count_as_a_manual_override(app, conn):
    """Auctions tracks whether the user edited the price. Applying a treatment
    is our own change, not theirs — writing the spin box directly would flag it
    as manual and stop future auto-pricing."""
    from tcginventory.data.auctions import open_auctions
    from tcginventory.ui.views.auctions import AuctionsView

    view = AuctionsView(conn, auctions_conn=open_auctions(":memory:"))
    try:
        row = view._add_card_row()
        row.set_variants(
            [
                CardVariant(
                    "Base",
                    "u",
                    224,
                    True,
                    treatments={"Normal": 224, "Reverse Holofoil": 61633},
                )
            ]
        )
        row.offer_treatments()
        row.holo.setCurrentIndex(1)
        row.apply_treatment_choice()
        assert round(row.market.value() * 100) == 61633
        assert not row._market_is_manual
    finally:
        view.deleteLater()
