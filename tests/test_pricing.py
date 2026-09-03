"""Tests for tcgcsv pricing provider + service, using a fake fetcher (offline)."""

from __future__ import annotations

from tcginventory.data.repositories import ItemRepository, PriceSnapshotRepository
from tcginventory.domain.enums import Category, Language
from tcginventory.domain.models import Item
from tcginventory.services.pricing import (
    PokemonTcgProvider,
    PricingService,
    TcgcsvProvider,
)

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
    # This Pikachu declares no holo_pattern, so nothing says which printing it
    # is. Ticket 56 takes the CHEAPEST of the product's treatments (425c normal
    # over 999c holofoil), not the dearest: overstating a portfolio inflates
    # unrealized gain and collection value. This assertion was `== 999` before
    # that ticket, which was the bug written down as a test.
    assert quotes[0].price_cents == 425
    assert quotes[0].treatment == "Normal"
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
    assert history[0].price_cents == 425  # cheapest treatment; see ticket 56
    assert history[0].source == "tcgcsv"
    # Current market value on the item is updated for the dashboard/columns.
    assert items.get(item.id).market_value_cents == 425


def _base_fetch(url: str):
    # Two "…Base Set" groups whose only reliable difference is the code.
    if url.endswith("/groups"):
        return {
            "results": [
                {"groupId": 604, "name": "Base Set", "abbreviation": "BS"},
                {
                    "groupId": 22873,
                    "name": "SV01: Scarlet & Violet Base Set",
                    "abbreviation": "SVI",
                },
            ]
        }
    if url.endswith("/604/products"):
        return {
            "results": [
                {
                    "productId": 42347,
                    "name": "Mewtwo",
                    "url": "base-mewtwo",
                    "extendedData": [{"name": "Number", "value": "10/102"}],
                }
            ]
        }
    if url.endswith("/604/prices"):
        return {"results": [{"productId": 42347, "marketPrice": 77.73}]}
    if url.endswith("/22873/products"):
        return {
            "results": [
                {
                    "productId": 487842,
                    "name": "Vivillon",
                    "url": "sv-viv",
                    "extendedData": [{"name": "Number", "value": "010/198"}],
                }
            ]
        }
    if url.endswith("/22873/prices"):
        return {"results": [{"productId": 487842, "marketPrice": 5.0}]}
    raise AssertionError(f"unexpected url: {url}")


def test_set_code_disambiguates_generic_set_name():
    # "Base" alone fuzzy-matches "…Scarlet & Violet Base Set"; the "BS" code nails
    # the original WOTC Base Set (regression: Mewtwo Base 10/102 → wrong URL).
    p = TcgcsvProvider(fetch_json=_base_fetch)
    assert p.price_for_card("Base", "10", "Mewtwo", set_code="BS") == (
        7773,
        "base-mewtwo",
    )


def test_name_check_rejects_wrong_group_match():
    # If resolution lands on the wrong group, number 10 there is Vivillon, not
    # Mewtwo — the name guard returns None rather than a blatantly wrong link.
    p = TcgcsvProvider(fetch_json=_base_fetch)
    assert p.price_for_card("SV01: Scarlet & Violet Base Set", "10", "Mewtwo") is None


def _promo_fetch(url: str):
    # "PR" is shared by many groups; "Deck Exclusives" is indexed first.
    if url.endswith("/groups"):
        return {
            "results": [
                {"groupId": 1840, "name": "Deck Exclusives", "abbreviation": "PR"},
                {"groupId": 1418, "name": "WoTC Promo", "abbreviation": "PR"},
            ]
        }
    if url.endswith("/1418/products"):
        return {
            "results": [
                {
                    "productId": 87309,
                    "name": "Meowth",
                    "url": "wotc-meowth",
                    "extendedData": [{"name": "Number", "value": "10"}],
                }
            ]
        }
    if url.endswith("/1418/prices"):
        return {"results": [{"productId": 87309, "marketPrice": 62.31}]}
    if url.endswith("/1840/products"):
        return {
            "results": [
                {
                    "productId": 999,
                    "name": "Something Else",
                    "url": "deck",
                    "extendedData": [{"name": "Number", "value": "10"}],
                }
            ]
        }
    if url.endswith("/1840/prices"):
        return {"results": [{"productId": 999, "marketPrice": 1.0}]}
    raise AssertionError(f"unexpected url: {url}")


