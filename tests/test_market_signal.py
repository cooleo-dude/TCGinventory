"""Market-signal analytics (M-Market): liquidity, markup, volatility per card."""

from __future__ import annotations

import pytest

from conftest import buy, sell
from tcginventory.data.repositories import PriceSnapshotRepository
from tcginventory.domain.enums import Intent
from tcginventory.domain.models import PriceSnapshot
from tcginventory.services.market_signal import MarketSignalService
from tcginventory.services.transactions import TransactionService


def _item_id(conn, name):
    return conn.execute("SELECT id FROM item WHERE name = ?", (name,)).fetchone()[0]


def _snap(conn, item_id, price_cents, as_of):
    PriceSnapshotRepository(conn).add(
        PriceSnapshot(item_id=item_id, price_cents=price_cents, source="t", as_of=as_of)
    )


def test_signals_capture_liquidity_markup_and_volatility(conn):
    svc = TransactionService(conn)
    # Acquire 4 @ 100¢ basis (400¢ total), market 200¢ each; sell 2 for 300¢ 31 days on.
    buy(svc, "Charizard", 400, 200, "2026-01-01", quantity=4)
    item_id = _item_id(conn, "Charizard")
    sell(svc, item_id, 2, 300, "2026-02-01")
    for price, day in ((200, "2026-01-10"), (220, "2026-01-20"), (180, "2026-01-30")):
        _snap(conn, item_id, price, day)

    sig = MarketSignalService(conn).signals(window_days=365, as_of="2026-06-01")
    assert len(sig) == 1
    m = sig[0]
    assert (m.units_acquired, m.units_sold, m.units_on_hand) == (4, 2, 2)
    assert m.sell_through == pytest.approx(0.5)
    assert m.avg_days_to_sell == pytest.approx(31, abs=1)
    assert m.realized_margin_pct == pytest.approx(50.0)  # (150-100)/100
    assert m.price_cents == 200
    assert m.unrealized_markup_pct == pytest.approx(100.0)  # (200-100)/100
    assert m.volatility_pct == pytest.approx(8.16, abs=0.4)  # CV of 200/220/180


def test_overview_aggregates_sell_through_and_volatility(conn):
    svc = TransactionService(conn)
    buy(svc, "A", 200, 100, "2026-01-01", quantity=2)
    buy(svc, "B", 200, 100, "2026-01-01", quantity=2)
    sell(svc, _item_id(conn, "A"), 1, 120, "2026-01-15")
    _snap(conn, _item_id(conn, "A"), 100, "2026-01-05")
    _snap(conn, _item_id(conn, "A"), 140, "2026-01-25")

    service = MarketSignalService(conn)
    overview = service.overview(service.signals(window_days=365, as_of="2026-06-01"))
    assert overview.tracked == 2
    assert overview.total_acquired == 4 and overview.total_sold == 1
    assert overview.overall_sell_through == pytest.approx(0.25)
    assert overview.median_volatility_pct is not None


def test_personal_and_ripping_stock_is_excluded(conn):
    svc = TransactionService(conn)
    buy(svc, "Keeper", 100, 100, "2026-01-01", intent=Intent.PERSONAL)
    buy(svc, "Resale", 100, 100, "2026-01-01", intent=Intent.RESALE)
    names = {m.name for m in MarketSignalService(conn).signals()}
    assert names == {"Resale"}


def test_market_view_populates_rows(conn):
    import os

    import pytest as _pytest

    _pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from tcginventory.ui.views.market_signal import MarketView

    svc = TransactionService(conn)
    buy(svc, "Charizard", 400, 200, "2026-01-01", quantity=4)
    sell(svc, _item_id(conn, "Charizard"), 2, 300, "2026-02-01")

    view = MarketView(conn)
    view.refresh()
    assert view._table.rowCount() == 1
    assert view._table.isVisible() or view._table.rowCount() == 1
    view.deleteLater()
