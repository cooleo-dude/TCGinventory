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
from tcginventory.services.pricing import CardVariant


def _variants(cents: int, url: str) -> list[CardVariant]:
    """One base printing — the shape PriceLookupWorker emits for a plain card."""
    return [CardVariant("Base", url, cents, True)]


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
    assert "$40.00" in view._bid_caption.text()  # card value 2 x $20
    # 100% max bid on $40 value, $5 ship, 6% tax = 3274 cents ($32.74):
    # (32.74 + 5) * 1.06 = 40.00.
    assert view._mini[100].text() == "$32.74"
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

    assert len(view._cards) == 1
    saved_id = view._edit_id
    assert saved_id is not None

    view._new()
    assert len(view._rows) == 0  # (table also holds a trailing ＋ add-row)
    assert view._edit_id is None

    view._load_auction(saved_id)
    assert view._title.text() == "Test lot"
    assert len(view._rows) == 1
    assert view._edit_id == saved_id

    # Deleting the loaded auction clears the editor and empties the watchlist.
    auction = view._repo.get(saved_id)
    from tcginventory.ui.views import auctions as auctions_mod

    orig = auctions_mod._confirm_delete
    auctions_mod._confirm_delete = staticmethod(lambda *a, **k: True)
    try:
        view._delete(auction)
    finally:
        auctions_mod._confirm_delete = orig
    assert len(view._cards) == 0
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

    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    view._refresh_values(view._repo.get(a.id))

    got = view._repo.get(a.id)
    vals = {c.name: c.market_value_cents for c in got.cards}
    assert vals["Charizard"] == 1_500  # repriced from history
    assert vals["Manual entry"] == 300  # no catalog match -> left unchanged
    # No live fetch spawned (history had the price) and the card reflects it.
    assert view._price_workers == []
    assert view._cards[0].value_text() == "$18.00"  # 1500 + 300
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
    # Simulate the live tcgcsv lookup returning the card's printings (base first).
    view._on_price_ready(
        row, None, _variants(1850, "https://www.tcgplayer.com/product/123")
    )
    assert row.tcg_btn.isEnabled()
    assert row.market.value() == 18.50  # live price applied
    assert row.to_card().tcgplayer_url == "https://www.tcgplayer.com/product/123"
    assert row.to_card().market_is_manual is False  # an auto price, not a manual edit
    view.deleteLater()


def test_auction_row_printing_picker(app):
    # A card with more than one tcgplayer product offers a per-row Printing
    # dropdown; base is applied by default, picking the exclusive re-prices.
    view = _view(app)
    row = view._add_card_row()
    row.name.setText("Eevee")
    variants = [
        CardVariant("Base", "u-base", 1370, True),
        CardVariant("Pokemon Center Exclusive", "u-excl", 9413, False),
    ]
    view._on_price_ready(row, None, variants)
    assert row.market.value() == 13.70  # base applied
    assert row.to_card().tcgplayer_url == "u-base"
    assert not row.printing.isHidden()  # picker offered
    assert row.printing.count() == 2

    row.printing.setCurrentIndex(1)
    row._on_printing_changed()
    assert row.market.value() == 94.13
    assert row.to_card().tcgplayer_url == "u-excl"

    # A single-printing card hides the picker (nothing to choose).
    row2 = view._add_card_row()
    view._on_price_ready(row2, None, _variants(500, "u1"))
    assert row2.printing.isHidden()
    view.deleteLater()


def test_button_disabled_without_url(app):
    view = _view(app)
    row = view._add_card_row()
    assert not row.tcg_btn.isEnabled()  # no link until one resolves
    view.deleteLater()


def test_ebay_button_opens_derived_sold_search(app, monkeypatch):
    view = _view(app)
    row = view._add_card_row()
    assert not row.ebay_btn.isEnabled()  # needs at least a name
    row.name.setText("Raticate")
    row.set.setText("Perfect Order")
    row.number.setText("99/88")
    assert row.ebay_btn.isEnabled()

    opened: list[str] = []
    monkeypatch.setattr(
        "tcginventory.ui.views.auctions.QDesktopServices",
        type("D", (), {"openUrl": staticmethod(lambda u: opened.append(u.toString()))}),
    )
    row._open_ebay()
    assert len(opened) == 1
    assert "_nkw=Raticate+Perfect+Order+99%2F88" in opened[0]
    assert "LH_Sold=1" in opened[0]
    view.deleteLater()


