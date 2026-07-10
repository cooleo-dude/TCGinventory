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

import sqlite3
from dataclasses import dataclass

from tcginventory.domain.enums import Intent

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


class ReportingService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- point-in-time totals ----------------------------------------------

    def net_cash_cents(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cash_delta_cents), 0) AS v "
            "FROM \"transaction\" WHERE status = 'posted'"
        ).fetchone()
        return row["v"]

    def realized_pl_cents(self, intent: Intent | None = None) -> int:
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
            params = (intent.value,)
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
        """Current market value of holdings; ripping lots count as $0."""
        sql = (
            "SELECT COALESCE(SUM(CASE WHEN l.intent = 'ripping' THEN 0 "
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
        """Total agreed value of the cards given up (the margin's denominator)."""
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
