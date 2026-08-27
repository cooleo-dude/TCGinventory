"""Personal Collection templates, slots and the three-state model (ticket 60).

Headless: no Qt, no network. The property under test throughout is the one
Inventory structurally cannot express — a slot for a card you do **not** own.
"""

from __future__ import annotations

import sqlite3

import pytest

from tcginventory.data.db import connect, now_iso
from tcginventory.data.migrator import migrate
from tcginventory.data.repositories import (
    CardCatalogRepository,
    CardTreatmentRepository,
    CollectionRepository,
    ItemRepository,
    LotRepository,
)
from tcginventory.domain.enums import Category, Condition, Edition, HoloPattern, Intent
from tcginventory.domain.models import CatalogCard, Collection, Item, Lot
from tcginventory.services.collections import (
    FILLED,
    IMPORTABLE,
    MISSING,
    SLOT_MASTER,
    TEMPLATE_ARTIST,
    TEMPLATE_CUSTOM,
    TEMPLATE_RARITY,
    TEMPLATE_SET,
    TEMPLATE_SUBSET,
    TEMPLATES,
    CollectionService,
    collector_key,
    summarise,
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    yield c
    c.close()


def _catalog(conn, cards):
    CardCatalogRepository(conn).replace_for_language(cards, "EN", now_iso())


def _own(
    conn,
    name,
    set_name,
    number,
    intent=Intent.RESALE,
    qty=1,
    value=1000,
    holo=HoloPattern.NON_HOLO,
    edition=None,
):
    item = ItemRepository(conn).add(
        Item(
            name=name,
            set_name=set_name,
            card_number=number,
            category=Category.SINGLE,
            condition=Condition.NM,
            holo_pattern=holo,
            edition=edition,
            market_value_cents=value,
        )
    )
    LotRepository(conn).add(
        Lot(
            item_id=item.id,
            acquired_at="2026-08-01",
            quantity=qty,
            remaining_qty=qty,
            unit_cost_cents=500,
            acquired_market_value_cents=value,
            intent=intent,
        )
    )
    return item


def _set_of_three(conn):
    _catalog(
        conn,
        [
            CatalogCard(
                card_id="s1-1",
                name="Alpha",
                set_id="s1",
                set_name="Set One",
                number="1",
                printed_total=3,
                rarity="Common",
                artist="Arita",
                supertype="Pokemon",
            ),
            CatalogCard(
                card_id="s1-2",
                name="Beta",
                set_id="s1",
                set_name="Set One",
                number="2",
                printed_total=3,
                rarity="Rare",
                artist="Arita",
                supertype="Trainer",
            ),
            CatalogCard(
                card_id="s1-3",
                name="Gamma",
                set_id="s1",
                set_name="Set One",
                number="3",
                printed_total=3,
                rarity="Common",
                artist="Sugimori",
                supertype="Pokemon",
            ),
        ],
    )


def _collection(conn, template=TEMPLATE_SET, selector="Set One", slot_mode="number"):
    return CollectionRepository(conn).add(
        Collection(
            name="Test", template=template, selector=selector, slot_mode=slot_mode
        )
    )


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def test_all_six_templates_are_registered():
    assert {t.id for t in TEMPLATES} == {
        "set",
        "subset",
        "artist",
        "rarity",
        "supertype",
        "custom",
    }


def test_the_english_only_templates_are_marked_as_such():
    """Set needs printed_total (0 of 29,845 JP rows) and artist needs an
    illustrator (JP ids do not join to TCGdex). The UI must be able to say so."""
    by_id = {t.id: t for t in TEMPLATES}
    assert by_id["set"].english_only
    assert by_id["artist"].english_only
    assert not by_id["custom"].english_only


# ---------------------------------------------------------------------------
# slots exist for cards you do not own
# ---------------------------------------------------------------------------


def test_a_set_collection_has_a_slot_per_card_owned_or_not(conn):
    """The whole point of the window: Inventory cannot draw an absent card."""
    _set_of_three(conn)
    _own(conn, "Alpha", "Set One", "1")
    slots = CollectionService(conn).slots_for(_collection(conn))
    assert len(slots) == 3
    assert [s.name for s in slots] == ["Alpha", "Beta", "Gamma"]
    assert [s.state for s in slots] == [IMPORTABLE, MISSING, MISSING]


def test_an_empty_collection_still_enumerates_every_slot(conn):
    _set_of_three(conn)
    slots = CollectionService(conn).slots_for(_collection(conn))
    assert len(slots) == 3
    assert all(s.state == MISSING for s in slots)


# ---------------------------------------------------------------------------
# the three states
# ---------------------------------------------------------------------------


def test_a_personal_copy_fills_a_slot(conn):
    _set_of_three(conn)
    _own(conn, "Alpha", "Set One", "1", intent=Intent.PERSONAL)
    slots = {s.name: s for s in CollectionService(conn).slots_for(_collection(conn))}
    assert slots["Alpha"].state == FILLED


def test_a_resale_copy_is_importable_not_filled(conn):
    """The distinction the user asked for: you own it, but it is stock."""
    _set_of_three(conn)
    _own(conn, "Alpha", "Set One", "1", intent=Intent.RESALE)
    slots = {s.name: s for s in CollectionService(conn).slots_for(_collection(conn))}
    assert slots["Alpha"].state == IMPORTABLE
    assert slots["Alpha"].lot_ids, "the UI needs the lot to offer an import"


def test_a_missing_flagged_lot_does_not_fill_a_slot(conn):
    """INV-MISSING-EXCLUDED: a stolen/lost lot is not on hand."""
    _set_of_three(conn)
    item = _own(conn, "Alpha", "Set One", "1", intent=Intent.PERSONAL)
    lot = LotRepository(conn).list_for_item(item.id)[0]
    lot.missing = True
    LotRepository(conn).update(lot)
    conn.commit()
    slots = {s.name: s for s in CollectionService(conn).slots_for(_collection(conn))}
    assert slots["Alpha"].state == MISSING


def test_a_sold_out_lot_does_not_fill_a_slot(conn):
    _set_of_three(conn)
    item = _own(conn, "Alpha", "Set One", "1")
    lot = LotRepository(conn).list_for_item(item.id)[0]
    lot.remaining_qty = 0
    LotRepository(conn).update(lot)
    conn.commit()
    slots = {s.name: s for s in CollectionService(conn).slots_for(_collection(conn))}
    assert slots["Alpha"].state == MISSING


def test_one_personal_lot_among_resale_ones_fills_the_slot(conn):
    """After an import splits a lot, the item has both. The slot is filled."""
    _set_of_three(conn)
    item = _own(conn, "Alpha", "Set One", "1", intent=Intent.RESALE, qty=2)
    LotRepository(conn).add(
        Lot(
            item_id=item.id,
            acquired_at="2026-08-01",
            quantity=1,
            remaining_qty=1,
            unit_cost_cents=500,
            acquired_market_value_cents=1000,
            intent=Intent.PERSONAL,
        )
    )
    conn.commit()
    slots = {s.name: s for s in CollectionService(conn).slots_for(_collection(conn))}
    assert slots["Alpha"].state == FILLED
    assert slots["Alpha"].qty == 3


def test_a_padded_number_still_matches_its_holding(conn):
    """Padding is a property of the set (ticket 49), so matching normalises both
    sides: a catalog "006" must find an item recorded "6/165"."""
    _catalog(
        conn,
        [
            CatalogCard(
                card_id="sv-6",
                name="Zeta",
                set_id="sv",
                set_name="SV",
                number="006",
                printed_total=165,
            )
        ],
    )
    _own(conn, "Zeta", "SV", "6/165", intent=Intent.PERSONAL)
    slots = CollectionService(conn).slots_for(_collection(conn, selector="SV"))
    assert slots[0].state == FILLED


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


def test_an_artist_collection_matches_only_that_illustrator(conn):
    _set_of_three(conn)
    slots = CollectionService(conn).slots_for(
        _collection(conn, template=TEMPLATE_ARTIST, selector="Arita")
    )
    assert [s.name for s in slots] == ["Alpha", "Beta"]


def test_a_rarity_collection_matches_only_that_rarity(conn):
    _set_of_three(conn)
    slots = CollectionService(conn).slots_for(
        _collection(conn, template=TEMPLATE_RARITY, selector="Common")
    )
    assert [s.name for s in slots] == ["Alpha", "Gamma"]


def test_a_subset_collection_matches_the_lettered_run(conn):
    _catalog(
        conn,
        [
            CatalogCard(
                card_id="p-1", name="Plain", set_id="p", set_name="P", number="1"
            ),
            CatalogCard(
                card_id="p-tg1",
                name="Gallery One",
                set_id="p",
                set_name="P",
                number="TG01",
            ),
            CatalogCard(
                card_id="p-tg2",
                name="Gallery Two",
                set_id="p",
                set_name="P",
                number="TG02",
            ),
        ],
    )
    slots = CollectionService(conn).slots_for(
        _collection(conn, template=TEMPLATE_SUBSET, selector="TG")
    )
    assert [s.name for s in slots] == ["Gallery One", "Gallery Two"]


def test_a_custom_collection_holds_only_its_picks(conn):
    _set_of_three(conn)
    coll = _collection(conn, template=TEMPLATE_CUSTOM, selector=None)
    repo = CollectionRepository(conn)
    repo.add_card(coll.id, "s1-3")
    conn.commit()
    slots = CollectionService(conn).slots_for(coll)
    assert [s.name for s in slots] == ["Gamma"]


def test_an_empty_custom_collection_has_no_slots(conn):
    _set_of_three(conn)
    coll = _collection(conn, template=TEMPLATE_CUSTOM, selector=None)
    assert CollectionService(conn).slots_for(coll) == []


# ---------------------------------------------------------------------------
# master mode
# ---------------------------------------------------------------------------


def test_master_mode_makes_one_slot_per_printing(conn):
    """A card existing as Normal and Reverse Holofoil is two slots in a master
    set and one slot by number."""
    _set_of_three(conn)
    CardTreatmentRepository(conn).replace_for_language(
        [
            ("s1-1", "Normal"),
            ("s1-1", "Reverse Holofoil"),
            ("s1-2", "Holofoil"),
            ("s1-3", "Normal"),
        ],
        "EN",
    )
    conn.commit()
    svc = CollectionService(conn)
    assert len(svc.slots_for(_collection(conn))) == 3
    master = svc.slots_for(_collection(conn, slot_mode=SLOT_MASTER))
    assert len(master) == 4
    assert sorted(s.treatment for s in master if s.card_id == "s1-1") == [
        "Normal",
        "Reverse Holofoil",
    ]


def test_master_mode_fills_only_the_printing_actually_held(conn):
    """Owning the Normal must not fill the Reverse Holofoil slot."""
    _set_of_three(conn)
    CardTreatmentRepository(conn).replace_for_language(
        [("s1-1", "Normal"), ("s1-1", "Reverse Holofoil")], "EN"
    )
    _own(
        conn, "Alpha", "Set One", "1", intent=Intent.PERSONAL, holo=HoloPattern.NON_HOLO
    )
    conn.commit()
    slots = {
        s.treatment: s
        for s in CollectionService(conn).slots_for(
            _collection(conn, slot_mode=SLOT_MASTER)
        )
        if s.card_id == "s1-1"
    }
    assert slots["Normal"].state == FILLED
    assert slots["Reverse Holofoil"].state == MISSING


def test_master_mode_matches_an_edition_bearing_printing(conn):
    _catalog(
        conn,
        [
            CatalogCard(
                card_id="b-4", name="Chari", set_id="b", set_name="Base", number="4"
            )
        ],
    )
    CardTreatmentRepository(conn).replace_for_language(
        [("b-4", "1st Edition Holofoil"), ("b-4", "Unlimited Holofoil")], "EN"
    )
    _own(
        conn,
        "Chari",
        "Base",
        "4",
        intent=Intent.PERSONAL,
        holo=HoloPattern.HOLO,
        edition=Edition.FIRST,
    )
    conn.commit()
    slots = {
        s.treatment: s
        for s in CollectionService(conn).slots_for(
            _collection(conn, selector="Base", slot_mode=SLOT_MASTER)
        )
    }
    assert slots["1st Edition Holofoil"].state == FILLED
    assert slots["Unlimited Holofoil"].state == MISSING


def test_a_card_with_no_treatment_data_is_unknown_not_single_printing(conn):
    """The 5% gap. Rendering it as one printing makes a master set under-count
    and read complete when it is not."""
    _set_of_three(conn)  # no treatments recorded at all
    slots = CollectionService(conn).slots_for(_collection(conn, slot_mode=SLOT_MASTER))
    assert len(slots) == 3
    assert all(s.treatments_unknown for s in slots)
    assert summarise(slots).unknown_treatments == 3


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------


def test_progress_counts_the_three_states_separately(conn):
    _set_of_three(conn)
    _own(conn, "Alpha", "Set One", "1", intent=Intent.PERSONAL)
    _own(conn, "Beta", "Set One", "2", intent=Intent.RESALE)
    conn.commit()
    p = CollectionService(conn).progress_for(_collection(conn))
    assert (p.total, p.filled, p.importable, p.missing) == (3, 1, 1, 1)
    assert round(p.percent) == 33


def test_progress_of_an_empty_collection_does_not_divide_by_zero(conn):
    _catalog(conn, [])
    assert CollectionService(conn).progress_for(_collection(conn)).percent == 0.0


# ---------------------------------------------------------------------------
# selector options
# ---------------------------------------------------------------------------


def test_selector_options_offer_only_what_the_catalog_supports(conn):
    _set_of_three(conn)
    svc = CollectionService(conn)
    assert svc.selector_options(TEMPLATE_SET) == ["Set One"]
    assert svc.selector_options(TEMPLATE_ARTIST) == ["Arita", "Sugimori"]
    assert svc.selector_options(TEMPLATE_RARITY) == ["Common", "Rare"]
    assert svc.selector_options(TEMPLATE_CUSTOM) == []


def test_a_template_with_no_data_offers_nothing_rather_than_a_bad_picker(conn):
    """An artist run before the catalog has been re-pulled. The UI must be able
    to tell the difference between "no artists" and "no cards"."""
    _catalog(
        conn,
        [CatalogCard(card_id="x-1", name="X", set_id="x", set_name="X", number="1")],
    )
    assert CollectionService(conn).selector_options(TEMPLATE_ARTIST) == []


def test_set_options_exclude_sets_with_no_printed_total(conn):
    """Without a denominator there is no "complete", so it is not a set project.
    Every JP set is in this position."""
    _catalog(
        conn,
        [
            CatalogCard(
                card_id="j-1", name="J", set_id="j", set_name="No Total", number="1"
            )
        ],
    )
    assert CollectionService(conn).selector_options(TEMPLATE_SET) == []


# ---------------------------------------------------------------------------
# custom membership
# ---------------------------------------------------------------------------


def test_removing_a_pick_leaves_the_lots_intent_alone(conn):
    """Settled with the user: removal is a membership change, not an inventory
    decision. Reverting would push a card back onto the resale side as a side
    effect of tidying a checklist."""
    _set_of_three(conn)
    item = _own(conn, "Alpha", "Set One", "1", intent=Intent.PERSONAL)
    coll = _collection(conn, template=TEMPLATE_CUSTOM, selector=None)
    repo = CollectionRepository(conn)
    repo.add_card(coll.id, "s1-1")
    repo.remove_card(coll.id, "s1-1")
    conn.commit()
    assert repo.cards_for(coll.id) == []
    assert LotRepository(conn).list_for_item(item.id)[0].intent is Intent.PERSONAL


def test_a_pick_cannot_be_added_twice(conn):
    """The primary key uses '' rather than NULL for "no printing", because
    SQLite treats NULLs as distinct in a unique index and would allow the
    duplicate."""
    coll = _collection(conn, template=TEMPLATE_CUSTOM, selector=None)
    repo = CollectionRepository(conn)
    repo.add_card(coll.id, "s1-1")
    repo.add_card(coll.id, "s1-1")
    conn.commit()
    assert repo.cards_for(coll.id) == [("s1-1", "")]


def test_one_card_can_be_two_slots_in_a_master_custom_collection(conn):
    coll = _collection(
        conn, template=TEMPLATE_CUSTOM, selector=None, slot_mode=SLOT_MASTER
    )
    repo = CollectionRepository(conn)
    repo.add_card(coll.id, "s1-1", "Normal")
    repo.add_card(coll.id, "s1-1", "Reverse Holofoil")
    conn.commit()
    assert len(repo.cards_for(coll.id)) == 2


def test_deleting_a_collection_takes_its_picks_and_nothing_else(conn):
    _set_of_three(conn)
    item = _own(conn, "Alpha", "Set One", "1", intent=Intent.PERSONAL)
    coll = _collection(conn, template=TEMPLATE_CUSTOM, selector=None)
    repo = CollectionRepository(conn)
    repo.add_card(coll.id, "s1-1")
    repo.delete(coll.id)
    conn.commit()
    assert repo.get(coll.id) is None
    assert repo.cards_for(coll.id) == []
    # The holding is untouched: a collection is a view, not a ledger event.
    assert LotRepository(conn).list_for_item(item.id)[0].intent is Intent.PERSONAL


# ---------------------------------------------------------------------------
# the honest gap
# ---------------------------------------------------------------------------


def test_unmatched_items_are_counted_not_hidden(conn):
    """`item` has no card_id, so ~12% of real items resolve to no slot. The
    window says so rather than dropping them."""
    _set_of_three(conn)
    _own(conn, "Alpha", "Set One", "1")
    _own(conn, "Nowhere Card", "Unlisted Set", "99")
    conn.commit()
    assert CollectionService(conn).unmatched_items() == 1


def test_the_service_is_qt_free():
    """QT-FREE-CORE."""
    import inspect

    from tcginventory.services import collections as mod

    assert "PySide6" not in inspect.getsource(mod)


def test_value_counts_only_what_is_in_the_collection(conn):
    """The grid read "Slots filled 0" beside "In collection $1,113.02" because
    value counted importable copies too — a figure contradicting its own label.
    Importable value is reported separately."""
    _set_of_three(conn)
    _own(conn, "Alpha", "Set One", "1", intent=Intent.PERSONAL, value=2500)
    _own(conn, "Beta", "Set One", "2", intent=Intent.RESALE, value=900)
    conn.commit()
    p = CollectionService(conn).progress_for(_collection(conn))
    assert p.value_cents == 2500
    assert p.importable_value_cents == 900


def test_value_counts_every_copy_held_of_a_filled_slot(conn):
    _set_of_three(conn)
    _own(conn, "Alpha", "Set One", "1", intent=Intent.PERSONAL, qty=3, value=1000)
    conn.commit()
    assert CollectionService(conn).progress_for(_collection(conn)).value_cents == 3000


# ---------------------------------------------------------------------------
# collector order
# ---------------------------------------------------------------------------


def test_collector_key_sorts_numerically_not_lexicographically():
    """A 102-card set ran 1, 10, 100, 101, 102, 11, 12 … which is useless as a
    checklist. Reverting to plain string order turns this red."""
    numbers = ["10", "2", "100", "1", "102", "11"]
    assert sorted(numbers, key=collector_key) == ["1", "2", "10", "11", "100", "102"]


def test_collector_key_keeps_lettered_runs_together_and_in_order():
    numbers = ["TG10", "TG2", "GG1", "5", "TG1"]
    assert sorted(numbers, key=collector_key) == ["5", "GG1", "TG1", "TG2", "TG10"]


def test_collector_key_handles_padding_and_letter_suffixes():
    assert collector_key("006") == collector_key("6")
    assert sorted(["195a", "195", "196"], key=collector_key) == ["195", "195a", "196"]


def test_collector_key_tolerates_a_number_with_no_digits():
    assert collector_key("")[1] == collector_key(None)[1]
    assert sorted(["", "3"], key=collector_key) == ["3", ""]


def test_a_set_collections_slots_come_out_in_collector_order(conn):
    _catalog(
        conn,
        [
            CatalogCard(
                card_id=f"s-{n}",
                name=f"C{n}",
                set_id="s",
                set_name="Big Set",
                number=str(n),
                printed_total=102,
            )
            for n in (1, 2, 10, 11, 100, 102)
        ],
    )
    slots = CollectionService(conn).slots_for(_collection(conn, selector="Big Set"))
    assert [s.number for s in slots] == ["1", "2", "10", "11", "100", "102"]
