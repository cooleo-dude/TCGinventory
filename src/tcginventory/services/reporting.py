"""ReportingService — realized/unrealized P/L, margin, and dashboard series.

All figures come straight from the ledger and only count **posted** transactions
(voided ones are excluded). Where meaningful, results can be filtered by intent
via the consumed/held lot's ``intent``.

Series implemented here are the two that are exact and event-based:
- **money-movement** — cumulative net cash over time.
- **realized-profit** — cumulative realized P/L over time.

The historical **collection-value** series (market value of holdings over time)
is deferred to M3: it needs a market-value history the V1 schema does not record.
``collection_value_cents`` gives the current point-in-time figure.
"""

from __future__ import annotations

import bisect
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from tcginventory.data.db import now_iso
from tcginventory.domain.enums import Intent

# Dashboard intent filter → the key stored in collection_value_snapshot.
_INTENT_KEYS: list[tuple[str, Intent | None]] = [
    ("all", None),
    ("personal", Intent.PERSONAL),
    ("resale", Intent.RESALE),
    ("ripping", Intent.RIPPING),
]

# Per-consumption realized P/L = proceeds share − cost drawn from the lot.
_REALIZED_EXPR = "tl.unit_price_cents * lc.quantity - lc.unit_cost_cents * lc.quantity"


@dataclass
class SeriesPoint:
    at: str  # ISO datetime of the transaction
    value_cents: int  # cumulative value at/after this event


@dataclass
class PortfolioSummary:
    cost_basis_cents: int
    market_value_cents: int
    unrealized_pl_cents: int
    realized_pl_cents: int
    net_cash_cents: int


@dataclass
class MoverRow:
    """One card in the Top Gainers / Top Losers list.

    Ranking is by **market-price movement** over the selected window — the change
    in the card's recorded market price, NOT unrealized P/L against cost."""

    item_id: int
    name: str
    set_name: str | None
    card_number: str | None
    qty: int  # units currently held
    price_cents: int  # current market price (per unit)
    change_cents: int  # market-price change over the window (per unit)
    change_pct: float | None  # vs the window-start price (None when unknown)


@dataclass
class SellerRow:
    """One card in the Top Sellers list (units sold over a window)."""

    item_id: int
    name: str
    set_name: str | None
    card_number: str | None
    units: int  # total quantity sold in the window
    sales: int  # distinct sale transactions
    revenue_cents: int  # proceeds
    realized_cents: int  # realized P/L


