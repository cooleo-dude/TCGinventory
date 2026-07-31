"""Calendar: personal-entry CRUD, conflict detection, and the view."""

from __future__ import annotations

import os

import pytest

from tcginventory.data.repositories import CalendarEntryRepository, CardShowRepository
from tcginventory.domain.models import CalendarEntry, CardShow
from tcginventory.services.calendar import CalendarService, find_conflicts


def _vac(start="2026-08-12", end="2026-08-16", **kw) -> CalendarEntry:
    return CalendarEntry(
        title=kw.pop("title", "Outer Banks"),
        kind=kw.pop("kind", "vacation"),
        starts_on=start,
        ends_on=end,
        **kw,
    )


def test_calendar_entry_crud_and_overlap(conn):
    repo = CalendarEntryRepository(conn)
    e = repo.add(_vac(color="#a78bfa"))
    conn.commit()
    assert e.id is not None

    got = repo.get(e.id)
    assert got.title == "Outer Banks"
    assert got.all_day is True and got.color == "#a78bfa"

    e.title = "Beach week"
    repo.update(e)
    conn.commit()
    assert repo.get(e.id).title == "Beach week"

    assert len(repo.list_overlapping("2026-08-01", "2026-08-31")) == 1
    assert repo.list_overlapping("2026-09-01", "2026-09-30") == []  # disjoint

    repo.delete(e.id)
    conn.commit()
    assert repo.get(e.id) is None


def test_find_conflicts_only_flags_busy_shows_inside_a_span():
    entries = [_vac()]  # Aug 12–16
    shows = [
        CardShow(source_id=1, name="A", state="DE", starts_on="2026-08-16",
                 status="attending"),  # inside + busy → conflict
        CardShow(source_id=2, name="B", state="PA", starts_on="2026-08-14",
                 status="pending"),  # inside but not "busy" → no conflict
        CardShow(source_id=3, name="C", state="NJ", starts_on="2026-08-20",
                 status="walking"),  # busy but outside the span → no conflict
    ]
    ids, dates = find_conflicts(shows, entries)
    assert ids == {1}
    assert dates == {"2026-08-16"}


def test_calendar_service_entry_lifecycle(conn):
    svc = CalendarService(conn)
    e = svc.add_entry(
        CalendarEntry(title="Dentist", kind="obligation",
                      starts_on="2026-08-21", ends_on="2026-08-21")
    )
    assert [x.title for x in svc.entries_overlapping("2026-08-01", "2026-08-31")] == [
        "Dentist"
    ]
    svc.delete_entry(e.id)
    assert svc.entries_overlapping("2026-08-01", "2026-08-31") == []


# -- view smoke -------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_calendar_view_flags_a_conflict(app, conn, tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.ui.views.calendar import CalendarView

    CardShowRepository(conn).upsert(
        CardShow(source_id=1, name="Delaware TCG Meet", state="NJ",
                 starts_on="2026-08-16", fetched_at="t")
    )
    conn.execute("INSERT INTO card_show_status(source_id,status) VALUES(1,'attending')")
    CalendarEntryRepository(conn).add(_vac())  # Aug 12–16 covers the show
    conn.commit()

    view = CalendarView(conn)
    view._year, view._month = 2026, 8
    view.reload()
    assert 1 in view._conflict_ids
    assert not view._banner.isHidden()  # conflict banner shown
    assert "conflict" in view._banner.text().lower()
    view.deleteLater()
