"""The checklist's secondary line: set · number · holo (ticket 66).

A bare collector number said the least useful thing about a card that is already
showing its own art. This is the same shape the Inventory grid's Card cell uses,
including its omissions.
"""

from __future__ import annotations

import pytest

from tcginventory.services.collections import FILLED, MISSING, Slot
from tcginventory.ui.models.collection_model import slot_subline


def _slot(**kw) -> Slot:
    base = dict(
        card_id="c-1",
        name="Alakazam",
        number="1",
        set_name="Base Set",
        image_url=None,
    )
    base.update(kw)
    return Slot(**base)


def test_the_subline_is_set_then_number():
    assert slot_subline(_slot()) == "Base Set · 1"


def test_a_master_mode_slot_names_its_printing():
    """In master mode the printing *is* the slot's identity."""
    assert (
        slot_subline(_slot(treatment="Reverse Holofoil"))
        == "Base Set · 1 · Reverse Holofoil"
    )


def test_an_owned_copys_foil_shows_when_the_slot_is_a_number():
    """By number the slot is a collector number, so the held copy's foil is the
    only way the checklist can say "you have the reverse holo"."""
    assert slot_subline(_slot(state=FILLED, holo="RH")) == "Base Set · 1 · Reverse Holo"
    assert slot_subline(_slot(state=FILLED, holo="H")) == "Base Set · 1 · Holo"


def test_a_plain_non_holo_says_nothing_about_foil():
    """ "NH" on nine cards in ten is noise, not information — the Inventory
    sub-line omits it for the same reason."""
    assert slot_subline(_slot(state=FILLED, holo="NH")) == "Base Set · 1"


def test_a_card_that_only_comes_one_way_names_that_printing():
    """How Base Set 1-16 read as holos without anyone owning them."""
    assert slot_subline(_slot(only_printing="Holofoil")) == "Base Set · 1 · Holofoil"


def test_a_sole_printing_of_normal_is_not_worth_saying():
    assert slot_subline(_slot(only_printing="Normal")) == "Base Set · 1"


def test_an_owned_copy_outranks_the_catalogs_guess():
    """What you actually hold beats what the card usually is."""
    assert (
        slot_subline(_slot(state=FILLED, holo="RH", only_printing="Holofoil"))
        == "Base Set · 1 · Reverse Holo"
    )


def test_the_slots_own_printing_outranks_everything():
    assert (
        slot_subline(
            _slot(treatment="1st Edition Holofoil", holo="NH", only_printing="Normal")
        )
        == "Base Set · 1 · 1st Edition Holofoil"
    )


@pytest.mark.parametrize(
    "kw,expected",
    [
        ({"set_name": None}, "1"),
        ({"number": None}, "Base Set"),
        ({"set_name": None, "number": None}, ""),
    ],
)
def test_missing_parts_are_omitted_rather_than_rendered_blank(kw, expected):
    assert slot_subline(_slot(**kw)) == expected


def test_an_unowned_slot_still_gets_a_subline():
    """A missing slot is the point of a checklist; it must still say what card
    it is waiting for."""
    assert slot_subline(_slot(state=MISSING)) == "Base Set · 1"
