"""ReportingService tests — realized/unrealized P/L, margin, dashboard series."""

from __future__ import annotations

import pytest

from conftest import buy, sell
from tcginventory.data.repositories import ItemRepository, PriceSnapshotRepository
from tcginventory.domain.enums import Intent
from tcginventory.domain.models import PriceSnapshot
from tcginventory.services.reporting import ReportingService


@pytest.fixture()
def rpt(conn):
    return ReportingService(conn)


def _snap(conn, item_id, price_cents, as_of):
    PriceSnapshotRepository(conn).add(
        PriceSnapshot(item_id=item_id, price_cents=price_cents, source="t", as_of=as_of)
    )


def _set_market(conn, item_id, cents):
    """Mimic what a price refresh does: sync the item's current value."""
    items = ItemRepository(conn)
    it = items.get(item_id)
    it.market_value_cents = cents
    items.update(it)
    conn.commit()


def test_collection_value_series_values_holdings_at_recorded_prices(svc, rpt, conn):
    p = buy(svc, "Pikachu", 1000, 500, "2026-01-01", quantity=2)
    item_id = p.lots[0].item_id
    _snap(conn, item_id, 600, "2026-02-01T00:00:00")
    _snap(conn, item_id, 800, "2026-03-01T00:00:00")
    _set_market(conn, item_id, 800)  # current value tracks the latest poll

    series = rpt.collection_value_series()
    by_date = {pt.at: pt.value_cents for pt in series}
    # Held 2 valued at each date's recorded price.
    assert by_date["2026-02-01T00:00:00"] == 1200
    assert by_date["2026-03-01T00:00:00"] == 1600
    # The line's endpoint always equals the Collection Value tile.
    assert series[-1].value_cents == rpt.collection_value_cents() == 1600


def test_collection_value_series_respects_holdings_and_sales_over_time(svc, rpt, conn):
    p = buy(svc, "Pikachu", 1000, 500, "2026-01-01", quantity=2)
    item_id = p.lots[0].item_id
    _snap(conn, item_id, 600, "2026-02-01T00:00:00")  # hold 2 → 1200
    sell(svc, item_id, 1, 900, "2026-02-15")  # now hold 1
    _snap(conn, item_id, 700, "2026-03-01T00:00:00")  # hold 1 → 700
    _set_market(conn, item_id, 700)

    series = rpt.collection_value_series()
    by_date = {pt.at: pt.value_cents for pt in series}
    assert by_date["2026-02-01T00:00:00"] == 1200
    assert by_date["2026-03-01T00:00:00"] == 700
    assert series[-1].value_cents == rpt.collection_value_cents() == 700


def test_collection_value_series_includes_never_polled_at_current_value(svc, rpt):
    # No price history at all → the line still reflects the current value (a
    # graded slab or manual entry that the poll never touches), ending at the
    # tile. (Previously this returned an empty line.)
    buy(svc, "Graded Charizard", 20000, 25000, "2026-01-01")
    series = rpt.collection_value_series()
    assert series  # not empty
    assert series[-1].value_cents == rpt.collection_value_cents() == 25000


def test_collection_value_series_uses_reviewed_value_not_poll(svc, rpt, conn):
    # A reviewed (condition-discounted) card: the poll records a higher NM price
    # but the line + tile must use the user's reviewed value.
    p = buy(svc, "MP Blastoise", 5000, 8000, "2026-01-01")
    item_id = p.lots[0].item_id
    _snap(conn, item_id, 16000, "2026-02-01T00:00:00")  # NM poll, ignored
    items = ItemRepository(conn)
    it = items.get(item_id)
    it.review = True
    it.market_value_cents = 8000  # user's MP value
    items.update(it)
    conn.commit()

    series = rpt.collection_value_series()
    assert series[-1].value_cents == rpt.collection_value_cents() == 8000
    assert all(pt.value_cents == 8000 for pt in series)  # never the 16000 poll