def test_promo_alias_beats_generic_set_code():
    # The generic "PR" code alone would hit "Deck Exclusives" (indexed first);
    # the curated promo alias resolves "Wizards Black Star Promos" → WoTC Promo.
    p = TcgcsvProvider(fetch_json=_promo_fetch)
    assert p.price_for_card(
        "Wizards Black Star Promos", "10", "Meowth", set_code="PR"
    ) == (6231, "wotc-meowth")


_RENAMED_PROMO_GROUPS = {
    22872: ("SV: Scarlet & Violet Promo Cards", "Eevee", "173", "sv-eevee", 4.50),
    24451: ("ME: Mega Evolution Promo", "Rowlet", "43", "me-rowlet", 2.00),
    2545: ("SWSH: Sword & Shield Promo Cards", "Pikachu", "1", "swsh-pika", 9.99),
}


def _renamed_promo_fetch(url: str):
    if url.endswith("/groups"):
        return {
            "results": [
                {"groupId": gid, "name": name, "abbreviation": "PR"}
                for gid, (name, *_rest) in _RENAMED_PROMO_GROUPS.items()
            ]
        }
    for gid, (_name, cname, num, curl, price) in _RENAMED_PROMO_GROUPS.items():
        pid = 500 + gid % 100
        if url.endswith(f"/{gid}/products"):
            return {
                "results": [
                    {
                        "productId": pid,
                        "name": cname,
                        "url": curl,
                        "extendedData": [{"name": "Number", "value": num}],
                    }
                ]
            }
        if url.endswith(f"/{gid}/prices"):
            return {"results": [{"productId": pid, "marketPrice": price}]}
    raise AssertionError(f"unexpected url: {url}")


def test_tcgdex_promo_names_resolve_after_tcgcsv_renames():
    # tcgcsv prefixed the S&V / SWSH promo groups and added a Mega-Evolution one;
    # the TCGdex catalog names must still resolve to them (svp/mep were broken).
    p = TcgcsvProvider(fetch_json=_renamed_promo_fetch)
    svp = p.price_for_card("SVP Black Star Promos", "173", "Eevee")
    mep = p.price_for_card("MEP Black Star Promos", "43", "Rowlet")
    swsh = p.price_for_card("SWSH Black Star Promos", "1", "Pikachu")
    assert svp == (450, "sv-eevee")
    assert mep == (200, "me-rowlet")
    assert swsh == (999, "swsh-pika")
    # Back-compat: an item still stored under the old pokemontcg.io S&V name.
    old = p.price_for_card("Scarlet & Violet Black Star Promos", "173", "Eevee")
    assert old == (450, "sv-eevee")


def _subset_fetch(url: str):
    # A subset set whose code collides with its parent (both "HIF"), plus a set
    # (Unified Minds) that lists only the base "79", not the "79a" alt-art.
    if url.endswith("/groups"):
        return {
            "results": [
                {"groupId": 2480, "name": "Hidden Fates", "abbreviation": "HIF"},
                {
                    "groupId": 2594,
                    "name": "Hidden Fates: Shiny Vault",
                    "abbreviation": "HIF",
                },
                {"groupId": 2464, "name": "SM - Unified Minds", "abbreviation": "SM11"},
            ]
        }
    if url.endswith("/2594/products"):
        return {
            "results": [
                {
                    "productId": 197833,
                    "name": "Silvally GX",
                    "url": "sv79",
                    "extendedData": [{"name": "Number", "value": "SV79/SV94"}],
                }
            ]
        }
    if url.endswith("/2594/prices"):
        return {"results": [{"productId": 197833, "marketPrice": 13.47}]}
    if url.endswith("/2480/products"):  # main set has no SV79
        return {
            "results": [
                {
                    "productId": 1,
                    "name": "Gyarados GX",
                    "url": "hf",
                    "extendedData": [{"name": "Number", "value": "7/68"}],
                }
            ]
        }
    if url.endswith("/2480/prices"):
        return {"results": [{"productId": 1, "marketPrice": 5.0}]}
    if url.endswith("/2464/products"):
        return {
            "results": [
                {
                    "productId": 195009,
                    "name": "Jirachi GX",
                    "url": "jira",
                    "extendedData": [{"name": "Number", "value": "79/236"}],
                }
            ]
        }
    if url.endswith("/2464/prices"):
        return {"results": [{"productId": 195009, "marketPrice": 13.93}]}
    raise AssertionError(f"unexpected url: {url}")


def test_subset_resolves_by_name_over_colliding_code():
    # "Hidden Fates Shiny Vault" (code "HIF") must resolve to the Shiny Vault
    # group, not the main Hidden Fates set that shares the code.
    p = TcgcsvProvider(fetch_json=_subset_fetch)
    assert p.price_for_card(
        "Hidden Fates Shiny Vault", "SV79", "Silvally-GX", set_code="HIF"
    ) == (1347, "sv79")


