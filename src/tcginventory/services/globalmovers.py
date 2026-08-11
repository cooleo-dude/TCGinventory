"""Catalog-wide Top Gainers / Top Losers — the market, not the shelf (Qt-free).

``ReportingService.price_movers`` answers "which of *my* cards moved", and it can only
ever answer that: its query opens ``FROM lot l JOIN item i``, so the Dashboard's
highlights strip is a restatement of Inventory. This is the other scope — which cards
moved in the catalog, whether or not a lot has ever held one.

Three things make it a separate service rather than another method on
``ReportingService``:

* **It reads a derived store.** The price history lives in ``pricehistory.db``, and
  ``ReportingService`` holds the *ledger* connection. Reaching one from the other is
  the blur ``LEDGER-VS-DERIVED`` exists to prevent. ``TrendService`` already takes
  both connections separately, and this follows it.
* **The ranking is in a different key space.** Prices are keyed by catalog
  ``card_id``; the ledger is keyed by ``item.id``, and there is no foreign key between
  them — only a resolution by printed identity.
* **Scale.** 759 held cards became 49,709 candidates, so the work is ordered to keep
  the expensive part in one place: rank inside ``pricehistory.db`` first, then hydrate
  names for the handful of rows that survive.

That ordering is the whole performance story. Joining prices to the catalog across two
database files to get names would touch every candidate; ranking first means the
catalog is asked about ~10 cards instead of ~18,000.
"""

from __future__ import annotations

import sqlite3

from tcginventory.data.pricehistory import PriceHistoryRepository
from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.services.reporting import MoverRow

# Defaults measured against the real 49,709-card history rather than chosen. See
# `PriceHistoryRepository.movers_since` for what each one caught; the short version is
# that without them the top of the ranking was a card that had gone from 26 cents to
# $1283.67 because the sweep changed which product it was pricing.
FLOOR_CENTS = 100
MIN_POINTS = 2
MAX_STEP_RATIO = 5.0


class GlobalMoversService:
    """Ranks the catalog by price movement and marks what you happen to hold.

    Takes the two connections explicitly — the price history and the ledger — because
    it genuinely needs both and hiding that would make the thread rule easy to break:
    whoever constructs this on a worker must give it that worker's own connections
    (`SQLITE-THREAD-AFFINITY`).
    """

    def __init__(
        self,
        history_conn: sqlite3.Connection,
        ledger_conn: sqlite3.Connection,
        *,
        floor_cents: int = FLOOR_CENTS,
        min_points: int = MIN_POINTS,
        max_step_ratio: float = MAX_STEP_RATIO,
    ) -> None:
        self._history = PriceHistoryRepository(history_conn)
        self._catalog = CardCatalogRepository(ledger_conn)
        self._ledger = ledger_conn
        self._floor_cents = floor_cents
        self._min_points = min_points
        self._max_step_ratio = max_step_ratio

    def movers(
        self, since: str | None = None, limit: int | None = 5
    ) -> tuple[list[MoverRow], list[MoverRow]]:
        """``(gainers, losers)`` across the catalog, biggest percent move first.

        ``since`` is the same cutoff the Dashboard's shared range selector produces;
        an ISO timestamp is accepted and truncated, because the history is date-only.
        """
        cutoff = since[:10] if since else None
        ranked = self._history.movers_since(
            cutoff,
            floor_cents=self._floor_cents,
            min_points=self._min_points,
            max_step_ratio=self._max_step_ratio,
            limit=limit,
        )
        if not ranked:
            return [], []

        held = self._held_by_card_id()
        gainers: list[MoverRow] = []
        losers: list[MoverRow] = []
        for card_id, price_now, change, pct in ranked:
            row = self._hydrate(card_id, price_now, change, pct, held)
            (gainers if change > 0 else losers).append(row)
        # `movers_since` sorts by percent descending and returns both ends, so the
        # gainers are already biggest-first and the losers biggest-fall-last.
        losers.reverse()
        return gainers, losers

    def _hydrate(
        self,
        card_id: str,
        price_now: int,
        change: int,
        pct: float,
        held: dict[str, tuple[int, int]],
    ) -> MoverRow:
        """One ranked card into a `MoverRow`, named from the catalog.

        A card the catalog cannot name still gets a row: the price movement is real
        and dropping it would silently shorten the list. `card_id` is a poor label but
        an honest one, and it is what the sweep recorded.
        """
        card = self._catalog.get(card_id)
        item_id, owned = held.get(card_id, (None, 0))
        return MoverRow(
            item_id=item_id,
            name=card.name if card else card_id,
            set_name=card.set_name if card else None,
            card_number=self._catalog.display_number(card) if card else None,
            qty=owned,
            price_cents=price_now,
            change_cents=change,
            change_pct=pct,
            cost_cents=0,  # no basis for a card you do not hold, and unused here
            card_id=card_id,
            owned_qty=owned,
        )

    def _held_by_card_id(self) -> dict[str, tuple[int, int]]:
        """``{card_id: (item_id, qty)}`` for everything currently held.

        Built from the **held** side, which is the cheap direction: ~759 items resolve
        to catalog ids, and the ranked slice then looks up in O(1). Going the other way
        — asking, per ranked card, whether any item matches its printed identity —
        would run the fuzzy `image_for` match once per candidate.

        Intent is deliberately *not* filtered here. The owned scope excludes personal
        and ripping lots because it is a resale view; this marker answers the simpler
        question "is this card in my collection at all", and a personal copy still
        makes a global row worth noticing.
        """
        rows = self._ledger.execute(
            "SELECT i.id AS id, i.name AS name, i.set_name AS set_name, "
            "i.card_number AS card_number, SUM(l.remaining_qty) AS qty "
            "FROM lot l JOIN item i ON i.id = l.item_id "
            "WHERE l.remaining_qty > 0 GROUP BY i.id"
        ).fetchall()
        out: dict[str, tuple[int, int]] = {}
        for r in rows:
            card_id = self._catalog.card_id_for(
                r["name"], r["set_name"], r["card_number"]
            )
            if not card_id:
                continue  # printed identity the catalog cannot place; not markable
            item_id, qty = out.get(card_id, (r["id"], 0))
            # Two items can resolve to one catalog card — a raw and a graded copy, or
            # a duplicate entry. Sum the units and keep the first item for navigation.
            out[card_id] = (item_id, qty + (r["qty"] or 0))
        return out
