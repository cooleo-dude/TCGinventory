"""Local reference card catalog, pulled from the open pokemontcg.io dataset.

The dataset (github.com/PokemonTCG/pokemon-tcg-data) publishes an index of sets
plus one JSON file of cards per set — bulk, versioned, no API key or rate
limits. We pull it into the ``card_catalog`` table so name autocomplete and
set/number auto-fill work offline; "update" just re-pulls. Qt-free; network I/O
is injected so it's testable offline.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.domain.models import CatalogCard

_RAW = "https://raw.githubusercontent.com/PokemonTCG/pokemon-tcg-data/master"


def _fetch_json(url: str):  # pragma: no cover - real network
    import httpx

    resp = httpx.get(url, timeout=30.0, headers={"User-Agent": "TCGinventory/1.0"})
    resp.raise_for_status()
    return resp.json()


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
        CardCatalogRepository(conn).replace_all(cards, now_iso())
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