def test_alt_art_suffix_falls_back_to_base_number():
    # tcgcsv lists only "79"; the "79a" alt-art reprint falls back to it.
    p = TcgcsvProvider(fetch_json=_subset_fetch)
    assert p.price_for_card("Unified Minds", "79a", "Jirachi-GX", set_code="UNM") == (
        1393,
        "jira",
    )


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


def _poketcg_card():
    return {
        "name": "Iono's Kilowattrel",
        "set": {"name": "Journey Together", "id": "sv9"},
        "number": "163",
        "tcgplayer": {
            "prices": {
                "reverseHolofoil": {"market": 0.40, "low": 0.09},
                "holofoil": {"market": 5.51, "low": 4.0},
            }
        },
    }


def test_pokemontcg_provider_quotes_via_injected_fetch(conn):
    item = ItemRepository(conn).add(
        Item(
            name="Kilowattrel",
            category=Category.SINGLE,
            set_name="Journey Together",
            card_number="163/159",
        )
    )

    def fake(url: str):
        assert "cards?q=" in url and "163" in url  # set-name + number query
        return {"data": [_poketcg_card()]}

    quotes = PokemonTcgProvider(fetch_json=fake).quotes_for([item])
    assert len(quotes) == 1
    # Prefers the holofoil printing (551c) over reverseHolofoil (40c).
    assert quotes[0].price_cents == 551
    assert quotes[0].source == "pokemontcg.io"


def test_pricing_service_accepts_pokemontcg_override(conn):
    """The alternate provider drops into PricingService unchanged (tcgcsv stays
    the default) and records snapshots tagged with its own source."""
    item = ItemRepository(conn).add(
        Item(
            name="Kilowattrel",
            category=Category.SINGLE,
            set_name="Journey Together",
            card_number="163/159",
        )
    )
    provider = PokemonTcgProvider(fetch_json=lambda url: {"data": [_poketcg_card()]})
    summary = PricingService(conn, provider=provider).refresh(
        as_of="2026-07-13T00:00:00+00:00"
    )
    assert summary.priced == 1
    snap = PriceSnapshotRepository(conn).history_for_item(item.id)[-1]
    assert snap.source == "pokemontcg.io"
    assert snap.price_cents == 551


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


def test_price_for_card_resolves_by_set_and_number():
    def fetch(url: str):
        if url.endswith("/groups"):
            return {"results": [{"groupId": 24073, "name": "SV09: Journey Together"}]}
        if url.endswith("/24073/products"):
            return {
                "results": [
                    {
                        "productId": 623590,
                        "url": "https://www.tcgplayer.com/product/623590/x",
                        "extendedData": [{"name": "Number", "value": "163/159"}],
                    }
                ]
            }
        if url.endswith("/24073/prices"):
            return {"results": [{"productId": 623590, "marketPrice": 6.10}]}
        raise AssertionError(f"unexpected url: {url}")

    provider = TcgcsvProvider(fetch_json=fetch)
    # Catalog stores the bare number '163'; tcgcsv products use '163/159'.
    result = provider.price_for_card("Journey Together", "163", "Iono's Kilowattrel")
    assert result == (610, "https://www.tcgplayer.com/product/623590/x")
    assert provider.price_for_card("Journey Together", "999") is None


def test_price_for_card_bridges_promo_set_naming():
    """pokemontcg.io's "SWSH Black Star Promos" must resolve to tcgcsv's
    "SWSH: Sword & Shield Promo Cards" group (systematic promo aliasing)."""

    def fetch(url: str):
        if url.endswith("/groups"):
            return {
                "results": [
                    {
                        "groupId": 2545,
                        "name": "SWSH: Sword & Shield Promo Cards",
                        "abbreviation": "SWSD",
                    },
                ]
            }
        if url.endswith("/2545/products"):
            return {
                "results": [
                    {
                        "productId": 285396,
                        "url": "https://www.tcgplayer.com/product/285396/x",
                        "extendedData": [{"name": "Number", "value": "SWSH262"}],
                    }
                ]
            }
        if url.endswith("/2545/prices"):
            return {"results": [{"productId": 285396, "marketPrice": 81.65}]}
        raise AssertionError(f"unexpected url: {url}")

    provider = TcgcsvProvider(fetch_json=fetch)
    result = provider.price_for_card(
        "SWSH Black Star Promos", "SWSH262", "Charizard VSTAR"
    )
    assert result == (8165, "https://www.tcgplayer.com/product/285396/x")


