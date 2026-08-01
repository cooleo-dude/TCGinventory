"""Dashboard highlights: Top Gainers / Top Losers (unrealized $) and Top
Sellers (units sold in a trailing window)."""

from __future__ import annotations

from conftest import buy, sell
from tcginventory.domain.enums import Intent
from tcginventory.services.reporting import ReportingService
from tcginventory.services.transactions import TransactionService


def _price(conn, item_id: int, cents: int) -> None:
    """Set a card's *current* market value."""
    conn.execute(
        "UPDATE item SET market_value_cents = ? WHERE id = ?", (cents, item_id)
    )
    conn.commit()


def _snapshot(conn, item_id: int, cents: int, as_of: str) -> None:
    """Record a historical market-price snapshot (the movers' baseline)."""
    conn.execute(
        "INSERT INTO price_snapshot (item_id, source, price_cents, as_of) "
        "VALUES (?, 't', ?, ?)",
        (item_id, cents, as_of),
    )
    conn.commit()


def test_top_gainers_losers_rank_by_market_price_movement(conn):
    svc = TransactionService(conn)
    rpt = ReportingService(conn)

    a = buy(svc, "Alakazam", 1000, 1000, "2026-06-01").lots[0].item_id
    b = buy(svc, "Charizard", 1000, 1000, "2026-06-01").lots[0].item_id
    c = buy(svc, "Miraidon", 1000, 1000, "2026-06-01").lots[0].item_id
    # baseline recorded prices, then current prices (the movement is now − base)
    for i in (a, b, c):
        _snapshot(conn, i, 1000, "2026-06-01T00:00:00")
    _price(conn, a, 1800)  # price moved +$8.00
    _price(conn, b, 1300)  # +$3.00
    _price(conn, c, 400)  # −$6.00

    gainers = rpt.top_gainers()  # All Time → baseline = earliest snapshot
    assert [g.item_id for g in gainers] == [a, b]  # by price change desc
    assert gainers[0].change_cents == 800
    assert gainers[0].price_cents == 1800  # displays the *current market price*
    assert round(gainers[0].change_pct) == 80

    losers = rpt.top_losers()
    assert [loss.item_id for loss in losers] == [c]
    assert losers[0].change_cents == -600

    # The combined one-pass call returns the same gainers/losers.
    g2, l2 = rpt.price_movers()
    assert [m.item_id for m in g2] == [m.item_id for m in gainers]
    assert [m.item_id for m in l2] == [m.item_id for m in losers]


def test_price_movers_use_the_window_start_baseline(conn):
    svc = TransactionService(conn)
    rpt = ReportingService(conn)
    a = buy(svc, "Gyarados", 1000, 1000, "2026-05-01").lots[0].item_id
    _snapshot(conn, a, 1000, "2026-05-01T00:00:00")  # earliest
    _snapshot(conn, a, 1500, "2026-06-01T00:00:00")  # last price before the window
    _price(conn, a, 1800)  # current

    # window starting 2026-06-15 baselines off the last prior snapshot (1500) → +$3
    windowed = rpt.top_gainers(since="2026-06-15T00:00:00")
    assert windowed[0].change_cents == 300
    # All Time baselines off the earliest snapshot (1000) → +$8
    assert rpt.top_gainers()[0].change_cents == 800


def test_price_movers_skip_cards_without_baseline(conn):
    svc = TransactionService(conn)
    rpt = ReportingService(conn)
    a = buy(svc, "NoHistory", 1000, 1000, "2026-06-01").lots[0].item_id
    _price(conn, a, 1800)  # current price set, but no snapshot history
    assert rpt.top_gainers() == []  # can't measure movement → excluded


def test_windowed_movers_fall_back_to_earliest_snapshot(conn):
    # A window whose start predates all recorded history must still populate:
    # baseline falls back to the card's earliest snapshot (regression — windows
    # other than All Time were coming up empty on short price histories).
    svc = TransactionService(conn)
    rpt = ReportingService(conn)
    a = buy(svc, "Ledian", 1000, 1000, "2026-07-25").lots[0].item_id
    _snapshot(conn, a, 1600, "2026-07-25T00:00:00")  # only recent history
    _price(conn, a, 1800)  # current

    gainers = rpt.top_gainers(since="2026-05-01T00:00:00")  # window starts earlier
    assert [g.item_id for g in gainers] == [a]
    assert gainers[0].change_cents == 200  # 1800 − earliest 1600


def test_realized_and_net_cash_respect_since(conn):
    svc = TransactionService(conn)
    rpt = ReportingService(conn)
    item = buy(svc, "Xatu", 1000, 1000, "2026-01-01", quantity=4).lots[0].item_id
    sell(svc, item, 1, 500, "2026-02-01")  # old sale
    sell(svc, item, 1, 700, "2026-07-20")  # recent sale

    cutoff = "2026-06-01T00:00:00"
    assert rpt.realized_pl_cents(since=cutoff) < rpt.realized_pl_cents()  # subset
    assert rpt.net_cash_cents(since=cutoff) == 700  # only the recent sale's cash in


def test_total_return_pct(conn):
    svc = TransactionService(conn)
    rpt = ReportingService(conn)
    assert rpt.total_return_pct() is None  # nothing held → undefined

    item = buy(svc, "Alakazam", 1000, 1000, "2026-06-01").lots[0].item_id
    _price(conn, item, 1200)  # +$2.00 unrealized on $10 cost, no sales
    assert rpt.total_return_pct() == 20.0


def _mark_review(conn, item_id: int) -> None:
    conn.execute("UPDATE item SET review = 1 WHERE id = ?", (item_id,))
    conn.commit()


