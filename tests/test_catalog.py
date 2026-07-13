"""Reference card catalog: pull/update from the dataset + autocomplete queries."""

from __future__ import annotations

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.services.catalog import update_catalog

_SETS = [
    {"id": "sv9", "name": "Journey Together", "ptcgoCode": "JTG"},
    {"id": "cel25", "name": "Celebrations", "ptcgoCode": "CEL"},
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
