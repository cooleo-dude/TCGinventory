"""The English attribute sweep: artist, rarity and supertype (ticket 54).

All offline — every test injects a ``fetch_json`` double, so nothing here touches
TCGdex. The numbers quoted in the docstrings come from a measured dry run against
the live API and are recorded in the ticket.
"""

from __future__ import annotations

import sqlite3

import pytest

from tcginventory.data.db import connect, now_iso
from tcginventory.data.migrator import migrate
from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.domain.models import CatalogCard
from tcginventory.services import catalog as cat


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# the substring-disambiguation rule
# ---------------------------------------------------------------------------


def test_longest_claimant_wins_over_a_substring_false_positive():
    """The rule the sweep exists for.

    ``?illustrator=Uta`` is substring-matched, so it also returns cards drawn by
    *Saya Tsuruta*. Both names therefore claim the card, and only the longer one
    is right. Reverting to "whichever arrived last" makes this go red.
    """
    resolved = cat._resolve_longest({"sv09-001": {"Uta", "Saya Tsuruta"}})
    assert resolved == {"sv09-001": "Saya Tsuruta"}


def test_a_genuine_short_name_is_kept_when_it_is_the_only_claimant():
    """The other half of the rule: 'Uta' really does illustrate some cards.

    A card whose true artist is the short name is claimed by that name alone —
    no longer name can be a substring of it — so longest-wins returns it
    unchanged. Guards against "prefer the longest name in the whole index".
    """
    resolved = cat._resolve_longest({"a-1": {"Uta"}, "b-2": {"Uta", "Utako Yamada"}})
    assert resolved == {"a-1": "Uta", "b-2": "Utako Yamada"}


def test_equal_length_claims_resolve_to_the_alphabetically_first():
    """Determinism: set iteration order is not stable across runs, so a tie has
    to be broken by something. Two orderings of the same claims must agree."""
    claims = {"c-1": {"Bbb", "Aaa"}}
    assert cat._resolve_longest(claims) == cat._resolve_longest({"c-1": {"Aaa", "Bbb"}})
    assert cat._resolve_longest(claims) == {"c-1": "Aaa"}


def test_a_card_with_no_claimants_is_omitted_rather_than_given_an_empty_artist():
    assert cat._resolve_longest({"d-1": set()}) == {}


# ---------------------------------------------------------------------------
# planning and sweeping
# ---------------------------------------------------------------------------


def _fake_api(
    *,
    illustrators=("Uta", "Saya Tsuruta"),
    rarities=("Common",),
    categories=("Pokemon",),
    cards_by_query=None,
    fail=(),
):
    """A ``fetch_json`` double over the TCGdex shapes the sweep uses.

    ``fail`` is a tuple of URL substrings that raise instead of returning.
    """
    cards_by_query = cards_by_query or {}

    def fetch(url: str):
        for needle in fail:
            if needle in url:
                raise RuntimeError(f"boom: {needle}")
        if url.endswith("/illustrators"):
            return list(illustrators)
        if url.endswith("/rarities"):
            return list(rarities)
        if url.endswith("/categories"):
            return list(categories)
        if "/cards?" in url:
            return cards_by_query.get(url.split("/cards?", 1)[1], [])
        raise AssertionError(f"unexpected url {url}")

    return fetch


def test_the_plan_is_one_entry_per_index_value_across_all_three_axes():
    plan, errors = cat._attribute_plan(
        _fake_api(illustrators=("A", "B"), rarities=("Common", "Rare"))
    )
    assert errors == []
    assert plan == [
        ("artist", "illustrator", "A"),
        ("artist", "illustrator", "B"),
        ("rarity", "rarity", "Common"),
        ("rarity", "rarity", "Rare"),
        ("supertype", "category", "Pokemon"),
    ]