def test_condition_flows_into_ebay_search_and_export(app, monkeypatch):
    from tcginventory.domain.enums import Condition

    view = _view(app)
    row = view._add_card_row()
    row.name.setText("Raticate")
    row.set.setText("Perfect Order")
    row.number.setText("99/88")
    row.condition.setCurrentIndex(row.condition.findData(Condition.LP.value))

    # Condition rides into the eBay sold-search…
    opened: list[str] = []
    monkeypatch.setattr(
        "tcginventory.ui.views.auctions.QDesktopServices",
        type("D", (), {"openUrl": staticmethod(lambda u: opened.append(u.toString()))}),
    )
    row._open_ebay()
    assert opened[0].endswith("99%2F88+LP&_sacat=0&LH_Sold=1")

    # …and into the exported Transaction incoming card.
    assert row.to_card().condition == "LP"
    captured = {}
    view.export_to_transaction.connect(lambda p: captured.update(cards=p[1]))
    view._export_current()
    assert captured["cards"][0].item.condition.value == "LP"
    view.deleteLater()


def test_live_price_overrides_stale_history_provisional(app):
    # The cached history value can be stale/mis-resolved (regression: Meowth WoTC
    # promo cached at $5.52); the authoritative live product price corrects it.
    view = _view(app)
    row = view._add_card_row()
    row.set_market_cents(552)  # instant history provisional
    assert row.market.value() == 5.52
    view._on_price_ready(row, None, _variants(6231, "http://t"))  # live product price
    assert row.market.value() == 62.31
    view.deleteLater()


def test_live_price_respects_manual_override(app):
    view = _view(app)
    row = view._add_card_row()
    row.set_market_cents(552)
    row.market.setValue(80.00)  # user hand-edits before the live result lands
    assert row.market_is_manual is True
    view._on_price_ready(row, None, _variants(6231, "http://t"))
    assert row.market.value() == 80.00  # manual edit preserved
    view.deleteLater()


# ---------------------------------------------------------------------------
# full parity + export + archive
# ---------------------------------------------------------------------------


def test_row_minimal_roundtrip(app):
    view = _view(app)
    ac = AuctionCard(
        name="Blastoise",
        set_name="Base",
        number="2/102",
        language="EN",
        quantity=3,
        market_value_cents=4200,
        tcgplayer_url="http://t",
    )
    row = view._add_card_row()
    row.set_card(ac)
    out = row.to_card()
    assert (out.name, out.set_name, out.number) == ("Blastoise", "Base", "2/102")
    assert (out.quantity, out.market_value_cents) == (3, 4200)
    assert out.tcgplayer_url == "http://t"
    assert out.market_is_manual is False  # loaded value, untouched
    view.deleteLater()


def test_manual_market_edit_flags_review_and_notes_on_export(app):
    view = _view(app)
    row = view._add_card_row()
    row.name.setText("Charizard")
    # Auto price fills (not manual), then the user overrides it (manual).
    view._on_price_ready(row, None, _variants(2000, "http://t"))
    assert row.to_card().market_is_manual is False
    row.market.setValue(12.00)
    assert row.to_card().market_is_manual is True

    captured = {}
    view.export_to_transaction.connect(lambda p: captured.update(cards=p[1]))
    view._export_current()
    inc = captured["cards"][0]
    assert inc.market_value_cents == 1200  # the manual value is carried
    assert inc.review is True  # marked for review so the poll won't overwrite
    assert "manually set" in (inc.item.notes or "").lower()  # noted
    view.deleteLater()


def test_auto_price_export_is_not_flagged(app):
    view = _view(app)
    row = view._add_card_row()
    row.name.setText("Pikachu")
    view._on_price_ready(row, None, _variants(500, "http://t"))
    captured = {}
    view.export_to_transaction.connect(lambda p: captured.update(cards=p[1]))
    view._export_current()
    inc = captured["cards"][0]
    assert inc.market_value_cents == 500
    assert inc.review is False and not inc.item.notes  # nothing manual → plain
    view.deleteLater()


def test_main_window_export_populates_transaction(app):
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.main_window import MainWindow

    conn = connect(":memory:")
    migrate(conn)
    win = MainWindow(conn)
    win._auctions._add_card_row().set_card(
        AuctionCard(
            name="Charizard",
            set_name="Base",
            number="4/102",
            quantity=2,
            market_value_cents=25000,
        )
    )
    win._auctions._title.setText("Vintage lot")
    win._auctions._export_current()

    tx = win._transaction
    assert win._nav.currentRow() == 1  # switched to the Transaction tab
    assert len(tx._receive_rows) == 1
    r = tx._receive_rows[0]
    assert r.name.text() == "Charizard"  # condition/intent set in Transaction
    assert r.qty.value() == 2 and r.market.value() == 250.0
    assert tx._counterparty.text() == "eBay" and tx._reference.text() == "Vintage lot"
    assert tx._cash_out.value() == 0.0  # winning bid left blank for the user
    win.deleteLater()