def _fake_jp_fetch(url: str):
    """tcgcsv category 85 ("Pokemon Japan") — a group with one card."""
    if url.endswith("/85/groups"):
        return {
            "results": [
                {"groupId": 24711, "name": "M5: Abyss Eye", "abbreviation": "M5"}
            ]
        }
    if url.endswith("/85/24711/products"):
        return {
            "results": [
                {
                    "productId": 694993,
                    "url": "https://www.tcgplayer.com/product/694993/x",
                    "extendedData": [{"name": "Number", "value": "103/081"}],
                }
            ]
        }
    if url.endswith("/85/24711/prices"):
        return {"results": [{"productId": 694993, "marketPrice": 0.91}]}
    raise AssertionError(f"unexpected url: {url}")


def test_price_for_card_uses_japanese_category():
    provider = TcgcsvProvider(fetch_json=_fake_jp_fetch)
    # category=85 routes to the Japanese groups/products/prices files.
    result = provider.price_for_card("M5: Abyss Eye", "103/081", category=85)
    assert result == (91, "https://www.tcgplayer.com/product/694993/x")


def test_quotes_for_routes_japanese_item_to_category_85(conn):
    item = ItemRepository(conn).add(
        Item(
            name="Energy Switch",
            category=Category.SINGLE,
            set_name="M5: Abyss Eye",
            card_number="103/081",
            language=Language.JP,
            tcgplayer_url="https://www.tcgplayer.com/product/694993/x",
        )
    )
    # A JP-language item is priced against category 85 (fetch asserts the URL).
    quotes = TcgcsvProvider(fetch_json=_fake_jp_fetch).quotes_for([item])
    assert len(quotes) == 1
    assert quotes[0].item_id == item.id and quotes[0].price_cents == 91


def test_refresh_skips_graded_cards(conn):
    """Graded slabs must not be priced from tcgcsv (that's the raw single's
    price) — no snapshot, no value change."""
    from tcginventory.domain.enums import GradingCompany

    items = ItemRepository(conn)
    graded = items.add(
        _pikachu(
            is_graded=True,
            condition=None,
            grading_company=GradingCompany.PSA,
            grade="10",
        )
    )
    svc = PricingService(conn, provider=TcgcsvProvider(fetch_json=_fake_fetch))
    summary = svc.refresh()
    assert summary.priced == 0
    assert graded.name not in summary.unmapped  # skipped, not "unmapped"
    assert PriceSnapshotRepository(conn).history_for_item(graded.id) == []


def test_latest_as_of_reports_most_recent_refresh(conn):
    snaps = PriceSnapshotRepository(conn)
    assert snaps.latest_as_of() is None  # never refreshed
    item = ItemRepository(conn).add(_pikachu())
    svc = PricingService(conn, provider=TcgcsvProvider(fetch_json=_fake_fetch))
    svc.refresh(as_of="2026-05-01T00:00:00+00:00")
    svc.refresh(as_of="2026-07-01T12:34:56+00:00")
    assert snaps.latest_as_of() == "2026-07-01T12:34:56+00:00"
    assert item.id  # sanity


def test_latest_prices_and_history_since(conn):
    item = ItemRepository(conn).add(_pikachu())
    snaps = PriceSnapshotRepository(conn)
    svc = PricingService(conn, provider=TcgcsvProvider(fetch_json=_fake_fetch))
    svc.refresh(as_of="2026-05-01T00:00:00+00:00")
    svc.refresh(as_of="2026-07-01T00:00:00+00:00")

    assert snaps.latest_prices()[item.id] == 425  # cheapest treatment (ticket 56)
    recent = snaps.history_for_item(item.id, since="2026-06-01")
    assert len(recent) == 1  # only the July snapshot is within range


def _variant_fetch(url: str):
    if url.endswith("/groups"):
        return {
            "results": [
                {
                    "groupId": 22872,
                    "name": "SV: Scarlet & Violet Promo Cards",
                    "abbreviation": "SVP",
                }
            ]
        }
    if url.endswith("/22872/products"):
        return {
            "results": [
                {
                    "productId": 610757,
                    "name": "Eevee - 173 (Pokemon Center Exclusive)",
                    "url": "eevee-excl",
                    "extendedData": [{"name": "Number", "value": "173"}],
                },
                {
                    "productId": 610758,
                    "name": "Eevee - 173",
                    "url": "eevee-base",
                    "extendedData": [{"name": "Number", "value": "173"}],
                },
            ]
        }
    if url.endswith("/22872/prices"):
        return {
            "results": [
                {"productId": 610757, "marketPrice": 94.13},
                {"productId": 610758, "marketPrice": 13.70},
            ]
        }
    raise AssertionError(f"unexpected url: {url}")


