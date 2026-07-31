"""Calendar aggregation + conflict detection (Qt-free).

Combines scraped card shows with the user's personal calendar entries and flags
double-bookings — a show you're **attending** or **walking** that falls inside a
personal entry's date span. Kept isolated and testable headless.
"""

from __future__ import annotations

import sqlite3

from tcginventory.data.repositories import CalendarEntryRepository, CardShowRepository
from tcginventory.domain.models import CalendarEntry, CardShow

# Selectable personal-entry kinds (order = the Add-entry dropdown order).
ENTRY_KINDS = ("vacation", "obligation", "travel", "other")

# Statuses that mean "you'll be there" — only these count for a conflict.
_BUSY = ("attending", "walking")


def find_conflicts(
    shows: list[CardShow], entries: list[CalendarEntry]
) -> tuple[set[int], set[str]]:
    """Return ``(conflicting show source_ids, conflict dates)``.

    A show conflicts when you're attending/walking it *and* its date falls within
    some personal entry's inclusive ``[starts_on, ends_on]`` span."""
    spans = [(e.starts_on, e.ends_on) for e in entries]
    conflict_ids: set[int] = set()
    conflict_dates: set[str] = set()
    for show in shows:
        if show.status not in _BUSY:
            continue
        for start, end in spans:
            if start <= show.starts_on <= end:
                conflict_ids.add(show.source_id)
                conflict_dates.add(show.starts_on)
                break
    return conflict_ids, conflict_dates


class CalendarService:
    """Thin coordinator over the show + entry repositories (owns commits)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.shows = CardShowRepository(conn)
        self.entries = CalendarEntryRepository(conn)

    def entries_overlapping(self, start: str, end: str) -> list[CalendarEntry]:
        return self.entries.list_overlapping(start, end)

    def add_entry(self, entry: CalendarEntry) -> CalendarEntry:
        saved = self.entries.add(entry)
        self.conn.commit()
        return saved

    def update_entry(self, entry: CalendarEntry) -> CalendarEntry:
        self.entries.update(entry)
        self.conn.commit()
        return entry

    def delete_entry(self, entry_id: int) -> None:
        self.entries.delete(entry_id)
        self.conn.commit()