def test_recorded_collection_value_drives_the_series(svc, rpt, conn):
    buy(svc, "Pikachu", 1000, 1200, "2026-01-01")
    rpt.record_collection_value(as_of="2026-05-01T12:00:00")
    series = rpt.collection_value_series()
    # The recorded daily point (the tile figure) is the authoritative endpoint.
    assert series[-1].at == "2026-05-01"
    assert series[-1].value_cents == 1200


def test_realized_pl_and_net_cash_after_a_sale(svc, rpt):
    p = buy(svc, "X", 9000, 9000, "2026-01-01")
    item_id = p.lots[0].item_id
    sell(svc, item_id, 1, 12000, "2026-02-01")

    assert rpt.realized_pl_cents() == 3000
    assert rpt.net_cash_cents() == 3000  # −9000 + 12000


def test_portfolio_summary(svc, rpt):
    # Hold one card bought at $80, market $100 (unrealized +$20)...
    buy(svc, "Hold", 8000, 10000, "2026-01-01")
    # ...and flip another for a $5 gain.
    flip = buy(svc, "Flip", 1000, 1000, "2026-01-02")
    sell(svc, flip.lots[0].item_id, 1, 1500, "2026-02-01")

    summary = rpt.portfolio_summary()
    assert summary.cost_basis_cents == 8000  # only the held card remains
    assert summary.market_value_cents == 10000
    assert summary.unrealized_pl_cents == 2000
    assert summary.realized_pl_cents == 500
    assert summary.net_cash_cents == -8000 - 1000 + 1500


def test_realized_pl_intent_filter(svc, rpt):
    keeper = buy(svc, "Keeper", 1000, 1000, "2026-01-01", intent=Intent.PERSONAL)
    flip = buy(svc, "Flip", 1000, 1000, "2026-01-02", intent=Intent.RESALE)
    sell(svc, keeper.lots[0].item_id, 1, 1200, "2026-02-01")
    sell(svc, flip.lots[0].item_id, 1, 1500, "2026-02-02")

    assert rpt.realized_pl_cents(intent=Intent.PERSONAL) == 200
    assert rpt.realized_pl_cents(intent=Intent.RESALE) == 500
    assert rpt.realized_pl_cents() == 700


def test_margin_and_gross_margin_pct(svc, rpt):
    p = buy(svc, "X", 8000, 8000, "2026-01-01")
    s = sell(svc, p.lots[0].item_id, 1, 12000, "2026-02-01")

    assert rpt.transaction_margin_cents(s.transaction.id) == 4000
    assert rpt.transaction_proceeds_cents(s.transaction.id) == 12000
    assert rpt.gross_margin_pct(s.transaction.id) == pytest.approx(33.333, abs=0.01)


def test_pure_purchase_has_no_margin(svc, rpt):
    p = buy(svc, "X", 8000, 8000, "2026-01-01")
    assert rpt.transaction_margin_cents(p.transaction.id) == 0
    assert rpt.gross_margin_pct(p.transaction.id) is None


def test_dashboard_series_are_cumulative(svc, rpt):
    p = buy(svc, "X", 9000, 9000, "2026-01-01")
    sell(svc, p.lots[0].item_id, 1, 12000, "2026-02-01")

    money = rpt.money_movement_series()
    assert [pt.value_cents for pt in money] == [-9000, 3000]

    profit = rpt.realized_profit_series()
    assert [pt.value_cents for pt in profit] == [0, 3000]  # purchase 0, then +3000


def test_voided_transactions_excluded_from_reporting(svc, rpt):
    p = buy(svc, "X", 9000, 9000, "2026-01-01")
    s = sell(svc, p.lots[0].item_id, 1, 12000, "2026-02-01")

    svc.void(s.transaction.id)

    assert rpt.realized_pl_cents() == 0
    assert rpt.net_cash_cents() == -9000  # only the purchase remains
