"""Catalog-wide price sweep + price-history DB (offline via a fake fetcher)."""

from __future__ import annotations

from datetime import date, timedelta

from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.data.pricehistory import PriceHistoryRepository, open_history
from tcginventory.data.repositories import CardCatalogRepository, ItemRepository
from tcginventory.domain.enums import Category, Language
from tcginventory.domain.models import CatalogCard, Item
from tcginventory.services.marketprices import sweep
from tcginventory.services.pricing import TcgcsvProvider


def _fetch(url: str):
    if url.endswith("/3/groups"):
        return {
            "results": [
                {
                    "groupId": 24073,
                    "name": "SV09: Journey Together",
                    "abbreviation": "JTG",
                }
            ]
        }
    if url.endswith("/3/24073/products"):
        return {
            "results": [
                {
                    "productId": 623590,
                    "url": "https://tcgplayer.com/product/623590/x",
                    "extendedData": [{"name": "Number", "value": "1/159"}],
                }
            ]
        }
    if url.endswith("/3/24073/prices"):
        return {"results": [{"productId": 623590, "marketPrice": 6.10}]}
    if url.endswith("/85/24711/prices"):
        return {"results": [{"productId": 694993, "marketPrice": 0.91}]}
    raise AssertionError(f"unexpected url: {url}")


def _seed(conn) -> None:
    CardCatalogRepository(conn).replace_all(
        [
            CatalogCard(
                card_id="sv9-1",
                name="Pikachu",
                set_id="sv9",
                set_name="Journey Together",
                set_code="JTG",
                number="1",
                language="EN",
            ),
            CatalogCard(
                card_id="jp-694993",
                name="Energy Switch",
                set_id="24711",
                set_name="M5: Abyss Eye",
                set_code="M5",
                number="103/081",
                language="JP",
            ),
        ],
        "t",
    )
    # An owned card in the Journey Together set → that set is priced first.
    ItemRepository(conn).add(
        Item(
            name="Pikachu",
            category=Category.SINGLE,
            set_name="Journey Together",
            card_number="1",
            language=Language.EN,
        )
    )
    conn.commit()


def _dbs():
    main = connect(":memory:")
    migrate(main)
    _seed(main)
    return main, open_history(":memory:")


def test_sweep_prices_en_and_jp():
    main, hist_conn = _dbs()
    provider = TcgcsvProvider(fetch_json=_fetch)
    summary = sweep(main, hist_conn, provider, as_of="2026-07-16")
    assert summary.sets == 2
    assert summary.priced == 2
    assert summary.snapshots == 2 and summary.errors == []

    hist = PriceHistoryRepository(hist_conn)
    assert hist.current_price("sv9-1") == 610  # EN via products number-join
    assert hist.current_price("jp-694993") == 91  # JP via productId in card_id


def test_sweep_dedups_unchanged_and_records_changes():
    main, hist_conn = _dbs()
    sweep(main, hist_conn, TcgcsvProvider(fetch_json=_fetch), as_of="2026-07-16")
    # Same prices next day → no new snapshots.
    again = sweep(
        main, hist_conn, TcgcsvProvider(fetch_json=_fetch), as_of="2026-07-17"
    )
    assert again.snapshots == 0

    # Price moves → exactly one new snapshot for the changed card.
    def moved(url: str):
        if url.endswith("/85/24711/prices"):
            return {"results": [{"productId": 694993, "marketPrice": 1.50}]}
        return _fetch(url)

    changed = sweep(
        main, hist_conn, TcgcsvProvider(fetch_json=moved), as_of="2026-07-18"
    )
    assert changed.snapshots == 1
    hist = PriceHistoryRepository(hist_conn)
    assert hist.current_price("jp-694993") == 150
    assert hist.history_for_card("jp-694993") == [
        ("2026-07-16", 91),
        ("2026-07-18", 150),
    ]


def test_sweep_orders_owned_sets_first():
    main, hist_conn = _dbs()
    # No prices resolve (empty fetch) — we only check the processing order.
    seen: list[str] = []

    def spy(url: str):
        seen.append(url)
        return {"results": []}

    sweep(main, hist_conn, TcgcsvProvider(fetch_json=spy), as_of="2026-07-16")
    # The owned set (Journey Together / EN) is resolved before the JP set.
    assert seen.index("https://tcgcsv.com/tcgplayer/3/groups") < seen.index(
        "https://tcgcsv.com/tcgplayer/85/24711/prices"
    )


def test_prune_drops_history_older_than_a_year():
    hist_conn = open_history(":memory:")
    hist = PriceHistoryRepository(hist_conn)
    old = (date.today() - timedelta(days=400)).isoformat()
    recent = (date.today() - timedelta(days=10)).isoformat()
    hist.record("c1", 100, old)
    hist.record("c1", 200, recent)  # a change → a second snapshot
    hist_conn.commit()
    removed = hist.prune((date.today() - timedelta(days=365)).isoformat())
    assert removed == 1
    assert hist.history_for_card("c1") == [(recent, 200)]
    assert hist.current_price("c1") == 200  # latest is untouched by prune
