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

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import CardCatalogRepository
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
    "HP", "CardType", "Stage", "Attack 1", "Attack 2", "Weakness",
    "Resistance", "Retreat Cost",
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
    errors: list[str] = field(default_factory=list)


def _tcgdex_number(local_id: str | None) -> str | None:
    """TCGdex ``localId`` → our collector-number convention. Numeric ids are
    zero-padded there ("001"); strip that to match the historical "1" form.
    Lettered ids ("SV074", "TG01") are kept as-is."""
    text = (local_id or "").strip()
    if not text:
        return None
    return str(int(text)) if text.isdigit() else text


def _tcgdex_image(image: str | None) -> str | None:
    """TCGdex serves an *asset base* URL; the concrete file needs a quality +
    extension. Use a small-ish PNG comparable to pokemontcg.io's ``small``."""
    return f"{image}/low.png" if image else None


def _norm_join(text: str | None) -> str:
    """Loose key for joining the two sources by identity (case/punctuation-free)."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _github_image_index(fetch) -> dict[tuple[str, str, str], str]:
    """``(name, set_name, number) → image_small`` from the pokemontcg.io dump —
    used to fill images TCGdex omits (it lacks art for most Trainer Gallery /
    Shiny Vault / Galarian Gallery subset cards, which pokemontcg.io carries).
    Identities line up: same names, set names, and (prefixed) numbers."""
    sets = _as_list(fetch(f"{_RAW}/sets/en.json"))
    set_names = {
        s["id"]: s.get("name") for s in sets if isinstance(s, dict) and s.get("id")
    }
    index: dict[tuple[str, str, str], str] = {}
    for set_id, set_name in set_names.items():
        try:
            entries = _as_list(fetch(f"{_RAW}/cards/en/{set_id}.json"))
        except Exception:
            continue
        for c in entries:
            image = (c.get("images") or {}).get("small")
            if not image:
                continue
            key = (
                _norm_join(c.get("name")),
                _norm_join(set_name),
                _norm_join(c.get("number")),
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
            (_norm_join(card.name), _norm_join(card.set_name), _norm_join(card.number))
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


def update_catalog(
    conn: sqlite3.Connection,
    fetch_json: Callable[[str], object] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
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
    total = len([s for s in sets if isinstance(s, dict) and s.get("id")])

    cards: list[CatalogCard] = []
    done = 0
    for s in sets:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        set_id = s["id"]
        try:
            detail = fetch(f"{_TCGDEX}/sets/{set_id}")
        except Exception as exc:
            summary.errors.append(f"{set_id}: {exc}")
            done += 1
            if on_progress is not None:
                on_progress(done, total)
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
                    number=_tcgdex_number(c.get("localId")),
                    rarity=None,
                    supertype=None,
                    image_small=_tcgdex_image(c.get("image")),
                    printed_total=printed_total,
                )
            )
        done += 1
        if on_progress is not None:
            on_progress(done, total)

    if not cards:
        summary.errors.append("no cards fetched — catalog left unchanged")
        return summary
    _fill_missing_images(cards, fetch)
    try:
        CardCatalogRepository(conn).replace_for_language(cards, "EN", now_iso())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    summary.sets = total
    summary.cards = len(cards)
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
    for i, (set_id, (set_name, set_code, printed_total)) in enumerate(
        set_meta.items()
    ):
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
            ext = {
                e.get("name"): e.get("value") for e in (p.get("extendedData") or [])
            }
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
