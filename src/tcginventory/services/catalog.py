"""Local reference card catalog.

* **English** comes from the open **TCGdex** API (api.tcgdex.net) — a set index
  plus one set-detail file per set (its full card list + images). It's the most
  current free source (has the newest promos the pokemontcg.io GitHub dump lags
  on). The GitHub dump remains as :func:`update_catalog_from_github`.
* **Japanese** comes from tcgcsv.com's "Pokemon Japan" category (85): groups
  (sets) and one products file per group, with English/romaji names, collector
  numbers, TCGplayer-CDN images, and — later — the same source for pricing.

Both are bulk, no API key, cached into the ``card_catalog`` table (tagged by
language) so autocomplete / set-number / image lookup work offline; "update"
re-pulls. Qt-free; network I/O is injected so it's testable offline.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import (
    CardCatalogRepository,
    CardTreatmentRepository,
)
from tcginventory.domain.models import CatalogCard

# tcgcsv embeds the collector number in the product name to disambiguate
# same-named cards ("Mega Darkrai ex - 046/081", "Landorus - 398/742 (Mirror
# Holo)"). Strip that " - <number>" segment so JP names read like the English
# catalog; the number already lives in its own field.
_NUMBER_IN_NAME = re.compile(r"\s*-\s*\d+/[\w-]+")

_RAW = "https://raw.githubusercontent.com/PokemonTCG/pokemon-tcg-data/master"
_TCGDEX = "https://api.tcgdex.net/v2/en"
_TCGCSV = "https://tcgcsv.com/tcgplayer"
_CATEGORY_JP = 85  # tcgcsv "Pokemon Japan"

# tcgcsv doesn't carry a collector Number for some vintage JP sets (e.g.
# "Challenge from the Darkness", 1998). Those products are still cards — identify
# them by these per-card mechanic fields so they aren't skipped as sealed product.
_JP_CARD_FIELDS = (
    "HP",
    "CardType",
    "Stage",
    "Attack 1",
    "Attack 2",
    "Weakness",
    "Resistance",
    "Retreat Cost",
)


def _fetch_json(url: str):  # pragma: no cover - real network
    import httpx

    resp = httpx.get(url, timeout=30.0, headers={"User-Agent": "TCGinventory/1.0"})
    resp.raise_for_status()
    return resp.json()


def _fetch_tcgcsv(url: str):  # pragma: no cover - real network
    import httpx

    # tcgcsv sits behind Cloudflare; use a browser-like UA.
    resp = httpx.get(url, timeout=30.0, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.json()


def _results(data) -> list:
    if isinstance(data, dict):
        return data.get("results") or []
    return data or []


@dataclass
class CatalogUpdateSummary:
    sets: int = 0
    cards: int = 0
    # How many rows came out with an illustrator. Reported because the attribute
    # sweep is best-effort: a pull that quietly resolved none of them still
    # succeeds, and this is the number that says so.
    artists: int = 0
    # Cards for which at least one foil printing was resolved. Reported because
    # the treatment sweep is best-effort: a pull that resolved none still
    # succeeds, and this is the number that says so. (Ticket 55.)
    treatments: int = 0
    errors: list[str] = field(default_factory=list)


def _tcgdex_number(local_id: str | None) -> str | None:
    """TCGdex ``localId`` → the collector number **printed on the card**.

    Kept verbatim, because ``localId`` already carries each era's own
    convention: Base Set, Celebrations and Sword & Shield give "1, 2, 3, 4"
    while 151 and every SV-era set give "001, 002, 003, 004" — which is exactly
    how those cards are printed, and how tcgcsv names the products
    ("...-charizard-ex-006-165").

    This used to strip the padding to a uniform "1", which was only ever right
    for the eras that do not pad: it made a card printed "006/165" render and
    save as "6/165", and unfindable by its own number. Matching is normalised on
    both sides instead — see :func:`repositories.number_forms`. (Ticket 49.)
    """
    text = (local_id or "").strip()
    return text or None


# TCGdex indexes the Celebrations Classic Collection with a synthetic
# "CC001"…"CC025" sequence. No card carries that number: every one of the 25 is a
# reprint that keeps its *original* set's collector number ("Dark Gyarados 8/82"),
# which is what is printed on the card, what tcgcsv/TCGplayer price it by, and
# what the pokemontcg.io art dump keys on. Left as TCGdex gives it, the whole set
# autofills with no price, no product link and no image.
#
# A static table rather than a live join: the set is 25 cards, frozen since 2021,
# and it is the only English set whose TCGdex numbering disagrees with every other
# source. Authored by joining tcgcsv group 2931's products by name. (Ticket 012.)
_CLASSIC_COLLECTION_SET = "cel25cc"
_CLASSIC_COLLECTION_NUMBERS = {
    "CC001": "2/102",  # Blastoise
    "CC002": "4/102",  # Charizard
    "CC003": "15/102",  # Venusaur
    "CC004": "73/102",  # Imposter Professor Oak
    "CC005": "8/82",  # Dark Gyarados
    "CC006": "15/82",  # Here Comes Team Rocket!
    "CC007": "15/132",  # Rocket's Zapdos
    "CC008": "24/53",  # _____'s Pikachu
    "CC009": "20/111",  # Cleffa
    "CC010": "66/64",  # Shining Magikarp
    "CC011": "9/95",  # Team Magma's Groudon
    "CC012": "86/109",  # Rocket's Admin.
    "CC013": "88/92",  # Mew ex
    "CC014": "93/101",  # Gardevoir ex (Delta Species)
    "CC015": "17/17",  # Umbreon Star
    "CC016": "15/106",  # Claydol
    "CC017": "109/111",  # Luxray GL LV.X
    "CC018": "145/147",  # Garchomp C LV.X
    "CC019": "107/123",  # Donphan (Prime)
    "CC020": "113/114",  # Reshiram
    "CC021": "114/114",  # Zekrom
    "CC022": "54/99",  # Mewtwo EX
    "CC023": "97/146",  # Xerneas EX
    "CC024": "76/108",  # M Rayquaza EX
    "CC025": "60/145",  # Tapu Lele GX
}


def _printed_number(set_id: str, local_id: str | None) -> str | None:
    """The collector number **printed on the card**, from TCGdex's ``localId``.

    Normally that is the localId itself; the Classic Collection is the one set
    where TCGdex's index and the printed number differ (see the table above)."""
    if set_id == _CLASSIC_COLLECTION_SET:
        printed = _CLASSIC_COLLECTION_NUMBERS.get((local_id or "").strip())
        if printed:
            return printed
    return _tcgdex_number(local_id)


