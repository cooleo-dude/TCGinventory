"""Catalog-wide market-price sweep (Qt-free).

Walks every set in the catalog, pulls its tcgcsv price file, and records a
snapshot per card into the separate price-history DB (dedup-on-change). Sets
containing owned cards are priced first, then the rest least-recently-priced
first, so an interrupted run still favours what matters. Old history is pruned
to a rolling window. Network I/O is injected via the provider for offline tests.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from tcginventory.data.pricehistory import PriceHistoryRepository
from tcginventory.services.pricing import TcgcsvProvider, _norm_number

_CATEGORY_EN = 3
_CATEGORY_JP = 85
_RECORD_DAYS = 365  # live retention window


@dataclass
class _Set:
    set_key: str  # "EN:sv9" / "JP:24711"
    language: str
    set_id: str
    set_name: str | None
    set_code: str | None


@dataclass
class SweepSummary:
    sets: int = 0
    snapshots: int = 0  # history rows written (price changes)
    priced: int = 0  # cards that resolved a price this run
    errors: list[str] = field(default_factory=list)


def _catalog_sets(main_conn: sqlite3.Connection) -> list[_Set]:
    rows = main_conn.execute(
        "SELECT DISTINCT language, set_id, set_name, set_code FROM card_catalog"
    ).fetchall()
    return [
        _Set(f"{r['language']}:{r['set_id']}", r["language"], r["set_id"],
             r["set_name"], r["set_code"])
        for r in rows
    ]


def _owned_set_keys(main_conn: sqlite3.Connection) -> set[str]:
    rows = main_conn.execute(
        "SELECT DISTINCT cc.language || ':' || cc.set_id AS set_key "
        "FROM card_catalog cc "
        "WHERE EXISTS (SELECT 1 FROM item i "
        "             WHERE i.set_name = cc.set_name AND i.language = cc.language)"
    ).fetchall()
    return {r["set_key"] for r in rows}


def sweep(
    main_conn: sqlite3.Connection,
    history_conn: sqlite3.Connection,
    provider: TcgcsvProvider | None = None,
    *,
    as_of: str | None = None,
    max_sets: int | None = None,
) -> SweepSummary:
    """Price the catalog into the history DB. ``max_sets`` caps how many sets a
    single run processes (owned + least-recent first); None = the whole catalog."""
    provider = provider or TcgcsvProvider()
    hist = PriceHistoryRepository(history_conn)
    as_of = as_of or date.today().isoformat()
    summary = SweepSummary()

    sets = _catalog_sets(main_conn)
    owned = _owned_set_keys(main_conn)
    last_priced = hist.set_last_priced()
    # Owned sets first, then least-recently-priced first (blank = never).
    sets.sort(key=lambda s: (s.set_key not in owned, last_priced.get(s.set_key, "")))
    if max_sets is not None:
        sets = sets[:max_sets]

    for s in sets:
        try:
            priced, written = _process_set(main_conn, hist, provider, s, as_of)
            summary.priced += priced
            summary.snapshots += written
            summary.sets += 1
            hist.touch_set(s.set_key, as_of)
            history_conn.commit()
        except Exception as exc:  # one bad set never aborts the sweep
            history_conn.rollback()
            summary.errors.append(f"{s.set_key}: {exc}")

    before = (date.today() - timedelta(days=_RECORD_DAYS)).isoformat()
    hist.prune(before)
    history_conn.commit()
    return summary


def _process_set(
    main_conn: sqlite3.Connection,
    hist: PriceHistoryRepository,
    provider: TcgcsvProvider,
    s: _Set,
    as_of: str,
) -> tuple[int, int]:
    """Returns ``(cards_priced, snapshots_written)`` for the set."""
    cards = main_conn.execute(
        "SELECT card_id, number FROM card_catalog WHERE language = ? AND set_id = ?",
        (s.language, s.set_id),
    ).fetchall()
    if not cards:
        return 0, 0

    if s.language == "JP":
        category = _CATEGORY_JP
        group_id = int(s.set_id)  # JP set_id IS the tcgcsv groupId
        prices = provider.market_prices(category, group_id)

        def price_of(card):
            # card_id is "jp-<productId>"
            try:
                pid = int(card["card_id"].split("-", 1)[1])
            except (IndexError, ValueError):
                return None
            return prices.get(pid)
    else:
        category = _CATEGORY_EN
        group_id = provider.resolve_group(s.set_name, s.set_code, category)
        if group_id is None:
            return 0, 0
        prices = provider.market_prices(category, group_id)
        num_to_pid = {
            _norm_number(num): pid
            for pid, num, _url, _name in provider.products(category, group_id)
        }

        def price_of(card):
            pid = num_to_pid.get(_norm_number(card["number"]))
            return prices.get(pid) if pid is not None else None

    priced = written = 0
    for card in cards:
        cents = price_of(card)
        if cents is None:
            continue
        priced += 1
        if hist.record(card["card_id"], cents, as_of):
            written += 1
    return priced, written
