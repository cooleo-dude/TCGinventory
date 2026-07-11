"""Tests for tcgcsv pricing provider + service, using a fake fetcher (offline)."""

from __future__ import annotations

from tcginventory.data.repositories import ItemRepository, PriceSnapshotRepository
from tcginventory.domain.enums import Category
from tcginventory.domain.models import Item
from tcginventory.services.pricing import PricingService, TcgcsvProvider

_PIKACHU_URL = (
    "https://www.tcgplayer.com/product/250303/pokemon-celebrations-pikachu"
    "?Condition=Near+Mint&Language=English&page=1"
)


def _fake_fetch(url: str):
    if url.endswith("/groups"):
        return {
            "results": [
                {"groupId": 3170, "name": "Celebrations", "abbreviation": "CEL"}
            ]
        }
    if url.endswith("/3170/prices"):
        return {
            "results": [
                {"productId": 250303, "marketPrice": 4.25, "subTypeName": "Normal"},
                {"productId": 250303, "marketPrice": 9.99, "subTypeName": "Holofoil"},
                {"productId": 777, "marketPrice": None},
            ]
        }
    raise AssertionError(f"unexpected url: {url}")


def _pikachu(**kw) -> Item:
    base = dict(
        name="Pikachu",
        category=Category.SINGLE,
        set_code="CEL",
        set_name="Celebrations",
        card_number="005/025",
        tcgplayer_url=_PIKACHU_URL,
    )
    base.update(kw)
    return Item(**base)


def test_provider_joins_price_by_product_id(conn):
    item = ItemRepository(conn).add(_pikachu())
    quotes = TcgcsvProvider(fetch_json=_fake_fetch).quotes_for([item])
    assert len(quotes) == 1
    # Highest sub-type market price wins (999c holofoil over 425c normal).
    assert quotes[0].price_cents == 999
    assert quotes[0].item_id == item.id


def test_refresh_writes_snapshot_and_updates_current_value(conn):
    items = ItemRepository(conn)
    item = items.add(_pikachu())
    svc = PricingService(conn, provider=TcgcsvProvider(fetch_json=_fake_fetch))

    summary = svc.refresh(as_of="2026-07-10T12:00:00+00:00")
    assert summary.priced == 1
    assert summary.unmapped == []

    history = PriceSnapshotRepository(conn).history_for_item(item.id)
    assert len(history) == 1
    assert history[0].price_cents == 999
    assert history[0].source == "tcgcsv"
    # Current market value on the item is updated for the dashboard/columns.
    assert items.get(item.id).market_value_cents == 999


def test_unmapped_items_reported(conn):
    items = ItemRepository(conn)
    items.add(_pikachu())
    items.add(_pikachu(name="No URL", tcgplayer_url=None))  # can't map
    svc = PricingService(conn, provider=TcgcsvProvider(fetch_json=_fake_fetch))

    summary = svc.refresh()
    assert summary.priced == 1
    assert "No URL" in summary.unmapped


def test_network_error_is_captured_not_raised(conn):
    ItemRepository(conn).add(_pikachu())

    def boom(url):
        raise RuntimeError("offline")

    summary = PricingService(conn, provider=TcgcsvProvider(fetch_json=boom)).refresh()
    assert summary.priced == 0
    assert summary.errors and "offline" in summary.errors[0]


def test_group_resolves_despite_setcode_prefix(conn):
    """tcgcsv names sets like 'SV09: Journey Together'; a plain set_name with
    no set_code must still resolve to the group (regression)."""

    def fetch(url: str):
        if url.endswith("/groups"):
            return {
                "results": [
                    {
                        "groupId": 24073,
                        "name": "SV09: Journey Together",
                        "abbreviation": "JTG",
                    }
                ]
            }
        if url.endswith("/24073/prices"):
            return {"results": [{"productId": 623590, "marketPrice": 6.10}]}
        raise AssertionError(f"unexpected url: {url}")

    item = ItemRepository(conn).add(
        Item(
            name="Kilowattrel",
            category=Category.SINGLE,
            set_name="Journey Together",  # no set_code
            card_number="163/159",
            tcgplayer_url="https://www.tcgplayer.com/product/623590/x",
        )
    )
    quotes = TcgcsvProvider(fetch_json=fetch).quotes_for([item])
    assert len(quotes) == 1
    assert quotes[0].price_cents == 610


def test_group_resolves_by_closest_match(conn):
    """A set_name that isn't an exact match still resolves to the nearest
    group (fuzzy fallback); the price join by product ID stays correct."""

    def fetch(url: str):
        if url.endswith("/groups"):
            return {
                "results": [
                    {"groupId": 100, "name": "SV: Scarlet & Violet 151"},
                    {"groupId": 24073, "name": "SV09: Journey Together"},
                ]
            }
        if url.endswith("/24073/prices"):
            return {"results": [{"productId": 623590, "marketPrice": 6.10}]}
        if url.endswith("/100/prices"):
            return {"results": []}
        raise AssertionError(f"unexpected url: {url}")

    item = ItemRepository(conn).add(
        Item(
            name="Kilowattrel",
            category=Category.SINGLE,
            set_name="Journey Togethar",  # typo, no set_code, non-exact
            card_number="163/159",
            tcgplayer_url="https://www.tcgplayer.com/product/623590/x",
        )
    )
    quotes = TcgcsvProvider(fetch_json=fetch).quotes_for([item])
    assert len(quotes) == 1
    assert quotes[0].price_cents == 610


def test_latest_prices_and_history_since(conn):
    item = ItemRepository(conn).add(_pikachu())
    snaps = PriceSnapshotRepository(conn)
    svc = PricingService(conn, provider=TcgcsvProvider(fetch_json=_fake_fetch))
    svc.refresh(as_of="2026-05-01T00:00:00+00:00")
    svc.refresh(as_of="2026-07-01T00:00:00+00:00")

    assert snaps.latest_prices()[item.id] == 999
    recent = snaps.history_for_item(item.id, since="2026-06-01")
    assert len(recent) == 1  # only the July snapshot is within range