def test_one_dead_index_drops_only_its_own_axis():
    """A missing rarity costs less than a lost catalog, so the other axes run."""
    plan, errors = cat._attribute_plan(_fake_api(fail=("/rarities",)))
    assert [a for a, _, _ in plan] == ["artist", "artist", "supertype"]
    assert len(errors) == 1 and "rarity index" in errors[0]


def test_blank_and_non_string_index_entries_are_skipped():
    plan, _ = cat._attribute_plan(
        _fake_api(illustrators=("Real", "", "   ", None, 7), rarities=(), categories=())
    )
    assert plan == [("artist", "illustrator", "Real")]


def test_the_sweep_inverts_the_index_and_disambiguates_end_to_end():
    """The whole mechanism on the real-world shape: one card claimed by two
    illustrator names, resolved to the longer."""
    fetch = _fake_api(
        illustrators=("Uta", "Saya Tsuruta"),
        rarities=("Common",),
        categories=("Pokemon",),
        cards_by_query={
            "illustrator=Uta": [{"id": "sv09-001"}, {"id": "sv09-002"}],
            "illustrator=Saya%20Tsuruta": [{"id": "sv09-001"}],
            "rarity=Common": [{"id": "sv09-001"}],
            "category=Pokemon": [{"id": "sv09-001"}],
        },
    )
    plan, _ = cat._attribute_plan(fetch)
    maps, errors = cat._sweep_attributes(fetch, plan)
    assert errors == []
    assert maps["artist"] == {"sv09-001": "Saya Tsuruta", "sv09-002": "Uta"}
    assert maps["rarity"] == {"sv09-001": "Common"}
    assert maps["supertype"] == {"sv09-001": "Pokemon"}


def test_one_failing_query_does_not_abandon_the_rest_of_the_sweep():
    fetch = _fake_api(
        illustrators=("Uta", "Saya Tsuruta"),
        rarities=(),
        categories=(),
        cards_by_query={"illustrator=Saya%20Tsuruta": [{"id": "x-1"}]},
        fail=("illustrator=Uta",),
    )
    plan, _ = cat._attribute_plan(fetch)
    maps, errors = cat._sweep_attributes(fetch, plan)
    assert maps["artist"] == {"x-1": "Saya Tsuruta"}
    assert len(errors) == 1 and "artist=Uta" in errors[0]


def test_the_sweep_reports_progress_once_per_planned_step():
    fetch = _fake_api(illustrators=("A", "B"), rarities=(), categories=())
    plan, _ = cat._attribute_plan(fetch)
    seen: list[int] = []
    cat._sweep_attributes(fetch, plan, on_step=seen.append)
    assert seen == [1, 2]


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def test_migration_0023_adds_a_nullable_artist_column_and_its_index(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(card_catalog)")}
    assert "artist" in cols
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='card_catalog'"
        )
    }
    assert "idx_card_catalog_artist" in names


def test_existing_rows_read_null_rather_than_failing(conn):
    """The column is un-backfilled by design: a database that has not been
    re-pulled since 0023 must keep working, with artist simply absent."""
    repo = CardCatalogRepository(conn)
    repo.replace_for_language(
        [CatalogCard(card_id="base1-4", name="Charizard", set_id="base1")],
        "EN",
        now_iso(),
    )
    assert repo.get("base1-4").artist is None


def test_artist_round_trips_through_insert_and_read(conn):
    repo = CardCatalogRepository(conn)
    repo.replace_for_language(
        [
            CatalogCard(
                card_id="base1-4",
                name="Charizard",
                set_id="base1",
                artist="Mitsuhiro Arita",
            )
        ],
        "EN",
        now_iso(),
    )
    assert repo.get("base1-4").artist == "Mitsuhiro Arita"