def _tcgdex_image(image: str | None) -> str | None:
    """TCGdex serves an *asset base* URL; the concrete file needs a quality +
    extension. Use a small-ish PNG comparable to pokemontcg.io's ``small``."""
    return f"{image}/low.png" if image else None


def _norm_join(text: str | None) -> str:
    """Loose key for joining the two sources by identity (case/punctuation-free)."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _norm_join_number(number: str | None) -> str:
    """Join key for a collector number: the numerator alone.

    The sources denominate differently — the catalog stores the printed
    "8/82" for a Classic Collection reprint, the pokemontcg.io dump the bare
    "8" — so join on the head and let the name and set carry the rest."""
    return _norm_join((number or "").split("/", 1)[0])


def _fetch_map(fetch, urls, workers: int = 16) -> dict:
    """Fetch many URLs concurrently → ``{url: result | Exception}``. A catalog
    pull is hundreds of small requests; doing them in a thread pool cuts it from
    minutes to seconds. The injected ``fetch`` must be thread-safe (the real
    httpx one and the test fakes are)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out: dict = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, url): url for url in urls}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                out[url] = fut.result()
            except Exception as exc:  # recorded per-url; caller decides
                out[url] = exc
    return out


def _github_image_index(fetch) -> dict[tuple[str, str, str], str]:
    """``(name, set_name, number) → image_small`` from the pokemontcg.io dump —
    used to fill images TCGdex omits (it lacks art for most Trainer Gallery /
    Shiny Vault / Galarian Gallery subset cards, which pokemontcg.io carries).
    Identities line up: same names, set names, and (prefixed) numbers."""
    sets = _as_list(fetch(f"{_RAW}/sets/en.json"))
    set_names = {
        s["id"]: s.get("name") for s in sets if isinstance(s, dict) and s.get("id")
    }
    urls = {set_id: f"{_RAW}/cards/en/{set_id}.json" for set_id in set_names}
    fetched = _fetch_map(fetch, list(urls.values()))
    index: dict[tuple[str, str, str], str] = {}
    for set_id, set_name in set_names.items():
        entries = fetched.get(urls[set_id])
        if isinstance(entries, Exception):
            continue
        for c in _as_list(entries):
            image = (c.get("images") or {}).get("small")
            if not image:
                continue
            key = (
                _norm_join(c.get("name")),
                _norm_join(set_name),
                _norm_join_number(c.get("number")),
            )
            index.setdefault(key, image)
    return index


