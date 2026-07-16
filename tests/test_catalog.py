"""Reference card catalog: pull/update from the dataset + autocomplete queries."""

from __future__ import annotations

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.services.catalog import update_catalog, update_catalog_ja

# tcgcsv category 85 ("Pokemon Japan") shape.
_JA_GROUPS = {"results": [{"groupId": 24711, "name": "M5: Abyss Eye",
                           "abbreviation": "M5"}]}
_JA_PRODUCTS = {
    "results": [
        {"productId": 694993, "name": "Energy Switch",
         "imageUrl": "https://tcgplayer-cdn.tcgplayer.com/product/694993_200w.jpg",
         "extendedData": [{"name": "Number", "value": "103/081"},
                          {"name": "Rarity", "value": "Super Rare"}]},
        # tcgcsv embeds the number to disambiguate same-named cards; the pull
        # must strip that "- <number>" segment (suffix form)…
        {"productId": 694994, "name": "Mega Darkrai ex - 046/081",
         "imageUrl": "https://x/694994.jpg",
         "extendedData": [{"name": "Number", "value": "046/081"}]},
        # …and mid-name form, keeping the trailing variant label.
        {"productId": 694995, "name": "Landorus - 398/742 (Mirror Holo)",
         "imageUrl": "https://x/694995.jpg",
         "extendedData": [{"name": "Number", "value": "398/742"}]},
        # A sealed product (no collector Number) must be skipped.
        {"productId": 999, "name": "Abyss Eye Booster Box",
         "imageUrl": "https://x/999.jpg", "extendedData": []},
    ]
}


def _fake_ja_fetch(url: str):
    if url.endswith("/85/groups"):
        return _JA_GROUPS
    if url.endswith("/85/24711/products"):
        return _JA_PRODUCTS
    raise AssertionError(f"unexpected url: {url}")

_SETS = [
    {"id": "sv9", "name": "Journey Together", "ptcgoCode": "JTG",
     "printedTotal": 159},
    {"id": "cel25", "name": "Celebrations", "ptcgoCode": "CEL",
     "printedTotal": 25},
]
_CARDS = {
    "sv9": [
        {"id": "sv9-163", "name": "Iono's Kilowattrel", "number": "163",
         "rarity": "Illustration Rare", "supertype": "Pokémon"},
        {"id": "sv9-1", "name": "Pikachu", "number": "1", "supertype": "Pokémon"},
    ],
    "cel25": [
        {"id": "cel25-5", "name": "Pikachu", "number": "5", "supertype": "Pokémon"},
    ],
}


def _fake_fetch(url: str):
    if url.endswith("/sets/en.json"):
        return _SETS
    for set_id, cards in _CARDS.items():
        if url.endswith(f"/cards/en/{set_id}.json"):
            return cards
    raise AssertionError(f"unexpected url: {url}")


def test_update_catalog_populates(conn):
    summary = update_catalog(conn, fetch_json=_fake_fetch)
    assert summary.sets == 2
    assert summary.cards == 3
    assert summary.errors == []
    assert CardCatalogRepository(conn).count() == 3


def test_catalog_autocomplete_queries(conn):
    update_catalog(conn, fetch_json=_fake_fetch)
    repo = CardCatalogRepository(conn)

    assert repo.distinct_names("pik") == ["Pikachu"]  # case-insensitive, deduped
    assert "Iono's Kilowattrel" in repo.distinct_names("Iono")

    # Picking a name yields every printing (for set/number auto-fill choices).
    pikachus = repo.find_by_name("Pikachu")
    assert {(c.set_name, c.number) for c in pikachus} == {
        ("Journey Together", "1"),
        ("Celebrations", "5"),
    }
    (kilo,) = repo.find_by_name("Iono's Kilowattrel")
    assert (kilo.set_code, kilo.number, kilo.rarity) == (
        "JTG", "163", "Illustration Rare",
    )


def test_search_enriches_with_set_and_number(conn):
    update_catalog(conn, fetch_json=_fake_fetch)
    repo = CardCatalogRepository(conn)

    # name + set narrows to the Celebrations Pikachu (#5), not the sv9 one (#1).
    (hit,) = repo.search("Pikachu Celebrations")
    assert (hit.set_name, hit.number) == ("Celebrations", "5")

    # name + number (XX/YY form) narrows by collector number.
    (hit,) = repo.search("Pikachu 1/99")
    assert (hit.set_name, hit.number) == ("Journey Together", "1")

    # name only returns every printing.
    assert len(repo.search("Pikachu")) == 2
    # partial name still matches (owner prefix).
    assert repo.search("Kilowattrel")[0].name == "Iono's Kilowattrel"

    # Tokens work in ANY order, and the number may be bare (no denominator).
    for q in ("Pikachu Celebrations 5", "Pikachu 5 Celebrations",
              "5 Celebrations Pikachu"):
        (hit,) = repo.search(q)
        assert (hit.set_name, hit.number) == ("Celebrations", "5"), q


