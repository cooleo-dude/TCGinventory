"""eBay auctions: max-bid math, persistence (auctions.db), and the view."""

from __future__ import annotations

import os

import pytest

from tcginventory.data.auctions import (
    Auction,
    AuctionCard,
    AuctionRepository,
    open_auctions,
)
from tcginventory.services.auctions import (
    DEFAULT_TAX_RATE_BPS,
    PERCENTAGES,
    breakdown,
    max_bid_cents,
)

# ---------------------------------------------------------------------------
# max-bid math
# ---------------------------------------------------------------------------


def test_worked_example_100pct():
    # $100 of cards, $5 shipping, 6% tax. All-in at 100% must be exactly $100:
    # (89.34 + 5) * 1.06 = 100.00 -> max bid $89.34.
    assert max_bid_cents(10_000, 500, 600, 100) == 8_934


def test_worked_example_70pct():
    assert max_bid_cents(10_000, 500, 600, 70) == 6_104


def test_no_tax_no_shipping_is_flat_percentage():
    assert max_bid_cents(10_000, 0, 0, 100) == 10_000
    assert max_bid_cents(10_000, 0, 0, 90) == 9_000
    assert max_bid_cents(10_000, 0, 0, 70) == 7_000


def test_clamped_to_zero_when_costs_exceed_target():
    # Cheap cards, expensive shipping -> no room to bid.
    assert max_bid_cents(1_000, 5_000, 600, 70) == 0


def test_zero_value_is_zero():
    assert max_bid_cents(0, 500, 600, 100) == 0


def test_breakdown_covers_all_percentages_and_is_monotonic():
    bd = breakdown(10_000, 500, 600)
    assert set(bd.bids) == set(PERCENTAGES)
    ordered = [bd.bids[p] for p in sorted(PERCENTAGES)]
    assert ordered == sorted(ordered)  # higher target -> higher (or equal) bid


def test_default_tax_rate_is_six_percent():
    assert DEFAULT_TAX_RATE_BPS == 600


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


@pytest.fixture()
def auctions_conn():
    c = open_auctions(":memory:")
    yield c
    c.close()


def test_repo_save_get_roundtrip(auctions_conn):
    repo = AuctionRepository(auctions_conn)
    a = Auction(
        title="Lot of 3",
        url="https://www.ebay.com/itm/123",
        shipping_cents=500,
        tax_rate_bps=600,
        cards=[
            AuctionCard(name="Charizard", market_value_cents=2_000, quantity=2),
            AuctionCard(name="Pikachu", market_value_cents=500),
        ],
    )
    repo.save(a, now="2026-07-16T00:00:00+00:00")
    assert a.id is not None

    got = repo.get(a.id)
    assert got.title == "Lot of 3"
    assert got.url == "https://www.ebay.com/itm/123"
    assert len(got.cards) == 2
    assert got.value_cents == 2_000 * 2 + 500  # 4500


def test_repo_update_replaces_cards(auctions_conn):
    repo = AuctionRepository(auctions_conn)
    a = Auction(title="orig", cards=[AuctionCard(name="A"), AuctionCard(name="B")])
    repo.save(a, now="2026-07-16T00:00:00+00:00")

    a.title = "renamed"
    a.cards = [AuctionCard(name="A")]
    repo.save(a, now="2026-07-16T01:00:00+00:00")

    got = repo.get(a.id)
    assert got.title == "renamed"
    assert [c.name for c in got.cards] == ["A"]
    # No orphaned child rows left behind by the replace.
    assert auctions_conn.execute("SELECT COUNT(*) FROM auction_card").fetchone()[0] == 1


def test_repo_delete_cascades(auctions_conn):
    repo = AuctionRepository(auctions_conn)
    a = Auction(title="x", cards=[AuctionCard(name="A"), AuctionCard(name="B")])
    repo.save(a, now="2026-07-16T00:00:00+00:00")
    repo.delete(a.id)
    assert repo.get(a.id) is None
    assert auctions_conn.execute("SELECT COUNT(*) FROM auction_card").fetchone()[0] == 0


def test_repo_list_newest_first(auctions_conn):
    repo = AuctionRepository(auctions_conn)
    first = Auction(title="first", cards=[AuctionCard(name="A")])
    repo.save(first, now="2026-07-16T00:00:00+00:00")
    second = Auction(title="second", cards=[AuctionCard(name="B")])
    repo.save(second, now="2026-07-16T02:00:00+00:00")
    titles = [a.title for a in repo.list_all()]
    assert titles == ["second", "first"]


# ---------------------------------------------------------------------------
# the view (offscreen)
# ---------------------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _FakeHistory:
    def __init__(self, prices):
        self._prices = prices  # card_id -> cents

    def current_price(self, card_id):
        return self._prices.get(card_id)


def _view(app, history=None):
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views.auctions import AuctionsView

    conn = connect(":memory:")
    migrate(conn)
    # history=None -> no pricehistory.db lookup / no filesystem access in tests.
    return AuctionsView(conn, auctions_conn=open_auctions(":memory:"), history=history)


def test_view_breakdown_updates_live(app):
    view = _view(app)
    row = view._add_card_row()
    row.name.setText("Charizard")
    row.market.setValue(20.0)
    row.qty.setValue(2)
    view._shipping.setValue(5.0)  # tax stays at the 6% default
    view._update_breakdown()
    text = view._breakdown.text()
    assert "$40.00" in text  # card value 2 x $20
    # 100% max bid on $40 value, $5 ship, 6% tax = 3274 cents ($32.74):
    # (32.74 + 5) * 1.06 = 40.00.
    assert "$32.74" in text
    view.deleteLater()


