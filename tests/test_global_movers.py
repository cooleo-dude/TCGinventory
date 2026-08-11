"""Global Top Gainers / Losers: the catalog, with what you hold marked on it.

The point of the scope is that it answers a question the owned path structurally
cannot — `ReportingService._price_movers` opens `FROM lot l JOIN item i`, so it can
only ever describe held cards. These tests pin the two things that makes possible: a
row for a card no lot has touched, and an ownership marker on the ones you do hold.

Headless and Qt-free, per `QT-FREE-CORE`.
"""

from __future__ import annotations

from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.data.pricehistory import PriceHistoryRepository, open_history
from tcginventory.data.repositories import (
    CardCatalogRepository,
    ItemRepository,
    LotRepository,
)
from tcginventory.domain.enums import Category, Intent
from tcginventory.domain.models import CatalogCard, Item, Lot
from tcginventory.services.globalmovers import GlobalMoversService


def _catalog_card(card_id: str, name: str, number: str) -> CatalogCard:
    return CatalogCard(
        card_id=card_id,
        name=name,
        set_id="base1",
        set_name="Base Set",
        number=number,
        printed_total=102,
        language="EN",
    )


def _fixture(prices: dict[str, list[tuple[str, int]]], *, cards=None):
    """A ledger with a catalog, plus a price history. Returns both connections."""
    ledger = connect(":memory:")
    migrate(ledger)
    CardCatalogRepository(ledger).replace_all(
        cards
        if cards is not None
        else [
            _catalog_card("base1-4", "Charizard", "4"),
            _catalog_card("base1-58", "Pikachu", "58"),
        ],
        "2026-08-11",
    )
    history = open_history(":memory:")
    repo = PriceHistoryRepository(history)
    for card_id, points in prices.items():
        for as_of, cents in points:
            repo.record(card_id, cents, as_of)
    return history, ledger


def _hold(ledger, name: str, number: str, qty: int, intent: Intent = Intent.RESALE):
    """An item with a lot, so it counts as held."""
    items = ItemRepository(ledger)
    item = items.add(
        Item(
            id=None,
            name=name,
            category=Category.SINGLE,
            set_name="Base Set",
            card_number=number,
        )
    )
    LotRepository(ledger).add(
        Lot(
            item_id=item.id,
            acquired_at="2026-07-01",
            quantity=qty,
            remaining_qty=qty,
            unit_cost_cents=1_000,
            acquired_market_value_cents=1_000,
            intent=intent,
        )
    )
    return item


def test_a_card_nobody_holds_still_appears():
    """The whole reason this scope exists. Charizard is in no lot, so the owned path
    cannot see it at all; global must."""
    history, ledger = _fixture(
        {"base1-4": [("2026-07-01", 10_000), ("2026-08-01", 20_000)]}
    )

    gainers, losers = GlobalMoversService(history, ledger).movers("2026-07-01")

    assert [r.name for r in gainers] == ["Charizard"]
    assert losers == []
    row = gainers[0]
    assert row.item_id is None, "a card no lot holds must not be navigable"
    assert row.card_id == "base1-4"
    assert row.owned_qty == 0
    assert row.change_cents == 10_000 and row.change_pct == 100.0


def test_a_card_you_hold_is_marked_and_stays_navigable():
    """Decision 2: the marker is what makes a global row answer "should I care?".
    Decision 3 hangs off `item_id` — held cards keep the Inventory jump."""
    history, ledger = _fixture(
        {"base1-58": [("2026-07-01", 1_000), ("2026-08-01", 3_000)]}
    )
    item = _hold(ledger, "Pikachu", "58", qty=3)

    gainers, _ = GlobalMoversService(history, ledger).movers("2026-07-01")

    assert len(gainers) == 1
    assert gainers[0].owned_qty == 3, "held units not marked on the global row"
    assert gainers[0].item_id == item.id, "a held card must stay reachable"


def test_units_are_summed_when_two_items_resolve_to_one_catalog_card():
    """A raw and a graded copy are two items and one card. Marking "you hold 2" and
    then "you hold 3" for the same row would be nonsense, and picking one arbitrarily
    would under-report."""
    history, ledger = _fixture(
        {"base1-58": [("2026-07-01", 1_000), ("2026-08-01", 2_000)]}
    )
    _hold(ledger, "Pikachu", "58", qty=2)
    _hold(ledger, "Pikachu", "58", qty=4)

    gainers, _ = GlobalMoversService(history, ledger).movers("2026-07-01")

    assert gainers[0].owned_qty == 6