def test_search_matches_lettered_number_prefix(conn):
    from tcginventory.domain.models import CatalogCard

    CardCatalogRepository(conn).replace_all(
        [
            CatalogCard(card_id="a", name="Falinks", set_id="s1",
                        set_name="Shining Fates Shiny Vault", number="SV074"),
            CatalogCard(card_id="b", name="Falinks", set_id="s2",
                        set_name="Rebel Clash", number="109"),
        ],
        "t",
    )
    repo = CardCatalogRepository(conn)

    # A lettered number can be typed partially — "SV", "SV0", "SV074" all narrow.
    for q in ("Falinks SV", "Falinks SV0", "Falinks SV074"):
        assert [c.number for c in repo.search(q)] == ["SV074"], q
    # A bare digit stays exact, so "10" does NOT surface "109".
    assert repo.search("Falinks 10") == []
    assert repo.search("Falinks 109")[0].number == "109"


def test_full_number_combines_number_with_set_total():
    from tcginventory.domain.models import CatalogCard

    def card(number, total):
        return CatalogCard(card_id="x", name="n", set_id="s", number=number,
                           printed_total=total)

    # plain numerator → numeric denominator; lettered numerator keeps its prefix.
    assert card("124", 142).full_number == "124/142"
    assert card("SV103", 122).full_number == "SV103/SV122"
    assert card("TG10", 30).full_number == "TG10/TG30"
    # unknown total or missing number degrade gracefully.
    assert card("124", None).full_number == "124"
    assert card(None, 142).full_number is None


def test_update_catalog_stores_printed_total(conn):
    update_catalog(conn, fetch_json=_fake_fetch)
    (kilo,) = CardCatalogRepository(conn).find_by_name("Iono's Kilowattrel")
    assert kilo.printed_total == 159
    assert kilo.full_number == "163/159"


def test_update_catalog_ja_pulls_japanese(conn):
    summary = update_catalog_ja(conn, fetch_json=_fake_ja_fetch)
    # Four products, but the sealed booster box (no Number) is skipped.
    assert summary.sets == 1 and summary.cards == 3 and summary.errors == []
    repo = CardCatalogRepository(conn)
    (card,) = repo.find_by_name("Energy Switch")
    assert card.language == "JP"
    assert card.number == "103/081"
    assert card.set_code == "M5"
    assert card.rarity == "Super Rare"
    assert card.image_small.endswith("/product/694993_200w.jpg")

    # The "- <number>" segment tcgcsv embeds in the name is stripped, in both
    # the suffix form and the mid-name form (variant label kept).
    (darkrai,) = repo.find_by_name("Mega Darkrai ex")
    assert darkrai.number == "046/081"
    (landorus,) = repo.find_by_name("Landorus (Mirror Holo)")
    assert landorus.number == "398/742"


def test_en_and_ja_catalogs_coexist(conn):
    update_catalog(conn, fetch_json=_fake_fetch)  # 3 EN cards
    update_catalog_ja(conn, fetch_json=_fake_ja_fetch)  # 3 JP cards (box skipped)
    repo = CardCatalogRepository(conn)
    assert repo.count() == 6
    # A JP re-pull must not wipe the English rows (replace_for_language).
    update_catalog_ja(conn, fetch_json=_fake_ja_fetch)
    assert repo.count() == 6
    # The Japanese card is reachable and tagged JP.
    (hit,) = repo.search("Energy Switch")
    assert hit.language == "JP"
    # …and by set code + full number.
    assert repo.search("M5 103/081")[0].name == "Energy Switch"


def test_update_catalog_replaces_and_skips_bad_set(conn):
    update_catalog(conn, fetch_json=_fake_fetch)

    def flaky(url: str):
        if url.endswith("/cards/en/cel25.json"):
            raise RuntimeError("boom")
        return _fake_fetch(url)

    summary = update_catalog(conn, fetch_json=flaky)
    assert summary.cards == 2  # only sv9's cards survived
    assert summary.errors and "cel25" in summary.errors[0]
    assert CardCatalogRepository(conn).count() == 2  # replaced, not appended
