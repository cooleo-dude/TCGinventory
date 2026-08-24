"""Display formatting — money, percent, and the give-side item label.

Pure and Qt-free: ``ui/formatting.py`` deliberately imports no PySide6 so this
runs without a display or an app fixture.
"""

from __future__ import annotations

from tcginventory.domain.enums import (
    Category,
    Condition,
    Edition,
    GradingCompany,
    HoloPattern,
    Language,
)
from tcginventory.domain.models import Item
from tcginventory.ui.formatting import item_label, item_labels, money, percent


def _card(name="Haunter", **kwargs) -> Item:
    kwargs.setdefault("set_name", "Fossil")
    kwargs.setdefault("card_number", "6/62")
    return Item(name=name, category=Category.SINGLE, **kwargs)


def test_money_and_percent():
    assert money(-500) == "-$5.00"
    assert money(None) == ""
    assert percent(33.333) == "33.3%"
    assert percent(None) == ""


def test_label_shows_set_and_number_for_a_lone_card():
    """The number is unconditional — a give row must say which printing it is."""
    assert item_label(_card(condition=Condition.NM), []) == "Haunter (Fossil · 6/62)"


def test_label_falls_back_to_the_bare_name():
    """No set and no number must not leave stray punctuation or eat the name."""
    bare = Item(name="Mystery Lot", category=Category.SEALED)
    assert item_label(bare, [bare]) == "Mystery Lot"


def test_label_separates_two_conditions():
    """The reported case: two Haunters differing only in wear.

    Both rendered as plain "Haunter (Fossil)" before, so the picker gave no way
    to see which lot was about to be drawn down.
    """
    mp = _card(condition=Condition.MP)
    lp = _card(condition=Condition.LP)
    siblings = [mp, lp]

    assert item_label(mp, siblings) == "Haunter (Fossil · 6/62 · MP)"
    assert item_label(lp, siblings) == "Haunter (Fossil · 6/62 · LP)"


def test_label_leaves_agreeing_fields_off():
    """Only a field that actually varies earns a slot — no blanket noise."""
    a = _card(condition=Condition.NM, holo_pattern=HoloPattern.NON_HOLO)
    b = _card(condition=Condition.LP, holo_pattern=HoloPattern.NON_HOLO)

    # Holo agrees, so it stays off even though the pair collides on condition.
    assert item_label(a, [a, b]) == "Haunter (Fossil · 6/62 · NM)"


def test_label_always_shows_a_grade_even_when_nothing_collides():
    """A slab is a different thing to sell, not a tie-breaker.

    These two are told apart only by a mis-keyed collector number, so an
    on-collision rule would suppress the grade entirely and let a PSA 10 be
    picked for a raw card's line. Real rows: the author's two Pawmi.
    """
    slab = _card("Pawmi", set_name="Paldean Fates", card_number="226/091")
    slab.is_graded = True
    slab.grading_company = GradingCompany.PSA
    slab.grade = "10"
    raw = _card("Pawmi", set_name="Paldean Fates", card_number="226/91")
    raw.condition = Condition.NM

    assert item_label(slab, [slab, raw]) == "Pawmi (Paldean Fates · 226/091 · PSA 10)"
    assert item_label(raw, [slab, raw]) == "Pawmi (Paldean Fates · 226/91)"


def test_label_disambiguates_holo_edition_and_language():
    a = _card(holo_pattern=HoloPattern.HOLO, edition=Edition.FIRST)
    b = _card(holo_pattern=HoloPattern.REVERSE_HOLO, edition=Edition.UNLIMITED)
    c = _card(
        holo_pattern=HoloPattern.HOLO, edition=Edition.FIRST, language=Language.JP
    )

    labels = {item_label(x, [a, b, c]) for x in (a, b, c)}
    assert len(labels) == 3, labels
    assert "Haunter (Fossil · 6/62 · H · 1st Edition · EN)" in labels
    assert "Haunter (Fossil · 6/62 · RH · Unlimited · EN)" in labels
    assert "Haunter (Fossil · 6/62 · H · 1st Edition · JP)" in labels


def test_label_keeps_the_user_note_without_the_review_marker():
    from tcginventory.services.review import with_review_note

    card = _card(notes=with_review_note("Signed by artist"))
    label = item_label(card, [card])

    assert label == "Haunter (Fossil · 6/62) — Signed by artist"
    assert "Manual price" not in label


def test_batched_labels_match_the_one_at_a_time_answers():
    """``item_labels`` is an optimisation, so it must not be a behaviour change."""
    cards = [
        _card(condition=Condition.NM),
        _card(condition=Condition.LP),
        _card("Pikachu", set_name="Celebrations", card_number="5"),
        _card(
            "Pikachu", set_name="Celebrations", card_number="5", language=Language.JP
        ),
        Item(name="Bulk Lot", category=Category.SEALED),
    ]
    assert item_labels(cards) == [item_label(c, cards) for c in cards]


def test_batched_labels_do_not_rescan_per_item():
    """The regression guard for the lag this replaced.

    Deriving labels one at a time is quadratic — each rescans every sibling — so
    a 2,000-card picker cost seconds of UI-thread work on every keystroke.

    Measured on the dev box: batched 0.002s, one-at-a-time 1.09s. The bound sits
    between them with room either way — ~100x slack over the linear pass so a
    slow machine cannot trip it, and ~4x under the quadratic one so a regression
    cannot slip beneath it.
    """
    import time

    cards = [_card(card_number=f"{n}/300", condition=Condition.NM) for n in range(2000)]
    start = time.perf_counter()
    labels = item_labels(cards)
    elapsed = time.perf_counter() - start

    assert len(labels) == 2000
    assert elapsed < 0.25, f"{elapsed:.2f}s — batching looks to have regressed"


def test_every_distinct_item_gets_a_distinct_label():
    """The property the picker depends on, over a group that collides every way.

    A label that repeats is a lot the user cannot choose deliberately, which is
    the whole defect — so this asserts uniqueness rather than any one string.
    """
    cards = [
        _card(condition=Condition.NM),
        _card(condition=Condition.LP),
        _card(condition=Condition.NM, holo_pattern=HoloPattern.REVERSE_HOLO),
        _card(condition=Condition.NM, edition=Edition.FIRST),
        _card(condition=Condition.NM, language=Language.JP),
    ]
    slab = _card()
    slab.is_graded = True
    slab.grading_company = GradingCompany.PSA
    slab.grade = "9"
    cards.append(slab)

    labels = [item_label(c, cards) for c in cards]
    assert len(set(labels)) == len(cards), labels
