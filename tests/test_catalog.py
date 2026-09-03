"""Reference card catalog: pull/update from the dataset + autocomplete queries."""

from __future__ import annotations

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.services.catalog import update_catalog, update_catalog_ja

# tcgcsv category 85 ("Pokemon Japan") shape.
_JA_GROUPS = {
    "results": [{"groupId": 24711, "name": "M5: Abyss Eye", "abbreviation": "M5"}]
}
_JA_PRODUCTS = {
    "results": [
        {
            "productId": 694993,
            "name": "Energy Switch",
            "imageUrl": "https://tcgplayer-cdn.tcgplayer.com/product/694993_200w.jpg",
            "extendedData": [
                {"name": "Number", "value": "103/081"},
                {"name": "Rarity", "value": "Super Rare"},
            ],
        },
        # tcgcsv embeds the number to disambiguate same-named cards; the pull
        # must strip that "- <number>" segment (suffix form)…
        {
            "productId": 694994,
            "name": "Mega Darkrai ex - 046/081",
            "imageUrl": "https://x/694994.jpg",
            "extendedData": [{"name": "Number", "value": "046/081"}],
        },
        # …and mid-name form, keeping the trailing variant label.
        {
            "productId": 694995,
            "name": "Landorus - 398/742 (Mirror Holo)",
            "imageUrl": "https://x/694995.jpg",
            "extendedData": [{"name": "Number", "value": "398/742"}],
        },
        # A sealed product (no collector Number) must be skipped.
        {
            "productId": 999,
            "name": "Abyss Eye Booster Box",
            "imageUrl": "https://x/999.jpg",
            "extendedData": [],
        },
    ]
}


def _fake_ja_fetch(url: str):
    if url.endswith("/85/groups"):
        return _JA_GROUPS
    if url.endswith("/85/24711/products"):
        return _JA_PRODUCTS
    raise AssertionError(f"unexpected url: {url}")


# A vintage JP set tcgcsv doesn't give collector Numbers (e.g. "Challenge from
# the Darkness", 1998): its cards carry per-card mechanic fields instead.
def _fake_ja_vintage_fetch(url: str):
    if url.endswith("/85/groups"):
        return {"results": [{"groupId": 23726, "name": "Challenge from the Darkness"}]}
    if url.endswith("/85/23726/products"):
        return {
            "results": [
                {
                    "productId": 575335,
                    "name": "Giovanni's Machamp",
                    "imageUrl": "https://x/gm.jpg",
                    "extendedData": [
                        {"name": "CardType", "value": "Fighting"},
                        {"name": "HP", "value": "90"},
                        {"name": "Stage", "value": "Stage 2"},
                    ],
                },
                # Sealed product: no Number and no card fields → skipped.
                {
                    "productId": 888,
                    "name": "Challenge Starter Deck",
                    "imageUrl": "https://x/d.jpg",
                    "extendedData": [],
                },
            ]
        }
    raise AssertionError(f"unexpected url: {url}")


def test_update_catalog_ja_imports_numberless_vintage_cards(conn):
    summary = update_catalog_ja(conn, fetch_json=_fake_ja_vintage_fetch)
    assert summary.cards == 1  # the card kept; the sealed deck skipped
    (gm,) = CardCatalogRepository(conn).find_by_name("Giovanni's Machamp")
    assert gm.set_name == "Challenge from the Darkness"
    assert gm.language == "JP"
    assert gm.number is None  # tcgcsv has no collector number for this set


# TCGdex shape: a brief /sets index + a /sets/{id} detail carrying the full card
# list (id, localId, name, image). Numbers are zero-padded there ("001").
_SETS = [
    {"id": "sv9", "name": "Journey Together"},
    {"id": "cel25", "name": "Celebrations"},
]
_SET_DETAIL = {
    "sv9": {
        "id": "sv9",
        "name": "Journey Together",
        "abbreviation": {"official": "JTG"},
        "cardCount": {"official": 159, "total": 159},
        "cards": [
            {
                "id": "sv9-163",
                "localId": "163",
                "name": "Iono's Kilowattrel",
                "image": "https://assets.tcgdex.net/en/sv/sv9/163",
            },
            {
                "id": "sv9-1",
                "localId": "001",
                "name": "Pikachu",
                "image": "https://assets.tcgdex.net/en/sv/sv9/001",
            },
        ],
    },
    "cel25": {
        "id": "cel25",
        "name": "Celebrations",
        "abbreviation": {"official": "CEL"},
        "cardCount": {"official": 25, "total": 25},
        "cards": [
            {"id": "cel25-5", "localId": "5", "name": "Pikachu", "image": None},
        ],
    },
}


