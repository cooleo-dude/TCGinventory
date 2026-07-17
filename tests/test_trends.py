"""Market-trend maths and per-item resolution (headless, Qt-free)."""

from __future__ import annotations

import sqlite3
from datetime import date

from tcginventory.data.migrator import migrate
from tcginventory.data.pricehistory import PriceHistoryRepository, open_history
from tcginventory.data.repositories import (
    CardCatalogRepository,
    PriceSnapshotRepository,
)
from tcginventory.domain.enums import Category
from tcginventory.domain.models import CatalogCard, Item, PriceSnapshot
from tcginventory.services.trends import MAX_MAGNITUDE, TrendService, compute_trend

AS_OF = date(2026, 7, 10)


def _daily(start: str, values: list[int]) -> list[tuple[str, int]]:
    """One point per day from `start` (dense series for a clean slope)."""
    from datetime import timedelta

    d0 = date.fromisoformat(start)
    return [((d0 + timedelta(days=i)).isoformat(), v) for i, v in enumerate(values)]


def test_no_data_is_none():
    t = compute_trend([], as_of=AS_OF)
    assert t.direction == "none"
    assert not t.known
    assert t.pct_over_window is None
    assert t.magnitude == 0


def test_rising_series_trends_up():
    # +50 cents/day for 30 days on a ~$5 card → a strong up move.
    series = _daily("2026-06-11", [500 + 50 * i for i in range(30)])
    t = compute_trend(series, as_of=AS_OF)
    assert t.direction == "up"
    assert t.slope_cents_per_day > 0
    assert t.pct_over_window > 0
    assert t.magnitude >= 1


def test_falling_series_trends_down():
    series = _daily("2026-06-11", [2000 - 40 * i for i in range(30)])
    t = compute_trend(series, as_of=AS_OF)
    assert t.direction == "down"
    assert t.slope_cents_per_day < 0
    assert t.pct_over_window < 0
    assert t.magnitude >= 1


def test_flat_series_below_threshold():
    # A single change-point long ago → flat plateau across the whole window.
    t = compute_trend([("2026-01-01", 1000)], as_of=AS_OF)
    assert t.direction == "flat"
    assert t.magnitude == 0
    assert t.known  # we *know* it's flat, unlike "none"


def test_magnitude_scales_with_steepness():
    gentle = compute_trend(
        _daily("2026-06-11", [1000 + 5 * i for i in range(30)]), as_of=AS_OF
    )
    steep = compute_trend(
        _daily("2026-06-11", [1000 + 60 * i for i in range(30)]), as_of=AS_OF
    )
    assert steep.magnitude > gentle.magnitude
    assert steep.magnitude <= MAX_MAGNITUDE


def test_carry_in_from_before_the_window():
    # Last change was well before the window and price hasn't moved since → flat,
    # anchored at the carried-in level (not "none").
    series = [("2026-03-01", 800), ("2026-03-15", 1200)]
    t = compute_trend(series, as_of=AS_OF, window_days=30)
    assert t.direction == "flat"
    assert t.points >= 2


def test_window_excludes_old_moves():
    # A big jump 60 days ago, flat within the last 30 → reads flat now.
    series = [("2026-05-01", 500), ("2026-05-02", 5000), ("2026-06-01", 5000)]
    t = compute_trend(series, as_of=AS_OF, window_days=30)
    assert t.direction == "flat"


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def test_service_prefers_catalog_history():
    conn = _memory_db()
    CardCatalogRepository(conn).replace_all(
        [
            CatalogCard(
                card_id="sv9-1", name="Pikachu", set_id="sv9",
                set_name="Journey Together", set_code="JTG", number="1",
                language="EN", image_small="http://x/a.png",
            )
        ],
        "t",
    )
    conn.commit()
    item = Item(
        name="Pikachu", category=Category.SINGLE, set_name="Journey Together",
        card_number="1", id=1,
    )

    hist = open_history(":memory:")
    repo = PriceHistoryRepository(hist)
    for day, cents in _daily("2026-06-11", [500 + 40 * i for i in range(30)]):
        repo.record("sv9-1", cents, day)
    hist.commit()

    svc = TrendService(conn, hist)
    assert svc.card_id_for(item) == "sv9-1"
    t = svc.trend_for(item, as_of=AS_OF)
    assert t.direction == "up"
    assert t.magnitude >= 1


def test_service_falls_back_to_item_snapshots():
    conn = _memory_db()  # empty catalog → no card_id resolution
    snaps = PriceSnapshotRepository(conn)
    for day, cents in _daily("2026-06-11", [3000 - 50 * i for i in range(30)]):
        snaps.add(
            PriceSnapshot(item_id=7, price_cents=cents, as_of=day, source="tcgcsv")
        )
    conn.commit()
    item = Item(name="Mystery", category=Category.SINGLE, id=7)

    svc = TrendService(conn, history_conn=None)
    t = svc.trend_for(item, as_of=AS_OF)
    assert t.direction == "down"


def test_trends_for_batches_by_item():
    conn = _memory_db()
    item = Item(name="Solo", category=Category.SINGLE, id=3)
    svc = TrendService(conn, history_conn=None)
    out = svc.trends_for([item, item], as_of=AS_OF)  # dup → resolved once
    assert set(out) == {3}
