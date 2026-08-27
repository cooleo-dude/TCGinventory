"""Which foil printings each catalog card exists in (ticket 55).

Offline: tcgcsv is injected as a fake provider. The Collections foil template
reads this table, so the query it will run is exercised directly.
"""

from __future__ import annotations

import sqlite3

import pytest

from tcginventory.data.db import connect, now_iso
from tcginventory.data.migrator import migrate
from tcginventory.data.repositories import (
    CardCatalogRepository,
    CardTreatmentRepository,
)
from tcginventory.domain.models import CatalogCard
from tcginventory.services import catalog as cat


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    yield c
    c.close()


class _Provider:
    """Enough of TcgcsvProvider for the treatment sweep."""

    def __init__(self, groups=None, products=None, treatments=None, raise_on=None):
        self._groups = groups if groups is not None else {"Base Set": 604}
        self._products = products if products is not None else {604: [(1, "4/102")]}
        self._treatments = (
            treatments if treatments is not None else {1: ["Holofoil", "Normal"]}
        )
        self._raise_on = raise_on or set()

    def resolve_group(self, set_name, set_code, category):
        if set_name in self._raise_on:
            raise RuntimeError(f"boom {set_name}")
        return self._groups.get(set_name)

    def products(self, category, group_id):
        return [(pid, num, "", "") for pid, num in self._products.get(group_id, [])]

    def treatments_by_product(self, category, group_id):
        return self._treatments


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def test_migration_0024_creates_the_table_and_its_lookup_index(conn):
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "card_catalog_treatment" in tables
    indexes = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='card_catalog_treatment'"
        )
    }
    assert "idx_treatment_lookup" in indexes


def test_a_card_can_hold_several_treatments(conn):
    """The whole reason this is a table and not a column: 45.6% of products
    carry more than one printing."""
    repo = CardTreatmentRepository(conn)
    repo.replace_for_language(
        [("sv09-001", "Normal"), ("sv09-001", "Reverse Holofoil")], "EN"
    )
    assert repo.treatments_for("sv09-001") == ["Normal", "Reverse Holofoil"]


def test_a_holo_only_card_holds_exactly_one(conn):
    repo = CardTreatmentRepository(conn)
    repo.replace_for_language([("base1-4", "Holofoil")], "EN")
    assert repo.treatments_for("base1-4") == ["Holofoil"]


def test_an_unresolved_card_reports_empty_not_a_single_treatment(conn):
    """Empty means "the pull could not say", which is different from "this card
    only comes one way" — a template must not confuse the two."""
    assert CardTreatmentRepository(conn).treatments_for("nope-1") == []


def test_a_repull_replaces_rather_than_accumulating(conn):
    repo = CardTreatmentRepository(conn)
    repo.replace_for_language([("a-1", "Normal"), ("a-1", "Reverse Holofoil")], "EN")
    repo.replace_for_language([("a-1", "Holofoil")], "EN")
    assert repo.treatments_for("a-1") == ["Holofoil"]
    assert repo.count() == 1


def test_a_repull_of_one_language_leaves_the_other_alone(conn):
    repo = CardTreatmentRepository(conn)
    repo.replace_for_language([("en-1", "Normal")], "EN")
    repo.replace_for_language([("jp-1", "Normal")], "JP")
    repo.replace_for_language([("en-2", "Holofoil")], "EN")
    assert repo.treatments_for("jp-1") == ["Normal"]
    assert repo.treatments_for("en-1") == []


def test_duplicate_pairs_collapse_rather_than_raising(conn):
    repo = CardTreatmentRepository(conn)
    repo.replace_for_language([("a-1", "Normal"), ("a-1", "Normal")], "EN")
    assert repo.count() == 1


# ---------------------------------------------------------------------------
# the query Collections will run
# ---------------------------------------------------------------------------


def _seed_catalog(conn):
    CardCatalogRepository(conn).replace_for_language(
        [
            CatalogCard(card_id="s1-1", name="A", set_id="s1"),
            CatalogCard(card_id="s1-2", name="B", set_id="s1"),
            CatalogCard(card_id="s2-1", name="C", set_id="s2"),
        ],
        "EN",
        now_iso(),
    )
    CardTreatmentRepository(conn).replace_for_language(
        [
            ("s1-1", "Normal"),
            ("s1-1", "Reverse Holofoil"),
            ("s1-2", "Holofoil"),
            ("s2-1", "Reverse Holofoil"),
        ],
        "EN",
    )


def test_card_ids_with_a_treatment_scoped_to_one_set(conn):
    """ "Every reverse holo in this set" — the Collections template query."""
    _seed_catalog(conn)
    repo = CardTreatmentRepository(conn)
    assert repo.card_ids_with("Reverse Holofoil", set_id="s1") == ["s1-1"]
    assert repo.card_ids_with("Reverse Holofoil") == ["s1-1", "s2-1"]
    assert repo.card_ids_with("Holofoil", set_id="s1") == ["s1-2"]


def test_treatments_for_many_batches_without_losing_anyone(conn):
    _seed_catalog(conn)
    got = CardTreatmentRepository(conn).treatments_for_many(["s1-1", "s1-2", "gone"])
    assert got == {"s1-1": ["Normal", "Reverse Holofoil"], "s1-2": ["Holofoil"]}