def test_a_personal_copy_still_marks_the_row():
    """Deliberately unlike the owned scope, which excludes personal and ripping lots
    because it is a resale view. The marker answers "is this in my collection", and a
    personal copy is."""
    history, ledger = _fixture(
        {"base1-58": [("2026-07-01", 1_000), ("2026-08-01", 2_000)]}
    )
    _hold(ledger, "Pikachu", "58", qty=1, intent=Intent.PERSONAL)

    gainers, _ = GlobalMoversService(history, ledger).movers("2026-07-01")

    assert gainers[0].owned_qty == 1


def test_gainers_and_losers_are_split_and_each_ordered_by_size():
    history, ledger = _fixture(
        {
            "base1-4": [("2026-07-01", 10_000), ("2026-08-01", 30_000)],  # +200%
            "base1-58": [("2026-07-01", 4_000), ("2026-08-01", 1_000)],  # -75%
        }
    )

    gainers, losers = GlobalMoversService(history, ledger).movers("2026-07-01")

    assert [r.name for r in gainers] == ["Charizard"]
    assert [r.name for r in losers] == ["Pikachu"]
    assert gainers[0].change_cents > 0 > losers[0].change_cents


def test_losers_are_ordered_biggest_fall_first():
    """`movers_since` returns one list sorted descending, so the losers arrive
    smallest-fall-first and have to be reversed. Getting this wrong puts the least
    interesting row at the top of the panel."""
    cards = [
        _catalog_card("base1-4", "Charizard", "4"),
        _catalog_card("base1-58", "Pikachu", "58"),
        _catalog_card("base1-2", "Blastoise", "2"),
    ]
    history, ledger = _fixture(
        {
            "base1-4": [("2026-07-01", 10_000), ("2026-08-01", 9_000)],  # -10%
            "base1-58": [("2026-07-01", 10_000), ("2026-08-01", 2_500)],  # -75%
            "base1-2": [("2026-07-01", 10_000), ("2026-08-01", 5_000)],  # -50%
        },
        cards=cards,
    )

    _, losers = GlobalMoversService(history, ledger).movers("2026-07-01", limit=None)

    assert [r.name for r in losers] == ["Pikachu", "Blastoise", "Charizard"]


def test_a_card_the_catalog_cannot_name_is_still_reported():
    """The movement is real. Dropping the row would silently shorten the list, and the
    card id is a poor label but an honest one."""
    history, ledger = _fixture(
        {"ghost-99": [("2026-07-01", 1_000), ("2026-08-01", 4_000)]}
    )

    gainers, _ = GlobalMoversService(history, ledger).movers("2026-07-01")

    assert len(gainers) == 1
    assert gainers[0].name == "ghost-99"
    assert gainers[0].set_name is None
    assert gainers[0].item_id is None


def test_an_iso_timestamp_cutoff_is_truncated_to_a_date():
    """The Dashboard's range selector produces `datetime.isoformat()`, and the history
    is date-only. Passed through whole, string comparison against 'YYYY-MM-DD' would
    silently exclude the cutoff day."""
    history, ledger = _fixture(
        {"base1-4": [("2026-07-01", 1_000), ("2026-08-01", 2_000)]}
    )

    svc = GlobalMoversService(history, ledger)
    stamped, _ = svc.movers("2026-07-01T14:23:59.123456")
    plain, _ = svc.movers("2026-07-01")

    assert [r.card_id for r in stamped] == [r.card_id for r in plain]
    assert stamped and stamped[0].change_pct == 100.0


def test_an_empty_price_history_yields_nothing():
    """A fresh install where the sweep has never run. The panel shows its empty state
    rather than the service raising."""
    history, ledger = _fixture({})

    assert GlobalMoversService(history, ledger).movers("2026-07-01") == ([], [])


def test_the_floor_is_applied_and_is_overridable():
    """The defaults are measured, not sacred — and a test that could not change them
    would be pinning the constant rather than the behaviour."""
    history, ledger = _fixture({"base1-4": [("2026-07-01", 20), ("2026-08-01", 80)]})

    assert GlobalMoversService(history, ledger).movers("2026-07-01") == ([], [])
    loose = GlobalMoversService(history, ledger, floor_cents=0)
    gainers, _ = loose.movers("2026-07-01")
    assert [r.name for r in gainers] == ["Charizard"]
