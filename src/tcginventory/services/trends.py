"""Market-trend detection over a card's price history (Qt-free, pure).

The trend of a card is the **least-squares slope** of its recent daily price
series — a line fit through the last ``window_days`` of prices. A slope is robust
to the day-to-day noise a single "now vs N days ago" comparison would trip over.
The slope is then expressed as a **percent change over the window** (against the
current price) so the UI can pick a direction (up / down / flat) and a magnitude
(steeper move → more arrows).

Two layers:

* :func:`compute_trend` — the pure maths: ``[(date, cents), …]`` → :class:`Trend`.
* :class:`TrendService` — resolves an inventory item to the best available
  series (the dense catalog-wide history when the item maps to a catalog card,
  else the item's own polled snapshots) and runs :func:`compute_trend` over it.

Both are dependency-light and unit-testable headless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from tcginventory.data.pricehistory import PriceHistoryRepository
from tcginventory.data.repositories import (
    CardCatalogRepository,
    PriceSnapshotRepository,
)

_DEFAULT_WINDOW_DAYS = 30
_MIN_DAYS = 2  # need at least two daily points to fit a line

# |percent change over the window| → number of arrows. Below the first break the
# move is "flat" (no arrows); each further break adds an arrow, so the tiers are
# 1..4 arrows for [2,10), [10,25), [25,50), [50,∞) percent. Tunable in one place.
_TIER_BREAKS = (2.0, 10.0, 25.0, 50.0)
MAX_MAGNITUDE = len(_TIER_BREAKS)


@dataclass(frozen=True)
class Trend:
    """A card's recent price direction and how strong it is."""

    direction: str  # "up" | "down" | "flat" | "none" (no data)
    slope_cents_per_day: float  # fitted slope of the daily price line
    pct_over_window: float | None  # signed % change the slope implies, or None
    magnitude: int  # 0 (flat/none) .. MAX_MAGNITUDE → arrow count
    points: int  # daily points behind the fit
    window_days: int

    @property
    def known(self) -> bool:
        return self.direction != "none"


def _parse(day: str) -> date:
    return date.fromisoformat(day[:10])


def _daily_series(
    series: list[tuple[str, int]], start: date, end: date
) -> list[tuple[date, int]]:
    """Forward-fill sparse ``(date, cents)`` change-points to one value per day in
    ``[start, end]``. Carries the last price known at or before ``start`` so a
    window that opens mid-plateau still starts from the right level."""
    pts = sorted((_parse(d), c) for d, c in series)
    if not pts:
        return []
    price: int | None = None
    idx = 0
    for day, cents in pts:  # carry-in: last value at or before the window start
        if day <= start:
            price = cents
            idx += 1
        else:
            break
    remaining = pts[idx:]
    out: list[tuple[date, int]] = []
    cur = start
    ri = 0
    while cur <= end:
        while ri < len(remaining) and remaining[ri][0] <= cur:
            price = remaining[ri][1]
            ri += 1
        if price is not None:
            out.append((cur, price))
        cur += timedelta(days=1)
    return out


def _magnitude(pct: float) -> int:
    strength = abs(pct)
    tier = 0
    for brk in _TIER_BREAKS:
        if strength >= brk:
            tier += 1
        else:
            break
    return tier


def compute_trend(
    series: list[tuple[str, int]],
    *,
    as_of: date,
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> Trend:
    """Fit a line to the last ``window_days`` of ``series`` and describe the move.

    ``series`` is ``(iso_date, price_cents)`` change-points in any order. Returns
    a ``"none"`` trend when there isn't enough recent data to fit a line.
    """
    start = as_of - timedelta(days=window_days)
    daily = _daily_series(series, start, as_of)
    if len(daily) < _MIN_DAYS:
        return Trend("none", 0.0, None, 0, len(daily), window_days)

    xs = [(day - start).days for day, _ in daily]
    ys = [cents for _, cents in daily]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:  # all points on the same day (shouldn't happen for daily fill)
        return Trend("flat", 0.0, 0.0, 0, n, window_days)
    slope = (
        sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom
    )

    current = ys[-1]
    pct = (slope * window_days / current * 100.0) if current else 0.0
    magnitude = _magnitude(pct)
    if magnitude == 0:
        direction = "flat"
    else:
        direction = "up" if slope > 0 else "down"
    return Trend(direction, slope, pct, magnitude, n, window_days)


class TrendService:
    """Compute per-item price trends from the best available history.

    Prefers the catalog-wide history (``pricehistory.db``, dense daily sweep) when
    an item resolves to a catalog card; falls back to the item's own polled
    snapshots. Card-id resolution is cached per instance, so scanning a whole
    inventory doesn't re-run the fuzzy match for repeated cards.
    """

    def __init__(
        self,
        conn,
        history_conn=None,
        *,
        window_days: int = _DEFAULT_WINDOW_DAYS,
    ) -> None:
        self._catalog = CardCatalogRepository(conn)
        self._snapshots = PriceSnapshotRepository(conn)
        self._history = (
            PriceHistoryRepository(history_conn) if history_conn is not None else None
        )
        self.window_days = window_days
        self._card_ids: dict[tuple, str | None] = {}

    def card_id_for(self, item) -> str | None:
        key = (item.name, item.set_name, item.card_number)
        if key not in self._card_ids:
            hit = self._catalog.image_for(item.name, item.set_name, item.card_number)
            self._card_ids[key] = hit[0] if hit else None
        return self._card_ids[key]

    def series_for(self, item) -> list[tuple[str, int]]:
        if self._history is not None:
            card_id = self.card_id_for(item)
            if card_id:
                catalog_pts = self._history.history_for_card(card_id)
                if catalog_pts:
                    return catalog_pts
        return [
            (snap.as_of[:10], snap.price_cents)
            for snap in self._snapshots.history_for_item(item.id)
        ]

    def trend_for(self, item, *, as_of: date | None = None) -> Trend:
        as_of = as_of or date.today()
        return compute_trend(
            self.series_for(item), as_of=as_of, window_days=self.window_days
        )

    def trends_for(self, items, *, as_of: date | None = None) -> dict[int, Trend]:
        as_of = as_of or date.today()
        out: dict[int, Trend] = {}
        for item in items:
            if item.id is not None and item.id not in out:
                out[item.id] = self.trend_for(item, as_of=as_of)
        return out