def test_archive_toggle_and_filter(app):
    view = _view(app)
    view._add_card_row().set_card(AuctionCard(name="X", market_value_cents=100))
    view._save()
    assert len(view._cards) == 1
    view._toggle_archived(view._repo.get(view._edit_id))
    assert len(view._cards) == 0  # archived → hidden from the active list
    view._show_archived.setChecked(True)
    assert len(view._cards) == 1  # revealed under the Archived segment
    view.deleteLater()


# ---------------------------------------------------------------------------
# end time / countdown
# ---------------------------------------------------------------------------


def test_countdown_text_formats():
    from datetime import datetime

    from tcginventory.ui.views.auctions import _countdown_text

    now = datetime(2026, 7, 17, 12, 0, 0)
    assert _countdown_text(None, now) == "—"
    assert _countdown_text("not-a-date", now) == "—"
    assert _countdown_text("2026-07-17T11:00:00", now) == "ENDED"  # past
    assert _countdown_text("2026-07-17T12:00:00", now) == "ENDED"  # exactly now
    assert _countdown_text("2026-07-17T12:00:30", now) == "00:00:30"
    assert _countdown_text("2026-07-19T14:01:05", now) == "2d 02:01:05"


def test_auction_ends_at_roundtrip(auctions_conn):
    repo = AuctionRepository(auctions_conn)
    a = Auction(title="t", ends_at="2026-07-20T10:00:00", cards=[AuctionCard(name="x")])
    repo.save(a, now="2026-07-17T00:00:00+00:00")
    assert repo.get(a.id).ends_at == "2026-07-20T10:00:00"