def _fake_fetch(url: str):
    if url.endswith("/sets"):
        return _SETS
    for set_id, detail in _SET_DETAIL.items():
        if url.endswith(f"/sets/{set_id}"):
            return detail
    # The English pull also sweeps TCGdex's attribute indexes for artist, rarity
    # and supertype (ticket 54). These tests are about the set/card half, so the
    # indexes answer empty and the sweep is a no-op — but they must answer, or
    # every one of them collects three "index unreachable" errors. The sweep has
    # its own coverage in tests/test_catalog_artist.py.
    if url.endswith(("/illustrators", "/rarities", "/categories")):
        return []
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
    # The number is TCGdex's localId verbatim — i.e. the number printed on the
    # card. SV-era sets pad it ("001"); Celebrations does not ("5").
    pikachus = repo.find_by_name("Pikachu")
    assert {(c.set_name, c.number) for c in pikachus} == {
        ("Journey Together", "001"),
        ("Celebrations", "5"),
    }
    (kilo,) = repo.find_by_name("Iono's Kilowattrel")
    # set_code comes from TCGdex's abbreviation; rarity isn't in the set payload.
    assert (kilo.set_code, kilo.number, kilo.rarity) == ("JTG", "163", None)
    # image_small is TCGdex's asset base + quality/extension.
    assert kilo.image_small == "https://assets.tcgdex.net/en/sv/sv9/163/low.png"


def test_search_enriches_with_set_and_number(conn):
    update_catalog(conn, fetch_json=_fake_fetch)
    repo = CardCatalogRepository(conn)

    # name + set narrows to the Celebrations Pikachu (#5), not the sv9 one (#1).
    (hit,) = repo.search("Pikachu Celebrations")
    assert (hit.set_name, hit.number) == ("Celebrations", "5")

    # name + number (XX/YY form) narrows by collector number — and finds the row
    # whichever padding convention the query and the row each use.
    for query in ("Pikachu 1/99", "Pikachu 001/099", "Pikachu 001"):
        (hit,) = repo.search(query)
        assert (hit.set_name, hit.number) == ("Journey Together", "001"), query

    # name only returns every printing.
    assert len(repo.search("Pikachu")) == 2
    # partial name still matches (owner prefix).
    assert repo.search("Kilowattrel")[0].name == "Iono's Kilowattrel"

    # Tokens work in ANY order, and the number may be bare (no denominator).
    for q in (
        "Pikachu Celebrations 5",
        "Pikachu 5 Celebrations",
        "5 Celebrations Pikachu",
    ):
        (hit,) = repo.search(q)
        assert (hit.set_name, hit.number) == ("Celebrations", "5"), q


