"""ReportingService tests — realized/unrealized P/L, margin, dashboard series."""

from __future__ import annotations

import pytest

from conftest import buy, sell
from tcginventory.domain.enums import Intent
from tcginventory.services.reporting import ReportingService
from tcginventory.services.transactions import TransactionService


@pytest.fixture()
def svc(conn):
    return TransactionService(conn)


@pytest.fixture()
def rpt(conn):
    return ReportingService(conn)


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
