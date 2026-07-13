"""Card-show scraping: parser + refresh service (offline, injected fetcher)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tcginventory.data.repositories import CardShowRepository
from tcginventory.domain.models import CardShow
from tcginventory.services.cardshows import (
    CardShowService,
    is_tcg,
    list_url,
    parse_shows,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "tcdb_nj.html"


def _nj_html() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def test_list_url_encodes_state_and_display():
    url = list_url("NJ")
    assert "State=NJ" in url
    assert "Display=New+Jersey" in url
    assert "MODE=Location" in url


def test_parse_shows_extracts_all_fields():
    shows = {s.source_id: s for s in parse_shows(_nj_html(), "NJ", fetched_at="t")}
    assert len(shows) == 4  # 1 past + 3 future

    g = shows[28222]
    assert g.name == "Garden State Trading Card Show"
    assert g.venue == "Baymont by Wyndham"
    assert g.city == "Pompton Plains"
    assert g.state == "NJ"
    assert g.starts_on == "2026-07-14"
    assert (g.start_time, g.end_time) == ("3:00 PM", "9:00 PM")
    assert g.detail_url == "https://www.tcdb.com/CardShows.cfm?MODE=VIEW&ID=28222"

    # A show with no time line still parses (times None), entities unescaped.
    t = shows[28205]
    assert t.venue == "La Quinta Inn & Suites"
    assert t.city == "Secaucus"
    assert t.start_time is None and t.end_time is None


def test_is_tcg_keyword_heuristic():
    shows = {s.source_id: s for s in parse_shows(_nj_html(), "NJ")}
    assert is_tcg(shows[20227]) is True  # "…Pokemon, and TCG…"
    assert is_tcg(shows[28222]) is False  # "…Trading Card Show" (no keyword)


def test_refresh_stores_future_prunes_past_and_is_idempotent(conn):
    svc = CardShowService(conn, fetch_html=lambda url: _nj_html())
    summary = svc.refresh(["NJ"], today="2026-06-01", fetched_at="2026-06-01T00:00:00Z")

    assert summary.fetched == 3  # the 2025 past show is excluded
    assert summary.errors == []
    stored = CardShowRepository(conn).list_for_states(["NJ"])
    assert len(stored) == 3
    assert 1001 not in {s.source_id for s in stored}  # past pruned

    # Re-running replaces the state's set — no duplicates.
    svc.refresh(["NJ"], today="2026-06-01")
    assert len(CardShowRepository(conn).list_for_states(["NJ"])) == 3


def test_refresh_captures_fetch_errors_without_raising(conn):
    def boom(url):
        raise RuntimeError("403 Forbidden")

    summary = CardShowService(conn, fetch_html=boom).refresh(["NJ"])
    assert summary.fetched == 0
    assert summary.errors and "NJ" in summary.errors[0]


def test_repo_list_between(conn):
    CardShowService(conn, fetch_html=lambda url: _nj_html()).refresh(
        ["NJ"], today="2026-01-01"
    )
    repo = CardShowRepository(conn)
    only_14th = repo.list_between("2026-07-14", "2026-07-14")
    assert {s.source_id for s in only_14th} == {28222, 20227}


@pytest.fixture()
def app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_status_persists_across_refresh(conn):
    svc = CardShowService(conn, fetch_html=lambda url: _nj_html())
    svc.refresh(["NJ"], today="2026-01-01")
    repo = CardShowRepository(conn)

    repo.set_status(28222, "attending")
    conn.commit()
    shows = {s.source_id: s for s in repo.list_for_states(["NJ"])}
    assert shows[28222].status == "attending"
    assert shows[20227].status is None  # untouched show has no status

    # A re-scrape wipes/reinserts card_show rows but keeps the status.
    svc.refresh(["NJ"], today="2026-01-01")
    shows = {s.source_id: s for s in repo.list_for_states(["NJ"])}
    assert shows[28222].status == "attending"

    repo.set_status(28222, None)  # clearing removes it
    conn.commit()
    shows = {s.source_id: s for s in repo.list_for_states(["NJ"])}
    assert shows[28222].status is None


def test_shows_view_status_button_marks_show(app, conn, tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from PySide6.QtCore import QDate
    from PySide6.QtWidgets import QToolButton

    from tcginventory.ui.views.shows import ShowsView

    repo = CardShowRepository(conn)
    repo.upsert(
        CardShow(
            source_id=5, name="Show A", state="NJ", starts_on="2026-07-14",
            detail_url="https://x", fetched_at="t",
        )
    )
    conn.commit()

    view = ShowsView(conn)
    view._calendar.setSelectedDate(QDate(2026, 7, 14))
    view._update_day_list()
    row = view._day_list.itemWidget(view._day_list.item(0))
    buttons = row.findChildren(QToolButton)
    assert len(buttons) == 4  # denied / pending / attending / walking

    buttons[0].click()  # STATUS_ORDER[0] == attending
    assert repo.list_for_states(["NJ"])[0].status == "attending"
    assert view._calendar.selectedDate() == QDate(2026, 7, 14)  # no month jump

    # Clicking the active colour again clears it.
    row = view._day_list.itemWidget(view._day_list.item(0))
    row.findChildren(QToolButton)[0].click()
    assert repo.list_for_states(["NJ"])[0].status is None
    view.deleteLater()


def test_shows_view_renders_cells_and_day_list(app, conn, tmp_path, monkeypatch):
    # Isolate prefs to a temp dir (don't touch the real preferences.json).
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from PySide6.QtCore import QDate

    from tcginventory.ui.views.shows import ShowsView

    repo = CardShowRepository(conn)
    repo.upsert(
        CardShow(
            source_id=1,
            name="Garden State Show",
            state="NJ",
            starts_on="2026-07-14",
            venue="Baymont",
            city="Pompton Plains",
            start_time="3:00 PM",
            end_time="9:00 PM",
            detail_url="https://www.tcdb.com/x",
            fetched_at="t",
        )
    )
    repo.upsert(
        CardShow(
            source_id=2,
            name="Pokemon TCG Show",
            state="NJ",
            starts_on="2026-07-14",
            city="Newark",
            fetched_at="t",
        )
    )
    conn.commit()

    view = ShowsView(conn)  # :memory: → no auto-refresh / network
    day = QDate(2026, 7, 14)
    assert len(view._calendar.shows_on(day)) == 2

    view._calendar.setSelectedDate(day)
    view._update_day_list()
    assert view._day_list.count() == 2

    # "TCG only" keeps just the Pokemon show.
    view._tcg_toggle.setChecked(True)
    view._calendar.setSelectedDate(day)
    view._update_day_list()
    assert view._day_list.count() == 1
    view.deleteLater()
