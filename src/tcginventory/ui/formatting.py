"""Display formatting helpers for the UI (money, percent, item labels).

Kept tiny and Qt-free so it can be unit-tested. Money is always formatted from
integer cents — the UI never does money math, it only renders.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from tcginventory.domain.models import Item
from tcginventory.services.review import without_review_note

# What separates the parts inside a label's parenthetical, matching the catalog
# autocomplete's "name · set #number" so both search popups read alike.
_SEP = " · "


def money(cents: int | None) -> str:
    """Format integer cents as USD, e.g. ``-500`` → ``-$5.00``; ``None`` → ``""``."""
    if cents is None:
        return ""
    negative = cents < 0
    whole, frac = divmod(abs(cents), 100)
    text = f"${whole:,}.{frac:02d}"
    return f"-{text}" if negative else text


def percent(value: float | None, digits: int = 1) -> str:
    """Format a percentage value, e.g. ``33.333`` → ``33.3%``; ``None`` → ``""``."""
    if value is None:
        return ""
    return f"{value:.{digits}f}%"


def _enum_value(value) -> str | None:
    """An optional enum column as its display string; ``None`` stays ``None``."""
    return value.value if value is not None else None


def _grade(item: Item) -> str | None:
    """A slab's grade ("PSA 10"), or None for a raw card.

    Shown **unconditionally**, unlike the discriminators below. A graded card is
    not a variant of the raw one, it is a different thing to sell at a different
    price, and suppressing that whenever something else happens to disambiguate
    is how a PSA 10 gets picked for a raw card's line. On the author's ledger
    that was not hypothetical: the two Pawmi and the two Gallade rows are a slab
    beside a raw copy, told apart only by a mis-keyed collector number
    ("226/091" vs "226/91"), which would have hidden the grade entirely.
    """
    if not item.is_graded:
        return None
    parts = [_enum_value(item.grading_company), item.grade]
    return " ".join(p for p in parts if p) or "graded"


# Appended, in this order, only to tell otherwise-identical labels apart. Order
# is by how much the field moves the card's value: wear first, then foiling, then
# print run, then language. ``condition`` is NULL whenever a card is graded
# (specs/02-data-model.md), so it never doubles up with _grade.
_DISCRIMINATORS = (
    lambda item: _enum_value(item.condition),
    lambda item: _enum_value(item.holo_pattern),
    lambda item: _enum_value(item.edition),
    lambda item: _enum_value(item.language),
)


def _base_parts(item: Item) -> list[str]:
    """Set and collector number — shown for every card, always.

    The number is unconditional because it is the single most common way two
    give-side rows differ (22 of 33 colliding groups on the author's ledger), and
    without it a row gives no indication which printing is being pulled.
    """
    return [p for p in (item.set_name, item.card_number, _grade(item)) if p]


def _render(name: str, parts: list[str]) -> str:
    """``name (a · b · c)``, or bare ``name`` when there is nothing to qualify."""
    return f"{name} ({_SEP.join(parts)})" if parts else name


def _varying(group: list[Item]) -> list:
    """Which discriminators actually differ across a colliding group.

    A group of one has nothing to tell apart, so nothing is appended and the
    common case keeps a clean label.
    """
    if len(group) < 2:
        return []
    return [f for f in _DISCRIMINATORS if len({f(o) for o in group}) > 1]


def _label_for(item: Item, varying: list) -> str:
    """One item's label, given the fields its group needs to be told apart by."""
    parts = _base_parts(item)
    # A None on one side still separates it from a sibling that has a value.
    parts += [value for f in varying if (value := f(item))]
    label = _render(item.name, parts)
    note = without_review_note(item.notes)
    return f"{label} — {note}" if note else label


def item_label(item: Item, siblings: Iterable[Item] = ()) -> str:
    """A give-side label for ``item``, disambiguated against ``siblings``.

    Two *distinct* items can share a name, a set and a number and still be
    different things to sell — a PSA 10 beside a raw NM, an LP beside an MP. The
    picker used to render both as plain "Haunter (Fossil)", so the user could not
    see which lot they were about to draw down. Any field that actually differs
    within the colliding group is appended; fields that agree stay off, so the
    common case keeps a clean label.

    ``siblings`` is whatever is pickable — the give row passes its sellable
    stock — so a card is disambiguated against what it can be confused with, not
    against the whole collection. Pass nothing for a bare label.

    The user's own note, if any, is always appended (the automatic "manual price"
    review marker is stripped), since that is where a hand-written distinction
    like "signed" ends up.
    """
    base = _render(item.name, _base_parts(item))
    group = [o for o in siblings if _render(o.name, _base_parts(o)) == base]
    return _label_for(item, _varying(group))


def item_labels(items: Sequence[Item]) -> list[str]:
    """:func:`item_label` for a whole picker at once, returned in input order.

    Building a list of labels one call at a time is **quadratic** — each label
    rescans every sibling to find its collision group. At 769 sellable cards that
    was 196 ms of UI-thread work *per keystroke*, since the give row refills its
    completion model on every edit.

    Only the base decides collision, so bucket by it once and work out each
    group's varying fields once: O(n) rather than O(n²). Same answers — the
    parity is pinned by a test.
    """
    items = list(items)
    bases = [_render(i.name, _base_parts(i)) for i in items]

    groups: dict[str, list[Item]] = defaultdict(list)
    for item, base in zip(items, bases, strict=True):
        groups[base].append(item)
    varying = {base: _varying(group) for base, group in groups.items()}

    pairs = zip(items, bases, strict=True)
    return [_label_for(item, varying[base]) for item, base in pairs]