def test_price_defaults_to_base_print_over_stamped_variant():
    # tcgcsv lists "(Pokemon Center Exclusive)" first; the plain base print — the
    # product name closest to the card name — must win by default.
    p = TcgcsvProvider(fetch_json=_variant_fetch)
    assert p.price_for_card("SVP Black Star Promos", "173", "Eevee") == (
        1370,
        "eevee-base",
    )


def test_variants_for_card_lists_base_first():
    p = TcgcsvProvider(fetch_json=_variant_fetch)
    variants = p.variants_for_card("SVP Black Star Promos", "173", "Eevee")
    assert [(v.label, v.price_cents, v.is_base) for v in variants] == [
        ("Base", 1370, True),
        ("Pokemon Center Exclusive", 9413, False),
    ]
    assert variants[0].tcgplayer_url == "eevee-base"


# ---------------------------------------------------------------------------
# Group resolution (ticket 67)
# ---------------------------------------------------------------------------

# The real collision, trimmed to what reproduces it: the 151 set's own group,
# the McDonald's group whose *abbreviation* is "M15", and the SM-era groups
# whose abbreviations "SM9"/"SM01" a 3-character "SUM" scores 0.667 against.
_GROUPS_67 = {
    "results": [
        {"groupId": 1694, "name": "McDonald's Promos 2015", "abbreviation": "M15"},
        {"groupId": 23237, "name": "SV: Scarlet & Violet 151", "abbreviation": "MEW"},
        {
            "groupId": 22873,
            "name": "SV01: Scarlet & Violet Base Set",
            "abbreviation": "SVI",
        },
        {"groupId": 2377, "name": "SM - Team Up", "abbreviation": "SM9"},
        {"groupId": 1863, "name": "SM Base Set", "abbreviation": "SM01"},
    ]
}


def _groups_only_fetch(url: str):
    if url.endswith("/groups"):
        return _GROUPS_67
    raise AssertionError(f"unexpected url: {url}")


def test_a_three_character_key_does_not_fuzzy_match_an_abbreviation():
    """The negative case the fuzzy floor exists for.

    "151" shares two characters with "M15" and scored 0.667 — over the 0.6
    threshold — so the 151 set was priced against McDonald's Promos 2015. The
    same happened to "SUM" against "SM9". Neither may resolve on fuzz alone.
    """
    p = TcgcsvProvider(fetch_json=_groups_only_fetch)

    assert p.resolve_group("151", None, 3) is None
    assert p.resolve_group(None, "SUM", 3) is None
    # A long enough name still fuzzy-matches, so the fallback is not disabled.
    assert p.resolve_group("Scarlet & Violet Base", None, 3) == 22873


def test_group_resolves_exactly_once_the_set_code_is_known():
    """The codes the catalog holds resolve all three sets exactly."""
    p = TcgcsvProvider(fetch_json=_groups_only_fetch)

    assert p.resolve_group("151", "MEW", 3) == 23237
    assert p.resolve_group("Scarlet & Violet", "SVI", 3) == 22873
    # "Sun & Moon"/"SUM" matches no group name or code; the alias bridges it.
    assert p.resolve_group("Sun & Moon", "SUM", 3) == 1863


def test_item_borrows_the_catalog_set_code_for_its_group(conn):
    """Ledger items carry no set_code, so the provider looks one up.

    Without it, "151" reached the fuzzy fallback and priced against McDonald's.
    """
    from tcginventory.data.repositories import CardCatalogRepository
    from tcginventory.domain.models import CatalogCard
    from tcginventory.services.pricing import set_codes_by_name

    item = _pikachu(set_name="151", set_code=None, card_number="199/165")
    assert item.set_code is None  # true of every item in the real ledger

    bare = TcgcsvProvider(fetch_json=_groups_only_fetch)
    assert bare._group_id(item) is None  # nothing left to resolve on

    coded = TcgcsvProvider(fetch_json=_groups_only_fetch, set_codes={"151": "MEW"})
    assert coded._group_id(item) == 23237

    # And the map the service builds carries the code the catalog holds.
    CardCatalogRepository(conn).replace_all(
        [
            CatalogCard(
                card_id="c1",
                name="Charizard ex",
                set_id="sv03.5",
                set_name="151",
                set_code="MEW",
                number="199",
            )
        ],
        "t",
    )
    assert set_codes_by_name(conn)["151"] == "MEW"