def test_distinct_artists_sorts_excludes_null_and_scopes_by_language(conn):
    repo = CardCatalogRepository(conn)
    repo.replace_for_language(
        [
            CatalogCard(card_id="e-2", name="B", set_id="s", artist="Uta"),
            CatalogCard(card_id="e-1", name="A", set_id="s", artist="Mitsuhiro Arita"),
            CatalogCard(card_id="e-3", name="C", set_id="s", artist=None),
            CatalogCard(card_id="e-4", name="D", set_id="s", artist=""),
        ],
        "EN",
        now_iso(),
    )
    repo.replace_for_language(
        [
            CatalogCard(
                card_id="jp-1", name="J", set_id="s", language="JP", artist="Someone"
            )
        ],
        "JP",
        now_iso(),
    )
    assert repo.distinct_artists("EN") == ["Mitsuhiro Arita", "Uta"]
    assert repo.distinct_artists("JP") == ["Someone"]


# ---------------------------------------------------------------------------
# the pull as a whole
# ---------------------------------------------------------------------------


def _pull_api(*, fail=()):
    """A minimal but complete ``update_catalog`` double: one set, two cards."""
    detail = {
        "id": "sv09",
        "name": "Journey Together",
        "cardCount": {"official": 2},
        "cards": [
            {"id": "sv09-001", "localId": "001", "name": "Caterpie", "image": "u/1"},
            {"id": "sv09-002", "localId": "002", "name": "Metapod", "image": "u/2"},
        ],
    }
    inner = _fake_api(
        illustrators=("Uta", "Saya Tsuruta"),
        rarities=("Common",),
        categories=("Pokemon",),
        cards_by_query={
            "illustrator=Uta": [{"id": "sv09-001"}, {"id": "sv09-002"}],
            "illustrator=Saya%20Tsuruta": [{"id": "sv09-001"}],
            "rarity=Common": [{"id": "sv09-001"}, {"id": "sv09-002"}],
            "category=Pokemon": [{"id": "sv09-001"}, {"id": "sv09-002"}],
        },
        fail=fail,
    )

    def fetch(url: str):
        if url.endswith("/sets"):
            return [{"id": "sv09", "name": "Journey Together"}]
        if url.endswith("/sets/sv09"):
            return detail
        return inner(url)

    return fetch


def test_a_pull_populates_artist_rarity_and_supertype(conn):
    summary = cat.update_catalog(conn, fetch_json=_pull_api())
    assert summary.cards == 2
    assert summary.artists == 2
    repo = CardCatalogRepository(conn)
    assert repo.get("sv09-001").artist == "Saya Tsuruta"
    assert repo.get("sv09-002").artist == "Uta"
    # The two columns the English pull used to leave NULL for every row.
    assert repo.get("sv09-001").rarity == "Common"
    assert repo.get("sv09-001").supertype == "Pokemon"


def test_a_dead_attribute_sweep_still_leaves_a_complete_catalog(conn):
    """The negative test: enrichment is layered onto a catalog that is already
    correct without it, so losing the whole sweep must cost artists only — not
    cards, and not the pull's success."""
    summary = cat.update_catalog(
        conn,
        fetch_json=_pull_api(
            fail=("/illustrators", "/rarities", "/categories", "/cards?")
        ),
    )
    assert summary.cards == 2
    assert summary.artists == 0
    assert summary.errors  # it says so rather than failing silently
    repo = CardCatalogRepository(conn)
    assert repo.count() == 2
    assert repo.get("sv09-001").name == "Caterpie"
    assert repo.get("sv09-001").artist is None
    assert repo.distinct_artists("EN") == []


def test_progress_spans_both_phases_and_ends_at_the_total(conn):
    """The bar must not reach 100% and then sit through the sweep."""
    seen: list[tuple[int, int]] = []
    cat.update_catalog(
        conn, fetch_json=_pull_api(), on_progress=lambda d, t: seen.append((d, t))
    )
    totals = {t for _, t in seen}
    assert len(totals) == 1, "one span, not two"
    total = totals.pop()
    assert total == 1 + 4, "one set + four planned index values"
    assert [d for d, _ in seen] == [1, 2, 3, 4, 5]