def test_search_matches_lettered_number_prefix(conn):
    from tcginventory.domain.models import CatalogCard

    CardCatalogRepository(conn).replace_all(
        [
            CatalogCard(
                card_id="a",
                name="Falinks",
                set_id="s1",
                set_name="Shining Fates Shiny Vault",
                number="SV074",
            ),
            CatalogCard(
                card_id="b",
                name="Falinks",
                set_id="s2",
                set_name="Rebel Clash",
                number="109",
            ),
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


def _ranking_catalog(conn):
    """A set *named* after a card, plus a vintage and a modern printing of it.

    The real shape behind ticket 67: "SV: Terastal Charizard ex Battle Deck"
    made every card in it match the token "Charizard", and alphabetical ordering
    floated them above the card actually being searched for.
    """
    from tcginventory.domain.models import CatalogCard

    def card(card_id, name, set_id, set_name, number):
        return CatalogCard(
            card_id=card_id,
            name=name,
            set_id=set_id,
            set_name=set_name,
            number=number,
        )

    CardCatalogRepository(conn).replace_all(
        [
            # Cards whose only tie to the query is their set's name. They sort
            # first alphabetically, which is exactly the bug.
            card("d1", "Arceus V", "deck", "SV: Terastal Charizard ex Deck", "009"),
            card("d2", "Artazon", "deck", "SV: Terastal Charizard ex Deck", "020"),
            card("d3", "Bibarel", "deck", "SV: Terastal Charizard ex Deck", "008"),
            card("d4", "Bidoof", "deck", "SV: Terastal Charizard ex Deck", "007"),
            # The vintage uppercase printing. BINARY collation sorts "EX" before
            # "ex", so this used to beat the modern card on an exact-name tie.
            card("v1", "Charizard EX", "flf", "Flashfire", "11"),
            # The card the search is actually for.
            card("m1", "Charizard ex", "sv03.5", "151", "199"),
        ],
        "t",
    )
    return CardCatalogRepository(conn)


def test_search_ranks_own_name_above_set_name_matches(conn):
    """A card matched only through its set name must not crowd out the card."""
    repo = _ranking_catalog(conn)

    # All six still match — the predicate is unchanged, only the order is.
    assert len(repo.search("Charizard ex")) == 6

    # The two Charizards rank above the four deck fillers, so a short popup
    # still shows them. Pre-fix this returned Arceus V and Artazon.
    top = [c.name for c in repo.search("Charizard ex", limit=2)]
    assert top == ["Charizard ex", "Charizard EX"]


def test_search_prefers_the_case_the_user_typed(conn):
    """ "Charizard ex" means the modern card; "Charizard EX" the vintage one."""
    repo = _ranking_catalog(conn)

    assert repo.search("Charizard ex", limit=1)[0].card_id == "m1"
    assert repo.search("Charizard EX", limit=1)[0].card_id == "v1"


def test_full_number_combines_number_with_set_total():
    from tcginventory.domain.models import CatalogCard

    def card(number, total):
        return CatalogCard(
            card_id="x", name="n", set_id="s", number=number, printed_total=total
        )

    # plain numerator → numeric denominator; lettered numerator keeps its prefix.
    assert card("124", 142).full_number == "124/142"
    assert card("SV103", 122).full_number == "SV103/SV122"
    assert card("TG10", 30).full_number == "TG10/TG30"
    # unknown total or missing number degrade gracefully.
    assert card("124", None).full_number == "124"
    assert card(None, 142).full_number is None
    # A number the source already denominated keeps its own denominator — the
    # set total is not this card's ("8/82/25" would be nonsense). Covers the JP
    # rows and the Celebrations Classic Collection reprints.
    assert card("8/82", 25).full_number == "8/82"
    assert card("046/081", None).full_number == "046/081"


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
        if url.endswith("/sets/cel25"):
            raise RuntimeError("boom")
        return _fake_fetch(url)

    summary = update_catalog(conn, fetch_json=flaky)
    assert summary.cards == 2  # only sv9's cards survived
    assert summary.errors and "cel25" in summary.errors[0]
    assert CardCatalogRepository(conn).count() == 2  # replaced, not appended


def test_update_catalog_keeps_catalog_when_source_unreachable(conn):
    update_catalog(conn, fetch_json=_fake_fetch)
    assert CardCatalogRepository(conn).count() == 3

    # TCGdex down → the set index is empty; the existing catalog must survive.
    summary = update_catalog(conn, fetch_json=lambda _url: [])
    assert summary.cards == 0 and summary.errors
    assert CardCatalogRepository(conn).count() == 3  # left intact, not wiped


def test_update_catalog_from_github_fallback_still_works(conn):
    from tcginventory.services.catalog import update_catalog_from_github

    sets = [
        {
            "id": "sv9",
            "name": "Journey Together",
            "ptcgoCode": "JTG",
            "printedTotal": 159,
        }
    ]
    cards = [
        {
            "id": "sv9-1",
            "name": "Pikachu",
            "number": "1",
            "rarity": "Common",
            "supertype": "Pokémon",
            "images": {"small": "https://images.pokemontcg.io/sv9/1.png"},
        }
    ]

    def fetch(url: str):
        if url.endswith("/sets/en.json"):
            return sets
        if url.endswith("/cards/en/sv9.json"):
            return cards
        raise AssertionError(url)

    summary = update_catalog_from_github(conn, fetch_json=fetch)
    assert summary.cards == 1
    (pika,) = CardCatalogRepository(conn).find_by_name("Pikachu")
    assert (pika.number, pika.rarity, pika.image_small) == (
        "1",
        "Common",
        "https://images.pokemontcg.io/sv9/1.png",
    )


def _hybrid_fetch(url: str):
    # TCGdex side: a Trainer Gallery card with NO image (as TCGdex serves them).
    if url.endswith("/sets"):
        return [{"id": "swsh10tg", "name": "Astral Radiance Trainer Gallery"}]
    if url.endswith("/sets/swsh10tg"):
        return {
            "id": "swsh10tg",
            "name": "Astral Radiance Trainer Gallery",
            "abbreviation": {"official": "TG"},
            "cardCount": {"official": 30, "total": 30},
            "cards": [
                {
                    "id": "swsh10.5tg-TG01",
                    "localId": "TG01",
                    "name": "Abomasnow",
                    "image": None,
                }
            ],
        }
    # pokemontcg.io dump side (the fallback): same identity, WITH an image.
    if url.endswith("/sets/en.json"):
        return [
            {
                "id": "swsh10tg",
                "name": "Astral Radiance Trainer Gallery",
                "printedTotal": 30,
            }
        ]
    if url.endswith("/cards/en/swsh10tg.json"):
        return [
            {
                "id": "swsh10tg-TG01",
                "name": "Abomasnow",
                "number": "TG01",
                "images": {"small": "https://images.pokemontcg.io/swsh10tg/TG01.png"},
            }
        ]
    raise AssertionError(f"unexpected url: {url}")


def test_missing_tcgdex_images_are_filled_from_github(conn):
    # TCGdex lacks art for most Trainer Gallery / Shiny Vault subset cards; the
    # pokemontcg.io dump supplies it, joined by name + set + number.
    update_catalog(conn, fetch_json=_hybrid_fetch)
    (card,) = CardCatalogRepository(conn).find_by_name("Abomasnow")
    assert card.image_small == "https://images.pokemontcg.io/swsh10tg/TG01.png"
    assert card.number == "TG01"


def _classic_collection_fetch(url: str):
    # TCGdex side: the synthetic "CC005" index, and no art for the set at all.
    if url.endswith("/sets"):
        return [{"id": "cel25cc", "name": "Celebrations Classic Collection"}]
    if url.endswith("/sets/cel25cc"):
        return {
            "id": "cel25cc",
            "name": "Celebrations Classic Collection",
            "abbreviation": {"official": "CEL:CC"},
            "cardCount": {"official": 25, "total": 25},
            "cards": [
                {
                    "id": "cel25cc-CC005",
                    "localId": "CC005",
                    "name": "Dark Gyarados",
                    "image": None,
                }
            ],
        }
    # pokemontcg.io dump side: same card under its *printed* numerator, "8".
    if url.endswith("/sets/en.json"):
        return [
            {
                "id": "cel25c",
                "name": "Celebrations: Classic Collection",
                "printedTotal": 25,
            }
        ]
    if url.endswith("/cards/en/cel25c.json"):
        return [
            {
                "id": "cel25c-8_A",
                "name": "Dark Gyarados",
                "number": "8",
                "images": {"small": "https://images.pokemontcg.io/cel25c/8_A.png"},
            }
        ]
    raise AssertionError(f"unexpected url: {url}")


def test_classic_collection_stores_the_printed_collector_number(conn):
    # TCGdex numbers this set "CC001…CC025", which is printed on no card and
    # matches no price source. Every card keeps its original set's number, so
    # the pull must store "8/82" — that is what tcgcsv prices Dark Gyarados by.
    update_catalog(conn, fetch_json=_classic_collection_fetch)
    repo = CardCatalogRepository(conn)
    (card,) = repo.find_by_name("Dark Gyarados")
    assert card.number == "8/82"
    # The set's own total (25) must not be appended, and the "CC" prefix must
    # not drive a subset denominator — the row showed "CC005/CC25" before.
    assert card.full_number == "8/82"
    assert repo.display_number(card) == "8/82"


def test_catalog_keeps_the_padding_each_set_actually_prints(conn):
    """Padding is a property of the set, so it is neither added nor removed.

    TCGdex's localId already carries each era's convention — "001" for SV-era
    sets like 151, "5" for Celebrations. Normalising either way is wrong for
    half the catalog: stripping made a card printed "006/165" render as "6/165"
    (ticket 49), and padding would make Base Set "4/102" read "004/102".
    """
    update_catalog(conn, fetch_json=_fake_fetch)
    repo = CardCatalogRepository(conn)

    (padded,) = [c for c in repo.find_by_name("Pikachu") if c.set_id == "sv9"]
    (unpadded,) = [c for c in repo.find_by_name("Pikachu") if c.set_id == "cel25"]
    assert padded.number == "001"
    assert unpadded.number == "5"
    # …and it survives into what the row displays, which is the reported symptom.
    assert padded.full_number == "001/159"
    assert repo.display_number(padded) == "001/159"


def test_classic_collection_art_joins_on_the_printed_numerator(conn):
    # The dump denominates differently ("8" vs the stored "8/82"), so the image
    # fallback has to join on the numerator or the whole set stays art-less.
    update_catalog(conn, fetch_json=_classic_collection_fetch)
    (card,) = CardCatalogRepository(conn).find_by_name("Dark Gyarados")
    assert card.image_small == "https://images.pokemontcg.io/cel25c/8_A.png"


# ---------------------------------------------------------------------------
# The FTS5 search index (migration 0020). search() answers from a trigram index
# instead of a full scan, but the SQL predicate is still the authority: the index
# only prunes the rows it runs against. These tests pin that equivalence, because
# a prune narrower than the predicate silently loses real results.


def _forms(text):
    """The number forms this oracle expects — spelled out rather than imported,
    so a bug in the real ``number_forms`` cannot be mirrored into the oracle."""
    bare = text.lstrip("0") or text
    out = [text, bare]
    if bare.isdigit():
        out.append(bare.zfill(3))
    return list(dict.fromkeys(out))


def _like_only_search(conn, query, limit=25):
    """The pre-0020 implementation, kept as the parity oracle.

    Deliberately a copy rather than a call: the point is to compare the indexed
    path against the predicate semantics, so this must not move when search()'s
    *implementation* does. It moves only when the predicate's *meaning* changes
    by intent — once so far, in ticket 49, when a number query became able to
    match a row stored in the other padding convention.
    """
    clauses, params = [], []
    for tok in query.strip().split():
        like = f"%{tok}%"
        parts = ["name LIKE ?", "set_name LIKE ?", "set_code LIKE ?"]
        tok_params = [like, like, like]
        if "/" in tok and any(ch.isdigit() for ch in tok):
            head = tok.split("/", 1)[0].strip()
            for form in dict.fromkeys([tok, *_forms(head)]):
                parts.append("number = ? COLLATE NOCASE")
                tok_params.append(form)
        elif any(ch.isalpha() for ch in tok):
            parts.append("number LIKE ? COLLATE NOCASE")
            tok_params.append(f"{tok}%")
        elif tok.isdigit():
            for form in _forms(tok):
                parts.append("number = ? COLLATE NOCASE")
                tok_params.append(form)
        clauses.append("(" + " OR ".join(parts) + ")")
        params += tok_params
    rows = conn.execute(
        "SELECT card_id FROM card_catalog WHERE "
        + " AND ".join(clauses)
        + " ORDER BY name, set_name, number LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [r["card_id"] for r in rows]


def test_search_matches_the_like_scan_it_replaced(conn):
    update_catalog(conn, fetch_json=_fake_fetch)
    repo = CardCatalogRepository(conn)

    # Every token form search()'s docstring promises, plus the ones the trigram
    # index cannot serve and must fall through to the scan for.
    for query in (
        "Pikachu Celebrations",
        "Pikachu Celebrations 5",
        "Pikachu 5 Celebrations",
        "Pikachu 1/99",
        "Kilowattrel",
        "Iono",
        "pik",  # below the trigram floor in one token, still exact
        "ex 5",  # no token long enough to prune on at all
        "Pikachu 005",  # zero-padded, long enough to prune, stored unpadded
        "Pikachu 005/025",  # …and the same padding inside a full number
        "005",
        "zzz nothing here",
        'quote"inside',  # the phrase terminator: must be escaped, not fatal
    ):
        assert [c.card_id for c in repo.search(query)] == _like_only_search(
            conn, query
        ), query


def test_search_finds_a_zero_padded_number_stored_unpadded(conn):
    """The prune must not out-narrow the predicate on zero padding.

    The catalog holds both conventions — English rows store the stripped "5",
    Japanese rows the printed "005/025" — so the predicate accepts "5" for a
    token of "005". A prune requiring the literal "005" deletes that row before
    the predicate ever sees it, which is how a card printed "006/165" became
    unfindable by its own number (ticket 49).
    """
    update_catalog(conn, fetch_json=_fake_fetch)
    repo = CardCatalogRepository(conn)

    (hit,) = repo.search("Pikachu 005")  # Celebrations #5
    assert (hit.card_id, hit.number) == ("cel25-5", "5")
    assert [c.card_id for c in repo.search("Pikachu 005/025")] == ["cel25-5"]

    # The other tokens still carry the index — only the padded one abstains.
    assert repo._fts_prune("Pikachu 005") == '"Pikachu"'
    assert repo._fts_prune("005") == ""


def test_search_finds_a_bare_number_given_in_full(conn):
    """The case that makes the index a *prune* and not a translation.

    "SV103/SV122" appears nowhere in a row numbered plain "SV103", so matching
    the whole token against the index finds nothing. Pruning on the head keeps
    the row, which the predicate then confirms.
    """
    from tcginventory.domain.models import CatalogCard

    CardCatalogRepository(conn).replace_all(
        [
            CatalogCard(
                card_id="w",
                name="Wooloo",
                set_id="s1",
                set_name="Shining Fates Shiny Vault",
                number="SV103",
            )
        ],
        "t",
    )
    (hit,) = CardCatalogRepository(conn).search("Wooloo Shiny Vault SV103/SV122")
    assert hit.card_id == "w"


def test_search_index_is_rebuilt_when_the_catalog_is_replaced(conn):
    """A stale index is worse than no index: it answers with rows that are gone.

    replace_for_language must leave the other language's rows searchable and the
    replaced language's old rows unfindable.
    """
    from tcginventory.domain.models import CatalogCard

    repo = CardCatalogRepository(conn)
    repo.replace_all(
        [
            CatalogCard(
                card_id="en1", name="Snorlax", set_id="s1", number="1", language="EN"
            ),
            CatalogCard(
                card_id="jp1", name="Kabigon", set_id="s2", number="2", language="JP"
            ),
        ],
        "t",
    )
    assert [c.card_id for c in repo.search("Snorlax")] == ["en1"]

    repo.replace_for_language(
        [
            CatalogCard(
                card_id="en2", name="Munchlax", set_id="s1", number="3", language="EN"
            )
        ],
        "EN",
        "t2",
    )
    assert repo.search("Snorlax") == []  # the replaced row is gone from the index
    assert [c.card_id for c in repo.search("Munchlax")] == ["en2"]
    assert [c.card_id for c in repo.search("Kabigon")] == ["jp1"]  # untouched

    counts = conn.execute(
        "SELECT (SELECT count(*) FROM card_catalog) AS rows, "
        "(SELECT count(*) FROM card_catalog_fts) AS indexed"
    ).fetchone()
    assert counts["rows"] == counts["indexed"]


def test_display_numbers_batches_without_changing_the_answer(conn):
    """The batched form is what the popups call, so it must agree with the single
    lookup row for row — including the subset denominator it exists to derive."""
    from tcginventory.domain.models import CatalogCard

    cards = [
        CatalogCard(
            card_id=f"rc{n}",
            name=f"Radiant {n}",
            set_id="s1",
            set_name="Legendary Treasures",
            number=f"RC{n}",
            printed_total=113,
        )
        for n in range(1, 4)
    ] + [
        CatalogCard(
            card_id="plain",
            name="Radiant Plain",
            set_id="s1",
            set_name="Legendary Treasures",
            number="12",
            printed_total=113,
        )
    ]
    repo = CardCatalogRepository(conn)
    repo.replace_all(cards, "t")

    stored = repo.search("Radiant")
    batched = repo.display_numbers(stored)
    assert batched == {c.card_id: repo.display_number(c) for c in stored}
    # The subset's own size (3), not the parent set's printed_total (113).
    assert batched["rc1"] == "RC1/RC3"
    assert batched["plain"] == "12/113"

    # search_labelled is the pairing the autocomplete rows consume.
    assert [(c.card_id, n) for c, n in repo.search_labelled("Radiant")] == [
        (c.card_id, batched[c.card_id]) for c in stored
    ]