def _fill_missing_images(cards, fetch) -> int:
    """Fill ``image_small`` on TCGdex cards that have none, from the pokemontcg.io
    dump. Best-effort — if that source is unreachable the cards just stay
    image-less (no thumbnail), never an error."""
    missing = [c for c in cards if not c.image_small]
    if not missing:
        return 0
    try:
        index = _github_image_index(fetch)
    except Exception:
        return 0
    filled = 0
    for card in missing:
        image = index.get(
            (
                _norm_join(card.name),
                _norm_join(card.set_name),
                _norm_join_number(card.number),
            )
        )
        if image:
            card.image_small = image
            filled += 1
    return filled


def _tcgdex_set_code(detail: dict) -> str | None:
    abbr = detail.get("abbreviation")
    if isinstance(abbr, dict):
        return abbr.get("official") or abbr.get("localId")
    if isinstance(abbr, str):
        return abbr
    serie = detail.get("serie")
    return serie.get("id") if isinstance(serie, dict) else None


# ---------------------------------------------------------------------------
# English attribute sweep — artist, rarity, supertype
# ---------------------------------------------------------------------------

# TCGdex's set payload (``/sets/{id}``) carries only a card *brief* — id,
# localId, name, image — which is why the English pull left rarity and supertype
# NULL, and had no artist to leave at all. Those three live on the per-card
# endpoint: 23,444 requests for English, roughly eighty minutes.
#
# TCGdex also publishes an index per axis (``/illustrators``, ``/rarities``,
# ``/categories``) and filters cards by it (``/cards?illustrator=…``). Inverting
# those indexes costs one request per *value* rather than per card: 410 + 40 + 3
# against 23,444, measured at 81s for the whole sweep and 96.6% / 99.5% / 99.5%
# coverage of the English catalog.
#
# ``/variants`` exists but ``?variants=`` matches nothing, so foil treatment
# (holo / reverse / first edition) is not reachable this way — it is per-card
# only, and deliberately out of scope.
_ATTRIBUTE_AXES: tuple[tuple[str, str, str], ...] = (
    ("artist", "illustrators", "illustrator"),
    ("rarity", "rarities", "rarity"),
    ("supertype", "categories", "category"),
)


def _resolve_longest(claims: dict[str, set[str]]) -> dict[str, str]:
    """Reduce ``{card_id: {every index value that claimed it}}`` to one value each.

    The filters are **substring**-matched, not exact. ``?illustrator=Uta``
    returns 493 cards, the first of them drawn by *Saya Tsuruta*; 2,935 of the
    23,444 English cards are claimed by more than one illustrator name this way.
    Keeping whichever arrived last attributes those cards by the order TCGdex
    happened to list its illustrators in — wrong on 13% of the catalog, and
    silently, which is why this is a named function with a test rather than a
    ``dict`` assignment inside the loop.

    A value ``N`` is returned for a card exactly when ``N`` is a substring of the
    card's true value ``A``. ``A`` always claims itself, and every false positive
    is therefore a *proper* substring of ``A`` — so the true value is always the
    **longest** claimant. Checked against the per-card endpoint on 25
    multi-claim cards: 25/25 correct, no counterexample.

    Sorted before ``max`` so an equal-length tie takes the alphabetically first
    claimant rather than whatever set iteration happened to yield, which is not
    stable across runs. Two values of equal length cannot each be a substring of
    the other, so a tie means neither is the true value anyway — determinism is
    all that is available, and all that matters.
    """
    return {
        card_id: max(sorted(names), key=len)
        for card_id, names in claims.items()
        if names
    }


