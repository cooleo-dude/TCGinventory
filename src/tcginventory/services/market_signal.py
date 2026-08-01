"""Market-signal analytics (spec 06, M-Market) — a card's monetary significance.

Complements the price **trend** (direction of the market price, see
``services/trends.py``) with three orthogonal, resale-focused signals derived
from the **ledger** + per-item **price snapshots** — no vision, no external
feeds:

* **Liquidity** — how readily a card converts to cash: sell-through
  (units sold / acquired) and the quantity-weighted average days a sold unit sat
  before selling.
* **Markup** — realized margin on sales (proceeds vs. cost basis) and the
  unrealized markup of on-hand stock (current market vs. basis).
* **Volatility** — dispersion of the card's recorded market price over a window
  (coefficient of variation), a risk proxy.

Resale intent only (personal/ripping excluded). Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, pstdev

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import ItemRepository, PriceSnapshotRepository


@dataclass
class MarketSignal:
    item_id: int
    name: str
    set_name: str | None
    card_number: str | None
    units_acquired: int
    units_sold: int
    units_on_hand: int
    price_cents: int | None
    sell_through: float | None  # 0..1 (units sold / acquired)
    avg_days_to_sell: float | None  # quantity-weighted
    realized_margin_pct: float | None  # realized P/L over cost of goods sold
    unrealized_markup_pct: float | None  # market vs basis of on-hand stock
    volatility_pct: float | None  # price coefficient of variation over the window


@dataclass
class MarketOverview:
    tracked: int
    total_acquired: int
    total_sold: int
    overall_sell_through: float | None
    avg_days_to_sell: float | None
    median_volatility_pct: float | None


def _window_start(window_days: int | None, as_of: str | None) -> str | None:
    if window_days is None:
        return None
    ref = as_of or now_iso()
    try:
        ref_dt = datetime.fromisoformat(ref)
    except ValueError:
        ref_dt = datetime.fromisoformat(ref[:10])
    return (ref_dt - timedelta(days=window_days)).isoformat()


def _coefficient_of_variation(prices: list[int]) -> float | None:
    if len(prices) < 2:
        return None
    avg = mean(prices)
    return pstdev(prices) / avg * 100.0 if avg else None


class MarketSignalService:
    def __init__(self, conn, snapshots: PriceSnapshotRepository | None = None) -> None:
        self.conn = conn
        self._snapshots = snapshots or PriceSnapshotRepository(conn)

    def _acquired(self) -> dict[int, dict]:
        rows = self.conn.execute(
            "SELECT item_id, SUM(quantity) AS acq, SUM(remaining_qty) AS onhand, "
            "SUM(quantity * unit_cost_cents) AS acq_cost, "
            "SUM(remaining_qty * unit_cost_cents) AS onhand_cost "
            "FROM lot WHERE intent = 'resale' GROUP BY item_id"
        ).fetchall()
        return {r["item_id"]: r for r in rows}

    def _sold(self) -> dict[int, dict]:
        rows = self.conn.execute(
            "SELECT tl.item_id AS item_id, SUM(lc.quantity) AS units, "
            "SUM(tl.unit_price_cents * lc.quantity) AS proceeds, "
            "SUM((tl.unit_price_cents - lc.unit_cost_cents) * lc.quantity) "
            "  AS realized, "
            "SUM(lc.unit_cost_cents * lc.quantity) AS sold_cost, "
            "SUM((julianday(t.occurred_at) - julianday(l.acquired_at)) * lc.quantity) "
            "  AS days_weighted "
            "FROM lot_consumption lc "
            "JOIN transaction_line tl ON tl.id = lc.transaction_line_id "
            'JOIN "transaction" t ON t.id = tl.transaction_id '
            "JOIN lot l ON l.id = lc.lot_id "
            "WHERE t.status = 'posted' AND t.type = 'sale' AND tl.direction = 'out' "
            "AND l.intent = 'resale' GROUP BY tl.item_id"
        ).fetchall()
        return {r["item_id"]: r for r in rows}

    def signals(
        self,
        *,
        window_days: int | None = 90,
        as_of: str | None = None,
        limit: int | None = None,
    ) -> list[MarketSignal]:
        """One :class:`MarketSignal` per resale card that has been acquired.

        Liquidity/markup are all-time; volatility is over ``window_days``.
        Sorted most-liquid first (highest sell-through, then most sold)."""
        acquired = self._acquired()
        sold = self._sold()
        items = {i.id: i for i in ItemRepository(self.conn).list_all()}
        since = _window_start(window_days, as_of)
        price_series = self._snapshots.prices_by_item_since(since)

        out: list[MarketSignal] = []
        for item_id, acq in acquired.items():
            item = items.get(item_id)
            if item is None:
                continue
            units_acq = acq["acq"] or 0
            onhand = acq["onhand"] or 0
            s = sold.get(item_id)
            units_sold = (s["units"] if s else 0) or 0

            sell_through = units_sold / units_acq if units_acq else None
            avg_days = (
                s["days_weighted"] / s["units"]
                if s and s["units"]
                else None
            )
            realized_pct = (
                s["realized"] / s["sold_cost"] * 100.0
                if s and s["sold_cost"]
                else None
            )
            price = item.market_value_cents
            onhand_avg_cost = (
                acq["onhand_cost"] / onhand if onhand else None
            )
            unrealized_pct = (
                (price - onhand_avg_cost) / onhand_avg_cost * 100.0
                if price is not None and onhand_avg_cost
                else None
            )
            prices = price_series.get(item_id, [])
            out.append(
                MarketSignal(
                    item_id=item_id,
                    name=item.name,
                    set_name=item.set_name,
                    card_number=item.card_number,
                    units_acquired=units_acq,
                    units_sold=units_sold,
                    units_on_hand=onhand,
                    price_cents=price,
                    sell_through=sell_through,
                    avg_days_to_sell=avg_days,
                    realized_margin_pct=realized_pct,
                    unrealized_markup_pct=unrealized_pct,
                    volatility_pct=_coefficient_of_variation(prices),
                )
            )

        out.sort(
            key=lambda m: (m.sell_through or 0.0, m.units_sold), reverse=True
        )
        return out[:limit] if limit is not None else out

    def overview(self, signals: list[MarketSignal] | None = None) -> MarketOverview:
        rows = signals if signals is not None else self.signals()
        total_acq = sum(m.units_acquired for m in rows)
        total_sold = sum(m.units_sold for m in rows)
        days = [
            (m.avg_days_to_sell, m.units_sold)
            for m in rows
            if m.avg_days_to_sell is not None and m.units_sold
        ]
        weighted_days = (
            sum(d * q for d, q in days) / sum(q for _, q in days) if days else None
        )
        vols = sorted(m.volatility_pct for m in rows if m.volatility_pct is not None)
        median_vol = _median(vols)
        return MarketOverview(
            tracked=len(rows),
            total_acquired=total_acq,
            total_sold=total_sold,
            overall_sell_through=total_sold / total_acq if total_acq else None,
            avg_days_to_sell=weighted_days,
            median_volatility_pct=median_vol,
        )


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2
