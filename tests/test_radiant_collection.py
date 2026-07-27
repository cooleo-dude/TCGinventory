"""Radiant Collection subsets: correct pricing group, TCGplayer link, and the
collector-number denominator (regression for RC19/RC32 vs the parent's RC83)."""

from __future__ import annotations

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.domain.models import CatalogCard
from tcginventory.services.pricing import TcgcsvProvider

_SWIRLIX_URL = (
    "https://www.tcgplayer.com/product/113761/"
    "pokemon-generations-radiant-collection-swirlix"
)


def _fake_fetch(url: str):
    # Two groups share the abbreviation "GEN" — the collision that made RC cards
    # resolve to the main set and find no price.
    if url.endswith("/groups"):
        return {
            "results": [
                {"groupId": 1728, "name": "Generations", "abbreviation": "GEN"},
                {
                    "groupId": 1729,
                    "name": "Generations: Radiant Collection",
                    "abbreviation": "GEN",
                },
            ]
        }
    if url.endswith("/1729/products"):
        return {
            "results": [
                {
                    "productId": 113761,
                    "url": _SWIRLIX_URL,
                    "extendedData": [{"name": "Number", "value": "RC19/RC32"}],
                }
            ]
        }
    if url.endswith("/1729/prices"):
        return {"results": [{"productId": 113761, "marketPrice": 1.25}]}
    if url.endswith("/1728/products"):
        return {
            "results": [
                {
                    "productId": 999,
                    "url": "main-url",
                    "extendedData": [{"name": "Number", "value": "19/83"}],
                }
            ]
        }
    if url.endswith("/1728/prices"):
        return {"results": [{"productId": 999, "marketPrice": 5.00}]}
    raise AssertionError(f"unexpected url: {url}")


def test_radiant_collection_prices_via_subset_group():
    prov = TcgcsvProvider(fetch_json=_fake_fetch)
    result = prov.price_for_card("Generations", "RC19", "Swirlix", category=3)
    assert result is not None
    cents, url = result
    assert cents == 125
    assert url == _SWIRLIX_URL  # so the autofill can store the TCGplayer link


def test_numeric_card_still_resolves_parent_group():
    prov = TcgcsvProvider(fetch_json=_fake_fetch)
    # A non-RC card must still resolve to the main "Generations" group.
    assert prov.price_for_card("Generations", "19", "SomeCard", category=3) == (
        500,
        "main-url",
    )


def _seed_generations(conn):
    cards = [
        CatalogCard(
            card_id=f"g1-RC{i}", name=f"RadiantCard{i}", set_id="g1",
            set_name="Generations", set_code="GEN", number=f"RC{i}",
            printed_total=83, language="EN",
        )
        for i in range(1, 33)  # RC1..RC32
    ]
    cards.append(
        CatalogCard(
            card_id="g1-1", name="MainCard", set_id="g1", set_name="Generations",
            set_code="GEN", number="1", printed_total=83, language="EN",
        )
    )
    CardCatalogRepository(conn).replace_all(cards, "t")
    conn.commit()


def test_display_number_uses_subset_denominator(conn):
    _seed_generations(conn)
    repo = CardCatalogRepository(conn)
    # RC card → subset size (32), not the parent set's printed_total (83).
    assert repo.display_number(repo.get("g1-RC19")) == "RC19/RC32"
    # Numeric card → parent total, unchanged.
    assert repo.display_number(repo.get("g1-1")) == "1/83"