def _attribute_plan(
    fetch: Callable[[str], object],
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """The sweep's work list: one ``(attribute, query param, value)`` per index
    entry.

    Fetched up front — three requests — so the caller can size a progress bar
    across the sweep instead of watching it stall after the set phase. An axis
    whose index will not load is dropped and reported; the others still run,
    because a missing rarity costs less than a lost catalog.
    """
    plan: list[tuple[str, str, str]] = []
    errors: list[str] = []
    for attribute, index_path, param in _ATTRIBUTE_AXES:
        try:
            values = _as_list(fetch(f"{_TCGDEX}/{index_path}"))
        except Exception as exc:
            errors.append(f"{attribute} index: {exc}")
            continue
        plan.extend(
            (attribute, param, value.strip())
            for value in values
            if isinstance(value, str) and value.strip()
        )
    return plan, errors


def _sweep_attributes(
    fetch: Callable[[str], object],
    plan: list[tuple[str, str, str]],
    on_step: Callable[[int], None] | None = None,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Run the work list, returning ``{attribute: {card_id: value}}``.

    One value that fails to fetch is recorded and skipped rather than abandoning
    the sweep: this is best-effort enrichment layered onto a catalog that is
    already complete without it, and a single dead illustrator query must not
    cost the other 409.
    """
    claims: dict[str, dict[str, set[str]]] = {}
    errors: list[str] = []
    for done, (attribute, param, value) in enumerate(plan, start=1):
        try:
            cards = _as_list(fetch(f"{_TCGDEX}/cards?{param}={quote(value)}"))
        except Exception as exc:
            errors.append(f"{attribute}={value}: {exc}")
            cards = []
        axis = claims.setdefault(attribute, {})
        for card in cards:
            if isinstance(card, dict) and card.get("id"):
                axis.setdefault(card["id"], set()).add(value)
        if on_step is not None:
            on_step(done)
    return {attr: _resolve_longest(a) for attr, a in claims.items()}, errors


# ---------------------------------------------------------------------------
# Foil treatments
# ---------------------------------------------------------------------------


def _sweep_treatments(
    cards: list[CatalogCard],
    provider,
    category: int = 3,
) -> tuple[list[tuple[str, str]], list[str]]:
    """``[(card_id, treatment)]`` for every card a tcgcsv product can be found for.

    Which foil printings a card exists in is not in any catalog feed, but it *is*
    in the tcgcsv price files the pricing service already downloads: one row per
    product per ``subTypeName``. So this costs group resolution and a cached
    fetch per set, not a per-card request — the alternative, TCGdex's per-card
    ``variants``, is 23,546 requests because ``?variants=`` filters nothing.

    Joined set-by-set the way ``marketprices._process_set`` already does it:
    resolve the set to a tcgcsv group, index that group's products by normalised
    collector number, and read the treatments off the price rows. A set that will
    not resolve is recorded and skipped rather than failing the pull.
    """
    from tcginventory.services.pricing import _norm_number

    rows: list[tuple[str, str]] = []
    errors: list[str] = []
    by_set: dict[str, list[CatalogCard]] = {}
    for card in cards:
        by_set.setdefault(card.set_id, []).append(card)

    for set_id, group in by_set.items():
        sample = group[0]
        try:
            group_id = provider.resolve_group(
                sample.set_name, sample.set_code, category
            )
            if group_id is None:
                errors.append(f"{set_id}: no tcgcsv group")
                continue
            prices = provider.treatments_by_product(category, group_id)
            by_number: dict[str, int] = {}
            for pid, number, _url, _name in provider.products(category, group_id):
                by_number.setdefault(_norm_number(number), pid)
        except Exception as exc:
            errors.append(f"{set_id}: {exc}")
            continue
        for card in group:
            pid = by_number.get(_norm_number(card.number))
            if pid is None:
                continue
            for treatment in prices.get(pid, ()):
                if treatment:
                    rows.append((card.card_id, treatment))
    return rows, errors


def update_catalog(
    conn: sqlite3.Connection,
    fetch_json: Callable[[str], object] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    price_provider=None,
) -> CatalogUpdateSummary:
    """Pull the full English card dataset from **TCGdex** into ``card_catalog``.

    Fetches the set index (``/sets``), then each set's detail (``/sets/{id}``,
    which carries that set's full card list + images), and replaces the English
    catalog wholesale. A single set that fails is recorded and skipped; if the
    whole pull yields nothing (TCGdex unreachable) the existing catalog is left
    untouched rather than wiped. No API key.

    TCGdex omits per-card rarity/supertype from the set payload (they need a
    per-card request each), so those columns are left NULL — the app doesn't key
    behaviour on them. See :func:`update_catalog_from_github` for the legacy
    source, kept as a fallback.
    """
    fetch = fetch_json or _fetch_json
    summary = CatalogUpdateSummary()

    sets = _as_list(fetch(f"{_TCGDEX}/sets"))
    valid = [s for s in sets if isinstance(s, dict) and s.get("id")]
    total = len(valid)
    details = _fetch_map(fetch, [f"{_TCGDEX}/sets/{s['id']}" for s in valid])

    # The attribute sweep is about half this pull's wall clock, so its work list
    # is planned here and folded into one progress span with the set phase —
    # otherwise the bar reaches 100% and then sits there for 80 seconds.
    plan, plan_errors = _attribute_plan(fetch)
    summary.errors.extend(plan_errors)
    steps = total + len(plan)

    cards: list[CatalogCard] = []
    for done, s in enumerate(valid, start=1):
        set_id = s["id"]
        detail = details.get(f"{_TCGDEX}/sets/{set_id}")
        if not isinstance(detail, dict):
            summary.errors.append(f"{set_id}: {detail}")
            if on_progress is not None:
                on_progress(done, steps)
            continue
        set_name = detail.get("name") or s.get("name")
        set_code = _tcgdex_set_code(detail)
        counts = detail.get("cardCount") or {}
        printed_total = counts.get("official") or counts.get("total")
        for c in detail.get("cards") or []:
            card_id = c.get("id")
            if not card_id:
                continue
            cards.append(
                CatalogCard(
                    card_id=card_id,
                    name=c.get("name") or "",
                    set_id=set_id,
                    set_name=set_name,
                    set_code=set_code,
                    number=_printed_number(set_id, c.get("localId")),
                    # Both filled by the attribute sweep below — the set payload
                    # is a card brief and carries neither.
                    rarity=None,
                    supertype=None,
                    image_small=_tcgdex_image(c.get("image")),
                    printed_total=printed_total,
                )
            )
        if on_progress is not None:
            on_progress(done, steps)

    if not cards:
        summary.errors.append("no cards fetched — catalog left unchanged")
        return summary

    # Enrich *before* the write, not after it: replace_for_language DELETEs the
    # language's rows and re-inserts, so anything applied by a later UPDATE would
    # be erased by the next pull. Populating the dataclasses here makes the
    # columns correct by construction.
    attributes, sweep_errors = _sweep_attributes(
        fetch,
        plan,
        on_step=(
            None
            if on_progress is None
            else lambda done: on_progress(total + done, steps)
        ),
    )
    summary.errors.extend(sweep_errors)
    by_artist = attributes.get("artist", {})
    by_rarity = attributes.get("rarity", {})
    by_supertype = attributes.get("supertype", {})
    for card in cards:
        card.artist = by_artist.get(card.card_id)
        card.rarity = by_rarity.get(card.card_id)
        card.supertype = by_supertype.get(card.card_id)
    summary.artists = sum(1 for c in cards if c.artist)

    _fill_missing_images(cards, fetch)

    # Which foil printings each card comes in. Best-effort like the attribute
    # sweep: a total failure still writes a complete catalog, just with no
    # treatments — a Collections foil template then has nothing to offer, which
    # is the honest state and better than a half-filled one that looks whole.
    treatments: list[tuple[str, str]] = []
    try:
        if price_provider is None:
            from tcginventory.services.pricing import TcgcsvProvider

            price_provider = TcgcsvProvider()
        treatments, treatment_errors = _sweep_treatments(cards, price_provider)
        summary.errors.extend(treatment_errors)
    except Exception as exc:
        summary.errors.append(f"treatment sweep: {exc}")

    try:
        # One transaction: replace_for_language DELETEs and re-inserts every card
        # row, so treatments written outside it would reference card_ids that no
        # longer exist (or be wiped by the next pull).
        CardCatalogRepository(conn).replace_for_language(cards, "EN", now_iso())
        CardTreatmentRepository(conn).replace_for_language(treatments, "EN")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    summary.sets = total
    summary.cards = len(cards)
    summary.treatments = len({card_id for card_id, _ in treatments})
    return summary


def update_catalog_from_github(
    conn: sqlite3.Connection,
    fetch_json: Callable[[str], object] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> CatalogUpdateSummary:
    """Legacy English source: the pokemontcg.io GitHub dump. Kept as a fallback
    (it lags the live data for the newest promos). Atomic; a single failed set is
    recorded and skipped."""
    fetch = fetch_json or _fetch_json
    summary = CatalogUpdateSummary()

    sets = _as_list(fetch(f"{_RAW}/sets/en.json"))
    set_meta = {
        s["id"]: (s.get("name"), s.get("ptcgoCode"), s.get("printedTotal"))
        for s in sets
        if isinstance(s, dict) and s.get("id")
    }
    total = len(set_meta)

    cards: list[CatalogCard] = []
    for i, (set_id, (set_name, set_code, printed_total)) in enumerate(set_meta.items()):
        try:
            entries = _as_list(fetch(f"{_RAW}/cards/en/{set_id}.json"))
        except Exception as exc:
            summary.errors.append(f"{set_id}: {exc}")
            continue
        for c in entries:
            card_id = c.get("id")
            if not card_id:
                continue
            cards.append(
                CatalogCard(
                    card_id=card_id,
                    name=c.get("name") or "",
                    set_id=set_id,
                    set_name=set_name,
                    set_code=set_code,
                    number=c.get("number"),
                    rarity=c.get("rarity"),
                    supertype=c.get("supertype"),
                    image_small=(c.get("images") or {}).get("small"),
                    printed_total=printed_total,
                )
            )
        if on_progress is not None:
            on_progress(i + 1, total)

    try:
        CardCatalogRepository(conn).replace_for_language(cards, "EN", now_iso())
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    summary.sets = total
    summary.cards = len(cards)
    return summary


def update_catalog_ja(
    conn: sqlite3.Connection,
    fetch_json: Callable[[str], object] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> CatalogUpdateSummary:
    """Pull the Japanese card catalog from tcgcsv (category 85) into the catalog.

    Fetches the group (set) index, then one products file per group. Products
    without a collector Number (sealed boxes/packs) are skipped so this stays a
    card catalog. Only JP rows are replaced; a group that fails is recorded and
    skipped.
    """
    fetch = fetch_json or _fetch_tcgcsv
    summary = CatalogUpdateSummary()

    groups = [
        g
        for g in _results(fetch(f"{_TCGCSV}/{_CATEGORY_JP}/groups"))
        if isinstance(g, dict) and g.get("groupId") is not None
    ]
    total = len(groups)

    cards: list[CatalogCard] = []
    for i, grp in enumerate(groups):
        gid = grp["groupId"]
        try:
            products = _results(fetch(f"{_TCGCSV}/{_CATEGORY_JP}/{gid}/products"))
        except Exception as exc:
            summary.errors.append(f"{gid}: {exc}")
            continue
        set_name = grp.get("name")
        set_code = grp.get("abbreviation") or None
        for p in products:
            pid = p.get("productId")
            if pid is None:
                continue
            ext = {e.get("name"): e.get("value") for e in (p.get("extendedData") or [])}
            number = ext.get("Number")
            # A card either has a collector Number or (for vintage sets tcgcsv
            # doesn't number) carries per-card mechanic fields. Products with
            # neither are sealed (boxes/packs/decks) → skip.
            if not number and not any(ext.get(k) for k in _JP_CARD_FIELDS):
                continue
            # Drop the embedded " - <number>" segment (see _NUMBER_IN_NAME) so
            # the stored name is clean like the English catalog; a trailing
            # variant label such as "(Mirror Holo)" is kept.
            name = " ".join(_NUMBER_IN_NAME.sub("", p.get("name") or "").split())
            cards.append(
                CatalogCard(
                    card_id=f"jp-{pid}",  # namespaced so it can't clash with EN ids
                    name=name,
                    set_id=str(gid),
                    set_name=set_name,
                    set_code=set_code,
                    number=number,
                    rarity=ext.get("Rarity"),
                    image_small=p.get("imageUrl") or None,
                    language="JP",
                )
            )
        if on_progress is not None:
            on_progress(i + 1, total)

    try:
        CardCatalogRepository(conn).replace_for_language(cards, "JP", now_iso())
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    summary.sets = total
    summary.cards = len(cards)
    return summary


def _as_list(data) -> list:
    if isinstance(data, dict):  # the raw files are top-level arrays, but be safe
        return data.get("data") or []
    return data or []