class ReportingService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- point-in-time totals ----------------------------------------------

    def net_cash_cents(self, since: str | None = None) -> int:
        """Net cash across posted transactions; ``since`` limits to a window."""
        sql = (
            "SELECT COALESCE(SUM(cash_delta_cents), 0) AS v "
            "FROM \"transaction\" WHERE status = 'posted'"
        )
        params: tuple = ()
        if since is not None:
            sql += " AND occurred_at >= ?"
            params = (since,)
        return self.conn.execute(sql, params).fetchone()["v"]

    def realized_pl_cents(
        self, intent: Intent | None = None, since: str | None = None
    ) -> int:
        """Realized P/L on disposals; ``since`` limits to sales within a window."""
        sql = (
            f"SELECT COALESCE(SUM({_REALIZED_EXPR}), 0) AS v "
            "FROM lot_consumption lc "
            "JOIN transaction_line tl ON tl.id = lc.transaction_line_id "
            'JOIN "transaction" t ON t.id = tl.transaction_id '
            "JOIN lot l ON l.id = lc.lot_id "
            "WHERE t.status = 'posted' AND tl.direction = 'out'"
        )
        params: tuple = ()
        if intent is not None:
            sql += " AND l.intent = ?"
            params += (intent.value,)
        if since is not None:
            sql += " AND t.occurred_at >= ?"
            params += (since,)
        return self.conn.execute(sql, params).fetchone()["v"]

    def cost_basis_cents(self, intent: Intent | None = None) -> int:
        sql = (
            "SELECT COALESCE(SUM(l.remaining_qty * l.unit_cost_cents), 0) AS v "
            "FROM lot l WHERE l.remaining_qty > 0"
        )
        params: tuple = ()
        if intent is not None:
            sql += " AND l.intent = ?"
            params = (intent.value,)
        return self.conn.execute(sql, params).fetchone()["v"]

    def collection_value_cents(self, intent: Intent | None = None) -> int:
        """Current market value of holdings; ripping and missing lots count as $0.

        Cost basis still includes missing lots, so their unrealized P/L is a loss
        equal to their cost basis (impairment write-down)."""
        sql = (
            "SELECT COALESCE(SUM(CASE WHEN l.intent = 'ripping' OR l.missing = 1 "
            "THEN 0 "
            "ELSE l.remaining_qty * COALESCE(i.market_value_cents, 0) END), 0) AS v "
            "FROM lot l JOIN item i ON i.id = l.item_id "
            "WHERE l.remaining_qty > 0"
        )
        params: tuple = ()
        if intent is not None:
            sql += " AND l.intent = ?"
            params = (intent.value,)
        return self.conn.execute(sql, params).fetchone()["v"]

    def unrealized_pl_cents(self, intent: Intent | None = None) -> int:
        return self.collection_value_cents(intent) - self.cost_basis_cents(intent)

    def total_return_pct(self, intent: Intent | None = None) -> float | None:
        """ROI = (realized + unrealized P/L) ÷ cost basis, as a percent.

        ``None`` when there is no cost basis (nothing held to divide by)."""
        cost = self.cost_basis_cents(intent)
        if cost == 0:
            return None
        total = self.realized_pl_cents(intent) + self.unrealized_pl_cents(intent)
        return 100.0 * total / cost

    def portfolio_summary(self, intent: Intent | None = None) -> PortfolioSummary:
        cost = self.cost_basis_cents(intent)
        market = self.collection_value_cents(intent)
        return PortfolioSummary(
            cost_basis_cents=cost,
            market_value_cents=market,
            unrealized_pl_cents=market - cost,
            realized_pl_cents=self.realized_pl_cents(intent),
            net_cash_cents=self.net_cash_cents(),
        )

    # -- top movers / sellers (dashboard highlights) -----------------------

    def _price_movers(self, intent: Intent | None, since: str | None) -> list[MoverRow]:
        """Held resale cards ranked by **market-price movement** over the window.

        For each held card we compare its current market price to its recorded
        price at the window start (the latest ``price_snapshot`` on/before
        ``since``; for All Time, its earliest snapshot). Cards without a baseline
        snapshot can't have their movement measured and are skipped.

        Excludes **personal**/**ripping** lots (not a resale-business view),
        **review** items and **graded** slabs (their market value isn't tracked
        consistently — see specs/03-features.md, F-VW-11)."""
        sql = (
            "SELECT i.id AS id, i.name AS name, i.set_name AS set_name, "
            "i.card_number AS card_number, i.market_value_cents AS price_now, "
            "SUM(l.remaining_qty) AS qty "
            "FROM lot l JOIN item i ON i.id = l.item_id "
            "WHERE l.remaining_qty > 0 AND l.intent NOT IN ('personal', 'ripping') "
            "AND COALESCE(i.review, 0) = 0 AND COALESCE(i.is_graded, 0) = 0 "
            "AND i.market_value_cents IS NOT NULL"
        )
        params: tuple = ()
        if intent is not None:
            sql += " AND l.intent = ?"
            params = (intent.value,)
        sql += " GROUP BY i.id"
        holdings = self.conn.execute(sql, params).fetchall()
        if not holdings:
            return []

        baseline = self._baseline_prices(since)
        out: list[MoverRow] = []
        for r in holdings:
            base = baseline.get(r["id"])
            if base is None:
                continue  # no price history to measure movement against
            change = r["price_now"] - base
            if change == 0:
                continue
            pct = (100.0 * change / base) if base else None
            out.append(
                MoverRow(
                    item_id=r["id"],
                    name=r["name"],
                    set_name=r["set_name"],
                    card_number=r["card_number"],
                    qty=r["qty"],
                    price_cents=r["price_now"],
                    change_cents=change,
                    change_pct=pct,
                )
            )
        return out

    def _earliest_prices(self) -> dict[int, int]:
        """Each item's earliest recorded market-price snapshot."""
        rows = self.conn.execute(
            "SELECT ps.item_id AS item_id, ps.price_cents AS price "
            "FROM price_snapshot ps JOIN ("
            "  SELECT item_id, MIN(as_of) AS m FROM price_snapshot GROUP BY item_id"
            ") t ON t.item_id = ps.item_id AND t.m = ps.as_of"
        ).fetchall()
        return {r["item_id"]: r["price"] for r in rows}

    def _baseline_prices(self, since: str | None) -> dict[int, int]:
        """Per-item baseline market price for the window.

        ``since`` None (All Time) → each item's earliest recorded snapshot.
        ``since`` set → the latest snapshot on/before it (the window-start price);
        for items with **no** snapshot that far back, fall back to their earliest
        snapshot so a card that started being tracked mid-window still shows its
        movement (otherwise a short price history would leave the movers empty)."""
        if since is None:
            return self._earliest_prices()
        rows = self.conn.execute(
            "SELECT ps.item_id AS item_id, ps.price_cents AS price "
            "FROM price_snapshot ps JOIN ("
            "  SELECT item_id, MAX(as_of) AS m FROM price_snapshot "
            "  WHERE as_of <= ? GROUP BY item_id"
            ") t ON t.item_id = ps.item_id AND t.m = ps.as_of",
            (since,),
        ).fetchall()
        baseline = {r["item_id"]: r["price"] for r in rows}
        for item_id, price in self._earliest_prices().items():
            baseline.setdefault(item_id, price)
        return baseline

    def top_gainers(
        self,
        intent: Intent | None = None,
        limit: int | None = 5,
        since: str | None = None,
    ) -> list[MoverRow]:
        """Held cards whose market price rose the most over the window."""
        rows = [m for m in self._price_movers(intent, since) if m.change_cents > 0]
        rows.sort(key=lambda m: m.change_cents, reverse=True)
        return rows[:limit]  # limit None → all

    def top_losers(
        self,
        intent: Intent | None = None,
        limit: int | None = 5,
        since: str | None = None,
    ) -> list[MoverRow]:
        """Held cards whose market price fell the most over the window."""
        rows = [m for m in self._price_movers(intent, since) if m.change_cents < 0]
        rows.sort(key=lambda m: m.change_cents)
        return rows[:limit]  # limit None → all

    def price_movers(
        self,
        intent: Intent | None = None,
        limit: int | None = 5,
        since: str | None = None,
    ) -> tuple[list[MoverRow], list[MoverRow]]:
        """Top gainers **and** losers in one pass. The dashboard shows both, so
        this computes the holdings + baseline query once instead of the twice
        that separate ``top_gainers`` + ``top_losers`` calls would."""
        movers = self._price_movers(intent, since)
        gainers = sorted(
            (m for m in movers if m.change_cents > 0),
            key=lambda m: m.change_cents,
            reverse=True,
        )
        losers = sorted(
            (m for m in movers if m.change_cents < 0), key=lambda m: m.change_cents
        )
        return gainers[:limit], losers[:limit]

    def top_sellers(
        self,
        window_days: int | None = 90,
        limit: int | None = 5,
        as_of: str | None = None,
    ) -> list[SellerRow]:
        """Cards that sold the most units within the trailing window.

        Counts posted **sale** transactions only (not trades or adjustments),
        ordered by units sold then revenue. ``window_days=None`` = all time.
        ``as_of`` overrides "now" for tests.
        """
        params: list = []
        time_clause = ""
        if window_days is not None:
            ref = as_of or now_iso()
            try:
                ref_dt = datetime.fromisoformat(ref)
            except ValueError:
                ref_dt = datetime.fromisoformat(ref[:10])
            time_clause = "AND t.occurred_at >= ? "
            params.append((ref_dt - timedelta(days=window_days)).isoformat())
        params.append(limit if limit is not None else -1)  # SQLite LIMIT -1 = all
        # Aggregate over lot_consumption so we can gate on the *sold lot's* intent
        # (a business view: exclude personal/ripping stock and review items).
        rows = self.conn.execute(
            "SELECT i.id AS id, i.name AS name, i.set_name AS set_name, "
            "i.card_number AS card_number, "
            "SUM(lc.quantity) AS units, "
            "COUNT(DISTINCT t.id) AS sales, "
            "SUM(tl.unit_price_cents * lc.quantity) AS revenue, "
            f"COALESCE(SUM({_REALIZED_EXPR}), 0) AS realized "
            "FROM lot_consumption lc "
            "JOIN transaction_line tl ON tl.id = lc.transaction_line_id "
            'JOIN "transaction" t ON t.id = tl.transaction_id '
            "JOIN lot l ON l.id = lc.lot_id "
            "JOIN item i ON i.id = tl.item_id "
            "WHERE t.status = 'posted' AND tl.direction = 'out' "
            "AND t.type = 'sale' " + time_clause + "AND l.intent NOT IN "
            "('personal', 'ripping') "
            "AND COALESCE(i.review, 0) = 0 AND COALESCE(i.is_graded, 0) = 0 "
            "GROUP BY i.id ORDER BY units DESC, revenue DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [
            SellerRow(
                item_id=r["id"],
                name=r["name"],
                set_name=r["set_name"],
                card_number=r["card_number"],
                units=r["units"],
                sales=r["sales"],
                revenue_cents=r["revenue"],
                realized_cents=r["realized"],
            )
            for r in rows
        ]

    # -- margin (per transaction) ------------------------------------------

    def transaction_margin_cents(self, transaction_id: int) -> int:
        """Realized margin (profit) on the cards given up in a transaction."""
        row = self.conn.execute(
            f"SELECT COALESCE(SUM({_REALIZED_EXPR}), 0) AS v "
            "FROM lot_consumption lc "
            "JOIN transaction_line tl ON tl.id = lc.transaction_line_id "
            "WHERE tl.transaction_id = ? AND tl.direction = 'out'",
            (transaction_id,),
        ).fetchone()
        return row["v"]

    def transaction_proceeds_cents(self, transaction_id: int) -> int:
        """Total proceeds on the cards given up (the margin's denominator)."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(tl.unit_price_cents * tl.quantity), 0) AS v "
            "FROM transaction_line tl "
            "WHERE tl.transaction_id = ? AND tl.direction = 'out'",
            (transaction_id,),
        ).fetchone()
        return row["v"]

    def gross_margin_pct(self, transaction_id: int) -> float | None:
        """Gross-margin % = profit ÷ proceeds, or None when nothing was sold."""
        proceeds = self.transaction_proceeds_cents(transaction_id)
        if proceeds == 0:
            return None
        return 100.0 * self.transaction_margin_cents(transaction_id) / proceeds

    # -- dashboard series (exact, event-based) -----------------------------

    def cost_basis_series(self) -> list[SeriesPoint]:
        """Cumulative acquired cost over time (drives the Cost Basis sparkline)."""
        rows = self.conn.execute(
            "SELECT acquired_at, quantity * unit_cost_cents AS c "
            "FROM lot ORDER BY acquired_at, id"
        ).fetchall()
        running = 0
        out = []
        for r in rows:
            running += r["c"]
            out.append(SeriesPoint(at=r["acquired_at"], value_cents=running))
        return out

    def money_movement_series(self) -> list[SeriesPoint]:
        """Cumulative net cash across posted transactions, ordered by date."""
        rows = self.conn.execute(
            'SELECT occurred_at, cash_delta_cents FROM "transaction" '
            "WHERE status = 'posted' ORDER BY occurred_at, id"
        ).fetchall()
        running = 0
        out = []
        for r in rows:
            running += r["cash_delta_cents"]
            out.append(SeriesPoint(at=r["occurred_at"], value_cents=running))
        return out

    def realized_profit_series(self, intent: Intent | None = None) -> list[SeriesPoint]:
        """Cumulative realized P/L across posted transactions, ordered by date."""
        sql = (
            f"SELECT t.occurred_at AS at, t.id AS id, "
            f"COALESCE(SUM({_REALIZED_EXPR}), 0) AS realized "
            'FROM "transaction" t '
            "LEFT JOIN transaction_line tl "
            "  ON tl.transaction_id = t.id AND tl.direction = 'out' "
            "LEFT JOIN lot_consumption lc ON lc.transaction_line_id = tl.id "
        )
        params: tuple = ()
        if intent is not None:
            sql += "LEFT JOIN lot l ON l.id = lc.lot_id "
            where_intent = " AND (l.intent = ? OR l.intent IS NULL)"
            params = (intent.value,)
        else:
            where_intent = ""
        sql += (
            f"WHERE t.status = 'posted'{where_intent} "
            "GROUP BY t.id ORDER BY t.occurred_at, t.id"
        )
        running = 0
        out = []
        for r in self.conn.execute(sql, params).fetchall():
            running += r["realized"]
            out.append(SeriesPoint(at=r["at"], value_cents=running))
        return out

    def record_collection_value(self, as_of: str | None = None) -> None:
        """Record today's **effective** collection value (the tile figure) per
        intent, so the chart matches the tile and stays accurate going forward.

        Deduped per day (upsert on ``(as_of, intent)``); safe to call repeatedly
        (startup, after a price refresh). Commits its own row."""
        day = (as_of or now_iso())[:10]
        for key, intent in _INTENT_KEYS:
            self.conn.execute(
                "INSERT INTO collection_value_snapshot (as_of, intent, value_cents) "
                "VALUES (?,?,?) ON CONFLICT(as_of, intent) DO UPDATE SET "
                "value_cents = excluded.value_cents",
                (day, key, self.collection_value_cents(intent)),
            )
        self.conn.commit()

    def _recorded_collection_points(self, intent: Intent | None) -> list[SeriesPoint]:
        key = intent.value if intent is not None else "all"
        return [
            SeriesPoint(at=r["as_of"], value_cents=r["value_cents"])
            for r in self.conn.execute(
                "SELECT as_of, value_cents FROM collection_value_snapshot "
                "WHERE intent = ? ORDER BY as_of",
                (key,),
            ).fetchall()
        ]

    def collection_value_series(
        self, intent: Intent | None = None
    ) -> list[SeriesPoint]:
        """Collection value over time, ending exactly at the "Collection Value"
        tile.

        Two sources, spliced by date: **recorded** daily tile figures
        (:meth:`record_collection_value`) are authoritative from the day
        recording began; **before** that they are back-filled by reconstructing
        from history. The reconstruction values holdings as they stood at each
        date — using the recorded poll price for polled cards, and the card's
        **current** value (respecting reviewed/manual values) for reviewed or
        never-polled cards (graded slabs, manual entries) so nothing is dropped.
        Ripping/missing lots count as $0.
        """
        recorded = self._recorded_collection_points(intent)
        # Only reconstruct the prefix *before* recording began — points on/after
        # the first recorded figure are authoritative and would be discarded, so
        # there's no point sampling all the post-recording poll dates.
        before = recorded[0].at if recorded else None
        reconstructed = self._reconstruct_collection_series(intent, before=before)
        return reconstructed + recorded

    def _reconstruct_collection_series(
        self, intent: Intent | None = None, before: str | None = None
    ) -> list[SeriesPoint]:
        items: dict[int, tuple[bool, int]] = {}  # id -> (review, market_value)
        for r in self.conn.execute(
            "SELECT id, review, market_value_cents FROM item"
        ).fetchall():
            items[r["id"]] = (bool(r["review"]), r["market_value_cents"] or 0)

        lots = self.conn.execute(
            "SELECT id, item_id, acquired_at, quantity, intent, missing FROM lot"
        ).fetchall()

        disposals: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for r in self.conn.execute(
            "SELECT lc.lot_id AS lot_id, t.occurred_at AS at, lc.quantity AS qty "
            "FROM lot_consumption lc "
            "JOIN transaction_line tl ON tl.id = lc.transaction_line_id "
            'JOIN "transaction" t ON t.id = tl.transaction_id '
            "WHERE t.status = 'posted'"
        ).fetchall():
            disposals[r["lot_id"]].append((r["at"], r["qty"]))

        times: dict[int, list[str]] = defaultdict(list)
        cents: dict[int, list[int]] = defaultdict(list)
        for r in self.conn.execute(
            "SELECT item_id, as_of, price_cents FROM price_snapshot ORDER BY as_of"
        ).fetchall():
            times[r["item_id"]].append(r["as_of"])
            cents[r["item_id"]].append(r["price_cents"])

        def effective(item_id: int, when: str) -> int:
            review, market = items.get(item_id, (False, 0))
            # Reviewed and never-polled cards are user-maintained — value them at
            # their current figure (flat) rather than a poll we don't trust/have.
            if review or item_id not in times:
                return market
            ts = times[item_id]
            idx = bisect.bisect_right(ts, when) - 1
            return cents[item_id][idx] if idx >= 0 else market

        if not lots:
            return []
        # Sample at acquisitions + poll dates so the line spans the collection's
        # life; the final "now" point is the exact tile figure so the chart's
        # endpoint always equals the Collection Value tile.
        dates: set[str] = {lot["acquired_at"] for lot in lots}
        dates.update(
            r["as_of"]
            for r in self.conn.execute(
                "SELECT DISTINCT as_of FROM price_snapshot"
            ).fetchall()
        )
        if before is not None:  # recorded figures cover on/after `before`
            dates = {d for d in dates if d < before}

        out: list[SeriesPoint] = []
        for d in sorted(dates):
            total = 0
            for lot in lots:
                if lot["intent"] == "ripping" or lot["missing"]:
                    continue  # ripping and missing lots are always $0
                if intent is not None and lot["intent"] != intent.value:
                    continue
                if lot["acquired_at"] > d:
                    continue  # not yet acquired at this date
                held = lot["quantity"] - sum(
                    q for cd, q in disposals.get(lot["id"], ()) if cd <= d
                )
                if held > 0:
                    total += held * effective(lot["item_id"], d)
            out.append(SeriesPoint(at=d, value_cents=total))
        # Anchor the endpoint to the tile (same computation) so the two never
        # disagree at "now" — only when we're building the whole series (no
        # recorded figures follow to own the endpoint).
        if before is None:
            out.append(
                SeriesPoint(
                    at=now_iso(), value_cents=self.collection_value_cents(intent)
                )
            )
        return out