def test_view_save_load_and_delete_roundtrip(app):
    view = _view(app)
    row = view._add_card_row()
    row.name.setText("Charizard")
    row.market.setValue(20.0)
    row.qty.setValue(2)
    view._title.setText("Test lot")
    view._shipping.setValue(5.0)
    view._save()

    assert view._watch.rowCount() == 1
    saved_id = view._edit_id
    assert saved_id is not None

    view._new()
    assert view._card_table.rowCount() == 0
    assert view._edit_id is None

    view._load_auction(saved_id)
    assert view._title.text() == "Test lot"
    assert view._card_table.rowCount() == 1
    assert view._edit_id == saved_id

    # Deleting the loaded auction clears the editor and empties the watchlist.
    auction = view._repo.get(saved_id)
    from PySide6.QtWidgets import QMessageBox

    orig = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    try:
        view._delete(auction)
    finally:
        QMessageBox.question = orig
    assert view._watch.rowCount() == 0
    assert view._edit_id is None
    view.deleteLater()


def test_refresh_values_from_history(app, monkeypatch):
    view = _view(app, history=_FakeHistory({"cel-4": 1_500}))
    a = Auction(
        title="lot",
        cards=[
            AuctionCard(
                name="Charizard",
                card_id="cel-4",
                set_name="Celebrations",
                number="4",
                market_value_cents=500,
            ),
            AuctionCard(name="Manual entry", market_value_cents=300),  # no card_id
        ],
    )
    view._repo.save(a, now="2026-07-16T00:00:00+00:00")
    view.refresh()

    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: None)
    )
    view._refresh_values(view._repo.get(a.id))

    got = view._repo.get(a.id)
    vals = {c.name: c.market_value_cents for c in got.cards}
    assert vals["Charizard"] == 1_500  # repriced from history
    assert vals["Manual entry"] == 300  # no catalog match -> left unchanged
    # No live fetch spawned (history had the price) and value cell reflects it.
    assert view._price_workers == []
    assert view._watch.item(0, 2).text() == "$18.00"  # 1500 + 300
    view.deleteLater()


def test_gallery_reflects_editor_cards(app, monkeypatch):
    view = _view(app)
    # Capture the entries handed to the gallery (avoids building real thumbnails
    # / spawning image-fetch threads in the test).
    captured = {}
    monkeypatch.setattr(
        view._gallery, "set_cards", lambda entries: captured.__setitem__("e", entries)
    )
    monkeypatch.setattr(view._gallery, "show_gallery", lambda: None)

    r1 = view._add_card_row()
    r1._card_id, r1._image_url = "a", "http://x/a.png"
    r2 = view._add_card_row()
    r2._card_id, r2._image_url = "b", "http://x/b.png"
    view._add_card_row()  # no card_id/image -> excluded from the gallery

    view._refresh_gallery()
    assert [e[0] for e in captured["e"]] == ["a", "b"]
    view.deleteLater()


def test_auction_card_tcgplayer_url_roundtrip(auctions_conn):
    repo = AuctionRepository(auctions_conn)
    a = Auction(
        title="lot",
        cards=[AuctionCard(name="Zard", tcgplayer_url="https://tcgplayer.com/z")],
    )
    repo.save(a, now="2026-07-17T00:00:00+00:00")
    assert repo.get(a.id).cards[0].tcgplayer_url == "https://tcgplayer.com/z"


def test_ensure_columns_adds_tcgplayer_url_to_old_db():
    import sqlite3

    from tcginventory.data.auctions import _ensure_columns

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE auction_card (id INTEGER PRIMARY KEY, name TEXT)")
    _ensure_columns(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(auction_card)")}
    assert "tcgplayer_url" in cols
    _ensure_columns(conn)  # idempotent — no error on a second run
    conn.close()


def test_price_result_populates_tcgplayer_button(app):
    view = _view(app)
    row = view._add_card_row()
    row.name.setText("Charizard")
    # Simulate the live tcgcsv lookup returning (price_cents, product_url).
    view._on_price_ready(
        row, None, (1850, "https://www.tcgplayer.com/product/123"), had_hist_price=False
    )
    assert row.tcg_btn.isEnabled()
    assert row.market.value() == 18.50  # backfilled since history had no price
    assert row.to_card().tcgplayer_url == "https://www.tcgplayer.com/product/123"
    view.deleteLater()


def test_tcgplayer_button_opens_url(app, monkeypatch):
    view = _view(app)
    row = view._add_card_row()
    row.set_tcgplayer_url("https://tcgplayer.com/x")
    assert row.tcg_btn.isEnabled()

    opened: list[str] = []
    monkeypatch.setattr(
        "tcginventory.ui.views.auctions.QDesktopServices",
        type("D", (), {"openUrl": staticmethod(lambda u: opened.append(u.toString()))}),
    )
    row.tcg_btn.click()
    assert opened == ["https://tcgplayer.com/x"]
    view.deleteLater()


def test_button_disabled_without_url(app):
    view = _view(app)
    row = view._add_card_row()
    assert not row.tcg_btn.isEnabled()  # no link until one resolves
    view.deleteLater()