def _mark_graded(conn, item_id: int) -> None:
    conn.execute("UPDATE item SET is_graded = 1 WHERE id = ?", (item_id,))
    conn.commit()


def test_movers_exclude_personal_ripping_review_and_graded(conn):
    svc = TransactionService(conn)
    rpt = ReportingService(conn)

    resale = buy(svc, "Resale", 1000, 1000, "2026-06-01").lots[0].item_id
    personal = (
        buy(svc, "Personal", 1000, 1000, "2026-06-01", intent=Intent.PERSONAL)
        .lots[0]
        .item_id
    )
    ripped = (
        buy(svc, "Sealed Box", 1000, 1000, "2026-06-01", intent=Intent.RIPPING)
        .lots[0]
        .item_id
    )
    reviewed = buy(svc, "Reviewed", 1000, 1000, "2026-06-01").lots[0].item_id
    graded = buy(svc, "Graded Slab", 1000, 1000, "2026-06-01").lots[0].item_id
    for i in (resale, personal, ripped, reviewed, graded):
        _snapshot(conn, i, 1000, "2026-06-01T00:00:00")  # baseline
        _price(conn, i, 1800)  # each would be a +$8 mover if included
    _mark_review(conn, reviewed)
    _mark_graded(conn, graded)

    ids = [g.item_id for g in rpt.top_gainers()]
    assert ids == [resale]  # only the plain resale card ranks
    for excluded in (personal, ripped, reviewed, graded):
        assert excluded not in ids
    # None of the excluded items surface as losers either.
    assert all(m.item_id == resale for m in rpt.top_losers()) or not rpt.top_losers()


def test_top_sellers_excludes_personal_and_reviewed(conn):
    svc = TransactionService(conn)
    rpt = ReportingService(conn)

    resale = buy(svc, "Resale", 500, 500, "2026-07-01", quantity=10).lots[0].item_id
    personal = (
        buy(
            svc,
            "Personal",
            500,
            500,
            "2026-07-01",
            quantity=10,
            intent=Intent.PERSONAL,
        )
        .lots[0]
        .item_id
    )
    reviewed = buy(svc, "Reviewed", 500, 500, "2026-07-01", quantity=10).lots[0].item_id
    sell(svc, resale, 4, 3200, "2026-07-20")
    sell(svc, personal, 9, 7200, "2026-07-20")  # personal-lot sale: excluded
    sell(svc, reviewed, 8, 6400, "2026-07-20")
    _mark_review(conn, reviewed)

    sellers = rpt.top_sellers(window_days=90, as_of="2026-07-30T00:00:00")
    assert [s.item_id for s in sellers] == [resale]  # personal + review excluded


def test_top_sellers_counts_units_in_window(conn):
    svc = TransactionService(conn)
    rpt = ReportingService(conn)

    hot = buy(svc, "Pikachu", 500, 500, "2026-07-01", quantity=10).lots[0].item_id
    cool = buy(svc, "Snorlax", 500, 500, "2026-07-01", quantity=10).lots[0].item_id
    old = buy(svc, "Mewtwo", 500, 500, "2026-01-01", quantity=10).lots[0].item_id

    sell(svc, hot, 5, 4000, "2026-07-20")
    sell(svc, hot, 2, 1600, "2026-07-25")  # 7 units, 2 sales
    sell(svc, cool, 3, 2400, "2026-07-22")  # 3 units, 1 sale
    sell(svc, old, 9, 7000, "2026-02-01")  # outside a 90-day window from now

    sellers = rpt.top_sellers(window_days=90, as_of="2026-07-30T00:00:00")
    assert [s.item_id for s in sellers] == [hot, cool]  # old excluded, hot first
    assert sellers[0].units == 7
    assert sellers[0].sales == 2
    assert sellers[1].units == 3


def test_upcoming_shows_returns_future_sorted(conn):
    from tcginventory.data.repositories import CardShowRepository
    from tcginventory.domain.models import CardShow
    from tcginventory.services.cardshows import CardShowService

    repo = CardShowRepository(conn)
    for sid, name, date in [
        (1, "Past", "2026-01-01"),  # attending but in the past → dropped
        (2, "Soon", "2026-08-02"),  # attending → shown
        (3, "Later", "2026-08-16"),  # walking → shown
        (4, "Maybe", "2026-08-09"),  # pending → excluded
        (5, "Unmarked", "2026-08-05"),  # no status → excluded
    ]:
        repo.upsert(
            CardShow(
                source_id=sid,
                name=name,
                state="NJ",
                starts_on=date,
                city="X",
                fetched_at="t",
            )
        )
    statuses = [(1, "attending"), (2, "attending"), (3, "walking"), (4, "pending")]
    for sid, status in statuses:
        conn.execute(
            "INSERT INTO card_show_status (source_id, status) VALUES (?, ?)",
            (sid, status),
        )
    conn.commit()

    up = CardShowService(conn).upcoming(limit=5, today="2026-07-30")
    # only future attending/walking, soonest first
    assert [s.name for s in up] == ["Soon", "Later"]
    assert up[0].status == "attending"


def test_top_sellers_ignores_trades_and_voids(conn):
    svc = TransactionService(conn)
    rpt = ReportingService(conn)
    item = buy(svc, "Gengar", 600, 600, "2026-07-01", quantity=6).lots[0].item_id

    voided = sell(svc, item, 4, 3200, "2026-07-15")
    svc.void(voided.transaction.id)  # a voided sale must not count

    sellers = rpt.top_sellers(window_days=90, as_of="2026-07-30T00:00:00")
    assert sellers == []
