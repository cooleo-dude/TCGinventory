"""Local reference card catalog.

* **English** comes from the open pokemontcg.io dataset
  (github.com/PokemonTCG/pokemon-tcg-data) — an index of sets plus one JSON file
  of cards per set.
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
_TCGCSV = "https://tcgcsv.com/tcgplayer"
_CATEGORY_JP = 85  # tcgcsv "Pokemon Japan"


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


def update_catalog(
    conn: sqlite3.Connection,
    fetch_json: Callable[[str], object] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> CatalogUpdateSummary:
    """Pull the full English card dataset into ``card_catalog``. Atomic.

    Fetches the sets index, then each set's card file, and replaces the catalog
    wholesale. A single set that fails to fetch is recorded and skipped.
    """
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
            if not number:
                continue  # sealed product / no collector number → not a card
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