def test_ensure_columns_adds_ends_at_to_old_db():
    import sqlite3

    from tcginventory.data.auctions import _ensure_columns

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE auction (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE auction_card (id INTEGER PRIMARY KEY, name TEXT)")
    _ensure_columns(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(auction)")}
    assert "ends_at" in cols
    conn.close()


def test_editor_end_date_and_time_are_separate_fields(app):
    from PySide6.QtCore import QDate, QDateTime, QTime

    view = _view(app)
    row = view._add_card_row()
    row.name.setText("Zard")
    row.market.setValue(10.0)
    # Two independent fields; tomorrow at 14:30.
    tomorrow = QDate.currentDate().addDays(1)
    view._ends_date.setDate(tomorrow)
    view._ends_time.setTime(QTime(14, 30))
    view._save()

    ends_at = view._repo.get(view._edit_id).ends_at
    assert ends_at == QDateTime(tomorrow, QTime(14, 30)).toString("yyyy-MM-ddTHH:mm:ss")
    view._on_tick()
    text = view._cards[0].countdown_text()
    assert text not in ("ENDED", "—")  # a running countdown
    assert view._countdown.text() not in ("ENDED", "—")  # editor label too
    view.deleteLater()


def test_watchlist_shows_ended_for_past_auction(app):
    view = _view(app)
    a = Auction(
        title="old",
        ends_at="2000-01-01T00:00:00",  # long past
        cards=[AuctionCard(name="x", market_value_cents=100)],
    )
    view._repo.save(a, now="2026-07-17T00:00:00+00:00")
    view.refresh()
    view._on_tick()
    assert view._cards[0].countdown_text() == "ENDED"
    view.deleteLater()


def test_countdown_timer_runs_without_needing_show(app):
    view = _view(app)
    # Always-on so a countdown flips to ENDED live, without a refresh/nav.
    assert view._tick_timer.isActive()
    view.deleteLater()


def test_is_ended_helper():
    from datetime import datetime

    from tcginventory.ui.views.auctions import _is_ended

    now = datetime(2026, 7, 20, 12, 0, 0)
    assert _is_ended("2026-07-20T11:59:59", now)  # past
    assert not _is_ended("2026-07-20T12:00:01", now)  # future
    assert not _is_ended(None, now)  # no end time
    assert not _is_ended("garbage", now)


def test_watchlist_active_first_then_no_end_then_ended(app):
    from datetime import datetime, timedelta

    view = _view(app)
    soon = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
    late = (datetime.now() + timedelta(days=5)).isoformat(timespec="seconds")
    for title, ends in [
        ("late", late),
        ("soon", soon),
        ("no-end", None),
        ("ended", "2000-01-01T00:00:00"),
    ]:
        view._repo.save(
            Auction(title=title, ends_at=ends, cards=[AuctionCard(name="x")])
        )
    view.refresh()
    order = [c.title_text() for c in view._cards]
    assert order == ["soon", "late", "no-end", "ended"]
    view.deleteLater()


def test_tick_resorts_when_ended_set_changes(app):
    from datetime import datetime, timedelta

    view = _view(app)
    future = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
    view._repo.save(Auction(title="a", ends_at=future, cards=[AuctionCard(name="x")]))
    view.refresh()
    assert view._ended_ids == set()
    view._ended_ids = {999_999}  # pretend the ended set is stale
    view._on_tick()  # detects the mismatch and re-sorts (recomputes _ended_ids)
    assert view._ended_ids == set()
    view.deleteLater()


def test_add_button_is_the_bottom_card_row(app):
    from PySide6.QtWidgets import QPushButton

    view = _view(app)

    def add_is_last() -> bool:
        t = view._card_table
        w = t.cellWidget(t.rowCount() - 1, 0)
        return isinstance(w, QPushButton) and not w.icon().isNull()

    assert view._card_table.rowCount() == 1  # just the add-row
    assert add_is_last()
    view._add_card_row()
    assert view._card_table.rowCount() == 2  # 1 card + add-row
    assert add_is_last()
    view.deleteLater()


def test_add_button_cannot_be_clipped_by_button_padding(app):
    """Regression: it was a "＋" *label*, and the app stylesheet's 14px of side
    padding left ~4px for the glyph inside a 32px cell — so it rendered as a
    sliver. An icon has no text to clip."""
    view = _view(app)
    table = view._card_table
    add = table.cellWidget(table.rowCount() - 1, 0)

    assert add.text() == "" and not add.icon().isNull()
    assert add.width() >= add.iconSize().width()
    view.deleteLater()


def test_card_columns_are_ordered_identity_printing_money_links(app):
    """Cond / Holo / Printing sit together: the three fields that decide *which*
    printing a row is."""
    from tcginventory.ui.views import auctions as auc

    assert auc._CARD_HEADERS == [
        "Name",
        "Set",
        "#",
        "Cond",
        "Holo",
        "Printing",
        "Market",
        "Qty",
        "TCG",
        "eBay",
        "",
    ]
    # widgets() is placed by index, so it must track the headers exactly.
    view = _view(app)
    row = view._add_card_row()
    assert len(row.widgets()) == len(auc._CARD_HEADERS) - 1  # last col is delete
    assert row.widgets()[3] is row.condition
    assert row.widgets()[4] is row.holo
    assert row.widgets()[5] is row.printing
    table = view._card_table
    for col, widget in enumerate(row.widgets()):
        assert table.cellWidget(0, col) is widget, auc._CARD_HEADERS[col]
    view.deleteLater()


def test_identity_columns_absorb_the_slack_so_nothing_is_cut_off(app):
    """Regression: fixed widths summed to more than the pane, so the right-hand
    columns fell off and the horizontal scrollbar ate the ＋ row."""
    from tcginventory.ui.views import auctions as auc

    view = _view(app)
    view._add_card_row()
    table = view._card_table

    def total() -> int:
        return sum(
            table.columnWidth(c)
            for c in range(table.columnCount())
            if not table.isColumnHidden(c)
        )

    table.resize(900, 220)  # a pane with room to spare
    app.processEvents()  # the viewport only recomputes on the event pass
    view._fit_card_columns()
    assert total() == table.viewport().width()  # fills it exactly: no dead space
    assert table.columnWidth(auc._NAME_COL) > auc._NAME_MIN

    # Squeezed narrower than the columns need, Name stops at its floor rather
    # than collapsing — scrolling is better than an unusable name field.
    table.resize(420, 220)
    app.processEvents()
    view._fit_card_columns()
    assert table.columnWidth(auc._NAME_COL) == auc._NAME_MIN
    for col, (floor, _cap, _share) in auc._FLEX_COLS.items():
        assert table.columnWidth(col) == floor, auc._CARD_HEADERS[col]
    view.deleteLater()


def test_spare_width_is_shared_with_set_and_number(app):
    """Name used to take every spare pixel — roughly 345px on a maximised
    window, near triple the longest name typed, while Set truncated at 108px.
    The three identity fields now split the surplus."""
    from tcginventory.ui.views import auctions as auc

    view = _view(app)
    view._add_card_row()
    table = view._card_table
    table.resize(960, 220)
    app.processEvents()
    view._fit_card_columns()

    name = table.columnWidth(auc._NAME_COL)
    card_set = table.columnWidth(auc._SET_COL)
    number = table.columnWidth(auc._CARD_HEADERS.index("#"))
    floors = {col: f for col, (f, _c, _s) in auc._FLEX_COLS.items()}

    # Every one of them grew, and Name no longer swallows the pane: what it
    # gained is smaller than what Set and # gained together.
    assert card_set > floors[auc._SET_COL]
    assert number > floors[auc._CARD_HEADERS.index("#")]
    gained_by_name = name - floors[auc._NAME_COL]
    gained_by_rest = (card_set - floors[auc._SET_COL]) + (
        number - floors[auc._CARD_HEADERS.index("#")]
    )
    assert 0 < gained_by_name < gained_by_rest
    assert card_set > name  # set names run longest, so Set ends up widest


def test_a_very_wide_pane_caps_name_and_number_and_gives_set_the_rest(app):
    """Ceilings stop Name and # from ballooning past anything they could hold;
    Set is the sink, so the table still ends flush with the pane."""
    from tcginventory.ui.views import auctions as auc

    view = _view(app)
    view._add_card_row()
    table = view._card_table
    table.resize(1400, 220)
    app.processEvents()
    view._fit_card_columns()

    assert table.columnWidth(auc._NAME_COL) == auc._FLEX_COLS[auc._NAME_COL][1]
    num_col = auc._CARD_HEADERS.index("#")
    assert table.columnWidth(num_col) == auc._FLEX_COLS[num_col][1]
    total = sum(
        table.columnWidth(c)
        for c in range(table.columnCount())
        if not table.isColumnHidden(c)
    )
    assert total == table.viewport().width()
    view.deleteLater()


def test_printing_column_appears_only_when_a_row_has_printings(app):
    from tcginventory.ui.views import auctions as auc

    class _Variant:
        def __init__(self, label):
            self.label, self.tcgplayer_url, self.price_cents = label, None, 500

    view = _view(app)
    row = view._add_card_row()
    assert view._card_table.isColumnHidden(auc._PRINTING_COL)

    row.set_variants([_Variant("Normal"), _Variant("Reverse Holofoil")])
    view._sync_printing_column()
    assert not view._card_table.isColumnHidden(auc._PRINTING_COL)

    # A single printing is no choice at all, so the column goes away again.
    row.set_variants([_Variant("Normal")])
    view._sync_printing_column()
    assert view._card_table.isColumnHidden(auc._PRINTING_COL)
    view.deleteLater()


def test_holo_round_trips_through_a_saved_card(app):
    from tcginventory.domain.enums import HoloPattern
    from tcginventory.ui.views import auctions as auc

    view = _view(app)
    row = view._add_card_row()
    row.name.setText("Charizard ex")
    assert row.holo.currentData() == HoloPattern.NON_HOLO.value  # sane default

    auc._select(row.holo, HoloPattern.REVERSE_HOLO.value)
    card = row.to_card()
    assert card.holo_pattern == HoloPattern.REVERSE_HOLO.value

    restored = view._add_card_row()
    restored.set_card(card)
    assert restored.holo.currentData() == HoloPattern.REVERSE_HOLO.value
    view.deleteLater()


def test_holo_survives_a_save_and_reload(app):
    """It reaches the exported purchase, so it has to persist with the auction."""
    from tcginventory.domain.enums import HoloPattern
    from tcginventory.ui.views import auctions as auc

    view = _view(app)
    row = view._add_card_row()
    row.name.setText("Charizard ex")
    auc._select(row.holo, HoloPattern.HOLO.value)
    view._title.setText("holo lot")
    view._save()

    saved = view._repo.list_all()[0]
    assert saved.cards[0].holo_pattern == HoloPattern.HOLO.value

    view._load_auction(saved.id)
    assert view._rows[0].holo.currentData() == HoloPattern.HOLO.value
    view.deleteLater()


def test_hover_watchlist_shows_auction_cards(app):
    view = _view(app)
    view._repo.save(
        Auction(
            title="lot",
            cards=[AuctionCard(name="A", card_id="x", image_url="http://i/x.png")],
        )
    )
    view.refresh()
    captured: dict = {}
    view._gallery.set_cards = lambda entries: captured.__setitem__("entries", entries)
    view._gallery.show_gallery = lambda: captured.__setitem__("shown", True)
    view._on_card_hover(view._cards[0])
    assert captured.get("shown") is True
    assert [e[0] for e in captured["entries"]] == ["x"]
    view.deleteLater()
