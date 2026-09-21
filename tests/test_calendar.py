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
        CardShow(
            source_id=1,
            name="A",
            state="DE",
            starts_on="2026-08-16",
            status="attending",
        ),  # inside + busy → conflict
        CardShow(
            source_id=2, name="B", state="PA", starts_on="2026-08-14", status="pending"
        ),  # inside but not "busy" → no conflict
        CardShow(
            source_id=3, name="C", state="NJ", starts_on="2026-08-20", status="walking"
        ),  # busy but outside the span → no conflict
    ]
    ids, dates = find_conflicts(shows, entries)
    assert ids == {1}
    assert dates == {"2026-08-16"}


def test_calendar_service_entry_lifecycle(conn):
    svc = CalendarService(conn)
    e = svc.add_entry(
        CalendarEntry(
            title="Dentist",
            kind="obligation",
            starts_on="2026-08-21",
            ends_on="2026-08-21",
        )
    )
    assert [x.title for x in svc.entries_overlapping("2026-08-01", "2026-08-31")] == [
        "Dentist"
    ]
    svc.delete_entry(e.id)
    assert svc.entries_overlapping("2026-08-01", "2026-08-31") == []


# -- view smoke -------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _agenda_texts(view) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [w.text() for w in view._agenda_body.findChildren(QLabel)]


def test_agenda_lists_only_marked_shows_and_entries(app, conn, tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.ui.views.calendar import CalendarView

    repo = CardShowRepository(conn)
    repo.upsert(
        CardShow(
            source_id=1,
            name="Marked Show",
            state="NJ",
            starts_on="2026-08-10",
            fetched_at="t",
        )
    )
    repo.upsert(
        CardShow(
            source_id=2,
            name="Unmarked Show",
            state="NJ",
            starts_on="2026-08-11",
            fetched_at="t",
        )
    )
    conn.execute("INSERT INTO card_show_status(source_id,status) VALUES(1,'walking')")
    CalendarEntryRepository(conn).add(
        _vac(title="Personal Note", start="2026-08-12", end="2026-08-12")
    )
    conn.commit()

    view = CalendarView(conn)
    view._year, view._month, view._states = 2026, 8, ["NJ"]
    view.reload()

    view._selected = "2026-08-01"  # a day with nothing on it
    view.reload()

    texts = _agenda_texts(view)
    assert any("Marked Show" in x for x in texts)  # attending/walking → shown
    assert any("Personal Note" in x for x in texts)  # personal entry → shown
    assert not any("Unmarked Show" in x for x in texts)  # scraped, unmarked → hidden
    assert any("No shows on this day" in x for x in texts)

    # Selecting the unmarked show's day surfaces it in the "Selected day" block —
    # the only route to a show you have not marked yet (ticket 75).
    view._on_day_selected("2026-08-11")
    texts = _agenda_texts(view)
    assert any("Unmarked Show" in x for x in texts)
    assert not any("No shows on this day" in x for x in texts)
    view.deleteLater()


def _show_row_buttons(view, name: str):
    """The four status buttons on the agenda row whose title is ``name``."""
    from PySide6.QtWidgets import QLabel, QToolButton

    for lbl in view._agenda_body.findChildren(QLabel):
        if lbl.text() == name:
            return lbl.parent().findChildren(QToolButton)
    raise AssertionError(f"no agenda row titled {name!r}")


def test_selected_day_row_sets_status_on_an_unmarked_show(
    app, conn, tmp_path, monkeypatch
):
    """Regression (ticket 75): status buttons lived only on month-agenda rows,
    which listed only attending/walking shows — so an unmarked, pending or
    denied show had no reachable buttons and could never be marked."""
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.ui.views.calendar import CalendarView

    repo = CardShowRepository(conn)
    repo.upsert(
        CardShow(
            source_id=7,
            name="Fresh Show",
            state="NJ",
            starts_on="2026-08-22",
            fetched_at="t",
        )
    )
    conn.commit()

    view = CalendarView(conn)
    view._year, view._month, view._states = 2026, 8, ["NJ"]
    view._on_day_selected("2026-08-22")

    buttons = _show_row_buttons(view, "Fresh Show")
    assert len(buttons) == 4  # attending / walking / pending / denied
    buttons[0].click()  # _STATUS_ORDER[0] == attending
    assert repo.list_for_states(["NJ"])[0].status == "attending"
    assert view._selected == "2026-08-22"  # the day stays selected

    # Now marked, it appears twice: the selected-day block and the month block.
    texts = _agenda_texts(view)
    assert sum(1 for x in texts if x == "Fresh Show") == 2

    # Pending/denied are reachable too, and clicking the active colour clears it.
    _show_row_buttons(view, "Fresh Show")[2].click()  # pending
    assert repo.list_for_states(["NJ"])[0].status == "pending"
    assert sum(1 for x in _agenda_texts(view) if x == "Fresh Show") == 1
    _show_row_buttons(view, "Fresh Show")[2].click()
    assert repo.list_for_states(["NJ"])[0].status is None
    view.deleteLater()


def test_reloading_replaces_the_chips_instead_of_stacking_them(
    app, conn, tmp_path, monkeypatch
):
    """Regression: chips were dropped with deleteLater alone, which only queues
    the deletion — until the event loop ran they stayed visible children of the
    cell, so a second render drew a fresh set on top of ghosts. Every reload
    leaked ~89 widgets, and the month grid doubled up after any ledger change.
    """
    from PySide6.QtWidgets import QWidget

    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.ui.views.calendar import CalendarView

    repo = CardShowRepository(conn)
    for i in range(4):
        repo.upsert(
            CardShow(
                source_id=i + 1,
                name=f"Show {i}",
                state="NJ",
                starts_on=f"2026-08-1{i}",
                fetched_at="t",
            )
        )
    conn.execute("INSERT INTO card_show_status(source_id,status) VALUES(1,'attending')")
    CalendarEntryRepository(conn).add(_vac())
    conn.commit()

    view = CalendarView(conn)
    view._year, view._month, view._states = 2026, 8, ["NJ"]
    view.reload()
    after_first = len(view.findChildren(QWidget))

    for _ in range(3):
        view.reload()
    assert len(view.findChildren(QWidget)) == after_first
    view.deleteLater()


def test_calendar_view_flags_a_conflict(app, conn, tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.ui.views.calendar import CalendarView

    CardShowRepository(conn).upsert(
        CardShow(
            source_id=1,
            name="Delaware TCG Meet",
            state="NJ",
            starts_on="2026-08-16",
            fetched_at="t",
        )
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