def test_treatments_for_many_survives_more_ids_than_sqlites_parameter_cap(conn):
    """The batch chunks at 500; a Collections grid can ask for a whole set."""
    repo = CardTreatmentRepository(conn)
    repo.replace_for_language([(f"c-{i}", "Normal") for i in range(1200)], "EN")
    got = repo.treatments_for_many([f"c-{i}" for i in range(1200)])
    assert len(got) == 1200


def test_distinct_treatments_lists_the_vocabulary(conn):
    _seed_catalog(conn)
    assert CardTreatmentRepository(conn).distinct_treatments("EN") == [
        "Holofoil",
        "Normal",
        "Reverse Holofoil",
    ]


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------


def test_the_sweep_joins_cards_to_products_by_collector_number():
    cards = [
        CatalogCard(
            card_id="base1-4",
            name="Charizard",
            set_id="base1",
            set_name="Base Set",
            number="4",
        ),
    ]
    rows, errors = cat._sweep_treatments(cards, _Provider())
    assert errors == []
    assert sorted(rows) == [("base1-4", "Holofoil"), ("base1-4", "Normal")]


def test_a_card_with_no_matching_product_is_skipped_silently():
    """Not an error — plenty of catalog cards have no tcgcsv product."""
    cards = [
        CatalogCard(
            card_id="base1-9",
            name="X",
            set_id="base1",
            set_name="Base Set",
            number="999",
        ),
    ]
    rows, errors = cat._sweep_treatments(cards, _Provider())
    assert rows == []
    assert errors == []


def test_an_unresolvable_set_is_recorded_and_skipped():
    cards = [
        CatalogCard(
            card_id="zz-1", name="X", set_id="zz", set_name="Nowhere", number="1"
        ),
    ]
    rows, errors = cat._sweep_treatments(cards, _Provider())
    assert rows == []
    assert len(errors) == 1 and "no tcgcsv group" in errors[0]


def test_one_exploding_set_does_not_cost_the_others():
    cards = [
        CatalogCard(
            card_id="base1-4",
            name="Charizard",
            set_id="base1",
            set_name="Base Set",
            number="4",
        ),
        CatalogCard(
            card_id="bad-1", name="Y", set_id="bad", set_name="Bad Set", number="1"
        ),
    ]
    rows, errors = cat._sweep_treatments(cards, _Provider(raise_on={"Bad Set"}))
    assert ("base1-4", "Normal") in rows
    assert len(errors) == 1 and "Bad Set" in errors[0]


# ---------------------------------------------------------------------------
# through the pull
# ---------------------------------------------------------------------------


def _pull_api(url: str):
    if url.endswith("/sets"):
        return [{"id": "base1", "name": "Base Set"}]
    if url.endswith("/sets/base1"):
        return {
            "id": "base1",
            "name": "Base Set",
            "cardCount": {"official": 102},
            "cards": [{"id": "base1-4", "localId": "4", "name": "Charizard"}],
        }
    if url.endswith(("/illustrators", "/rarities", "/categories")):
        return []
    if "/cards?" in url:
        return []
    raise AssertionError(f"unexpected url {url}")


def test_a_pull_records_treatments_alongside_the_cards(conn):
    summary = cat.update_catalog(conn, fetch_json=_pull_api, price_provider=_Provider())
    assert summary.cards == 1
    assert summary.treatments == 1
    assert CardTreatmentRepository(conn).treatments_for("base1-4") == [
        "Holofoil",
        "Normal",
    ]


def test_a_dead_treatment_sweep_still_leaves_a_complete_catalog(conn):
    """The negative test: treatments are enrichment layered onto a catalog that
    is already correct without them, so losing the sweep must cost treatments
    only — not cards, and not the pull's success."""

    class _Broken:
        def resolve_group(self, *a):
            raise RuntimeError("tcgcsv unreachable")

        def products(self, *a):
            raise RuntimeError("tcgcsv unreachable")

        def treatments_by_product(self, *a):
            raise RuntimeError("tcgcsv unreachable")

    summary = cat.update_catalog(conn, fetch_json=_pull_api, price_provider=_Broken())
    assert summary.cards == 1
    assert summary.treatments == 0
    assert summary.errors  # it says so rather than failing silently
    assert CardCatalogRepository(conn).get("base1-4").name == "Charizard"
    assert CardTreatmentRepository(conn).count() == 0


def test_a_repull_does_not_strand_treatments_on_deleted_cards(conn):
    """replace_for_language deletes every card row; treatments must be rewritten
    in the same transaction or they point at card_ids that no longer exist."""
    cat.update_catalog(conn, fetch_json=_pull_api, price_provider=_Provider())
    cat.update_catalog(conn, fetch_json=_pull_api, price_provider=_Provider())
    orphans = conn.execute(
        "SELECT COUNT(*) FROM card_catalog_treatment t "
        "LEFT JOIN card_catalog c ON c.card_id = t.card_id WHERE c.card_id IS NULL"
    ).fetchone()[0]
    assert orphans == 0
    assert CardTreatmentRepository(conn).count() == 2
