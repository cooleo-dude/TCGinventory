"""Calendar — a themed month grid + agenda for card shows and personal entries.

Scraped card shows (with attending/walking/pending/denied status) sit alongside
user-created **personal entries** (vacations, obligations, travel…). Overlaps
between a show you're attending/walking and a personal entry are flagged as a
**conflict** — on the day cell, in the agenda, and in a banner — so you don't
double-book. Replaces the old embedded Card-Shows calendar.
"""

from __future__ import annotations

import calendar as _cal
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

from PySide6.QtCore import QDate, QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tcginventory import config
from tcginventory.data.repositories import CardShowRepository
from tcginventory.domain.models import CalendarEntry, CardShow
from tcginventory.services.calendar import ENTRY_KINDS, CalendarService, find_conflicts
from tcginventory.services.cardshows import US_STATES, CardShowService
from tcginventory.ui import icons, theme

_DEFAULT_STATES = ["NJ"]
_DEFAULT_ENTRY_COLOR = "#a78bfa"  # violet — distinct from show statuses
_ENTRY_COLORS = ["#a78bfa", "#5b93a8", "#f6c233", "#83d15a", "#e0533a"]
_KIND_LABEL = {
    "vacation": "Vacation",
    "obligation": "Obligation",
    "travel": "Travel",
    "other": "Other",
}
_STATUS_ORDER = [
    ("attending", "Attending"),
    ("walking", "Walking"),
    ("pending", "Pending"),
    ("denied", "Denied"),
]
_WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_MAX_CELL_BARS = 3


def _status_color(status: str | None) -> str | None:
    t = theme.active()
    return {
        "attending": t.pos,
        "walking": t.info,
        "pending": t.warn,
        "denied": t.neg,
    }.get(status or "")


def _rgba(hex_color: str, a: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{a})"


def _qdate(iso: str) -> QDate:
    y, m, d = (int(x) for x in iso[:10].split("-"))
    return QDate(y, m, d)


def _db_file(conn: sqlite3.Connection) -> str | None:
    for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
        if name == "main":
            return file or None
    return None


class _RefreshWorker(QThread):
    """Re-scrape shows off the UI thread with its own connection."""

    done = Signal(object)

    def __init__(self, db_path: str, states: list[str]) -> None:
        super().__init__()
        self._db_path = db_path
        self._states = states

    def run(self) -> None:
        from tcginventory.data.db import open_database
        from tcginventory.services.cardshows import ShowRefreshSummary

        try:
            conn = open_database(self._db_path)
            try:
                summary = CardShowService(conn).refresh(self._states)
            finally:
                conn.close()
        except Exception as exc:  # never crash the thread
            summary = ShowRefreshSummary(errors=[str(exc)])
        self.done.emit(summary)


# ---------------------------------------------------------------------------
# Add / edit personal entry
# ---------------------------------------------------------------------------


class EntryDialog(QDialog):
    """Create or edit a personal calendar entry. ``deleted`` is set if the user
    pressed Delete on an existing entry."""

    def __init__(
        self,
        parent,
        entry: CalendarEntry | None = None,
        default_date: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._entry = entry
        self.deleted = False
        self._color = (entry.color if entry else None) or _DEFAULT_ENTRY_COLOR
        self.setWindowTitle("Edit entry" if entry else "New entry")
        self.setMinimumWidth(360)

        form = QGridLayout(self)
        form.setColumnStretch(1, 1)
        row = 0

        def add(label: str, widget) -> None:
            nonlocal row
            lab = QLabel(label)
            lab.setStyleSheet("font-weight:600;")
            form.addWidget(lab, row, 0, Qt.AlignRight | Qt.AlignVCenter)
            form.addWidget(widget, row, 1)
            row += 1

        self._title = QLineEdit(entry.title if entry else "")
        self._title.setPlaceholderText("e.g. Outer Banks — Vacation")
        add("Title", self._title)

        self._kind = QComboBox()
        for k in ENTRY_KINDS:
            self._kind.addItem(_KIND_LABEL[k], k)
        if entry:
            self._kind.setCurrentIndex(max(0, list(ENTRY_KINDS).index(entry.kind)))
        add("Type", self._kind)

        base = entry.starts_on if entry else (default_date or date.today().isoformat())
        self._start = QDateEdit(_qdate(base))
        self._start.setCalendarPopup(True)
        self._end = QDateEdit(_qdate(entry.ends_on if entry else base))
        self._end.setCalendarPopup(True)
        dates = QHBoxLayout()
        dates.setContentsMargins(0, 0, 0, 0)
        dates.addWidget(self._start)
        dates.addWidget(QLabel("to"))
        dates.addWidget(self._end)
        dwrap = QWidget()
        dwrap.setLayout(dates)
        add("Dates", dwrap)

        self._all_day = QCheckBox("All day")
        self._all_day.setChecked(entry.all_day if entry else True)
        self._time = QLineEdit(entry.start_time if entry and entry.start_time else "")
        self._time.setPlaceholderText("e.g. 2:00 PM")
        self._time.setEnabled(not self._all_day.isChecked())
        self._all_day.toggled.connect(lambda on: self._time.setEnabled(not on))
        timerow = QHBoxLayout()
        timerow.setContentsMargins(0, 0, 0, 0)
        timerow.addWidget(self._all_day)
        timerow.addWidget(self._time, 1)
        twrap = QWidget()
        twrap.setLayout(timerow)
        add("Time", twrap)

        self._swatches: list[QPushButton] = []
        swatch_row = QHBoxLayout()
        swatch_row.setContentsMargins(0, 0, 0, 0)
        swatch_row.setSpacing(6)
        for c in _ENTRY_COLORS:
            b = QPushButton()
            b.setFixedSize(22, 22)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, col=c: self._pick_color(col))
            self._swatches.append(b)
            swatch_row.addWidget(b)
        swatch_row.addStretch(1)
        cwrap = QWidget()
        cwrap.setLayout(swatch_row)
        add("Colour", cwrap)
        self._paint_swatches()

        self._notes = QPlainTextEdit(entry.notes if entry and entry.notes else "")
        self._notes.setFixedHeight(60)
        add("Notes", self._notes)

        buttons = QDialogButtonBox()
        if entry is not None:
            del_btn = buttons.addButton("Delete", QDialogButtonBox.DestructiveRole)
            del_btn.clicked.connect(self._on_delete)
        buttons.addButton(QDialogButtonBox.Cancel)
        save = buttons.addButton("Save entry", QDialogButtonBox.AcceptRole)
        save.setObjectName("primary")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons, row, 0, 1, 2)

    def _pick_color(self, color: str) -> None:
        self._color = color
        self._paint_swatches()

    def _paint_swatches(self) -> None:
        for b, c in zip(self._swatches, _ENTRY_COLORS, strict=True):
            sel = "border:2px solid #ffffff;" if c == self._color else "border:none;"
            b.setStyleSheet(f"background:{c};border-radius:6px;{sel}")

    def _on_delete(self) -> None:
        self.deleted = True
        self.accept()

    def _on_accept(self) -> None:
        if not self.deleted and not self._title.text().strip():
            self._title.setFocus()
            return  # title required
        # keep start <= end
        if self._end.date() < self._start.date():
            self._end.setDate(self._start.date())
        self.accept()

    def result_entry(self) -> CalendarEntry:
        e = self._entry or CalendarEntry(
            title="", kind="other", starts_on="", ends_on=""
        )
        e.title = self._title.text().strip()
        e.kind = self._kind.currentData()
        e.starts_on = self._start.date().toString("yyyy-MM-dd")
        e.ends_on = self._end.date().toString("yyyy-MM-dd")
        e.all_day = self._all_day.isChecked()
        e.start_time = None if e.all_day else (self._time.text().strip() or None)
        e.color = self._color
        e.notes = self._notes.toPlainText().strip() or None
        return e


# ---------------------------------------------------------------------------
# Month grid
# ---------------------------------------------------------------------------


class _DayCell(QFrame):
    clicked = Signal(str)  # ISO date

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("daycell")
        self._iso = ""
        self._in_month = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(5, 4, 5, 4)
        lay.setSpacing(2)
        self._num = QLabel("")
        self._num.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        lay.addWidget(self._num)
        self._events = QVBoxLayout()
        self._events.setContentsMargins(0, 0, 0, 0)
        self._events.setSpacing(2)
        lay.addLayout(self._events)
        lay.addStretch(1)
        self.setMinimumHeight(88)

    def _clear_events(self) -> None:
        while self._events.count():
            it = self._events.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def render(
        self,
        iso: str,
        day_num: int,
        in_month: bool,
        weekend: bool,
        selected: bool,
        today: bool,
        conflict: bool,
        bars: list[tuple[str, str, bool]],
    ) -> None:
        t = theme.active()
        self._iso = iso
        self._in_month = in_month
        self._clear_events()
        if not in_month:
            self.setStyleSheet("QFrame#daycell{background:transparent;border:none;}")
            self._num.setText("")
            return

        bg = t.zebra if weekend else t.surface
        border = t.line
        extra = ""
        if conflict:
            border = t.neg
        if selected:
            extra = f"QFrame#daycell{{border:1px solid {t.accent};}}"
        self.setStyleSheet(
            f"QFrame#daycell{{background:{bg};border:1px solid {border};"
            f"border-radius:9px;}}" + extra
        )
        num_color = t.accent if selected else (t.accent if today else t.ink2)
        warn = ""
        if conflict:
            warn = (
                f"  <span style='color:{t.neg};font-weight:800'>&#9888;</span>"
            )
        self._num.setText(
            f"<span style='color:{num_color};font-weight:700;font-family:monospace'>"
            f"{day_num}</span>{warn}"
        )

        shown = bars[:_MAX_CELL_BARS]
        for color, text, band in shown:
            self._events.addWidget(_bar(color, text, band))
        if len(bars) > _MAX_CELL_BARS:
            more = QLabel(f"+{len(bars) - _MAX_CELL_BARS} more")
            more.setStyleSheet(f"color:{t.ink3};font-size:10px;padding-left:2px;")
            self._events.addWidget(more)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._in_month and self._iso:
            self.clicked.emit(self._iso)
        super().mousePressEvent(event)


def _bar(color: str, text: str, band: bool) -> QLabel:
    """A single event bar in a day cell. ``band`` = a filled (multi-day) block."""
    lbl = QLabel(text)
    lbl.setToolTip(text)
    lbl.setFixedHeight(15)
    if band:
        lbl.setStyleSheet(
            f"background:{_rgba(color, 0.20)};color:{color};font-size:10px;"
            f"font-weight:700;border-left:2px solid {color};"
            "border-radius:3px;padding:0 4px;"
        )
    else:
        lbl.setStyleSheet(
            f"background:{_rgba(color, 0.14)};color:{color};font-size:10px;"
            "font-weight:600;border-radius:4px;padding:0 5px;"
        )
    return lbl


class MonthGrid(QWidget):
    daySelected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        for col, name in enumerate(_WEEKDAYS):
            head = QLabel(name.upper())
            head.setAlignment(Qt.AlignCenter)
            head.setObjectName("wkhead")
            grid.addWidget(head, 0, col)
        self._cells: list[_DayCell] = []
        for i in range(42):
            cell = _DayCell()
            cell.clicked.connect(self.daySelected)
            grid.addWidget(cell, 1 + i // 7, i % 7)
            self._cells.append(cell)
        for r in range(1, 7):
            grid.setRowStretch(r, 1)
        self._restyle()

    def _restyle(self) -> None:
        t = theme.active()
        self.setStyleSheet(
            f"QLabel#wkhead{{color:{t.ink3};font-size:11px;font-weight:700;"
            "letter-spacing:.06em;}"
        )

    @staticmethod
    def grid_dates(year: int, month: int) -> list[date]:
        first = date(year, month, 1)
        offset = (first.weekday() + 1) % 7  # days since the preceding Sunday
        start = first - timedelta(days=offset)
        return [start + timedelta(days=i) for i in range(42)]

    def render_month(
        self,
        year: int,
        month: int,
        selected: str,
        bars_by_date: dict[str, list[tuple[str, str, bool]]],
        conflict_dates: set[str],
        today: str,
    ) -> None:
        self._restyle()
        for cell, d in zip(self._cells, self.grid_dates(year, month), strict=True):
            iso = d.isoformat()
            cell.render(
                iso,
                d.day,
                in_month=(d.month == month),
                weekend=(d.weekday() in (5, 6)),
                selected=(iso == selected),
                today=(iso == today),
                conflict=(iso in conflict_dates),
                bars=bars_by_date.get(iso, []),
            )


# ---------------------------------------------------------------------------
# The Calendar page
# ---------------------------------------------------------------------------


def _color_dot(color: str, size: int = 10) -> QLabel:
    dot = QLabel()
    dot.setFixedSize(size, size)
    dot.setStyleSheet(f"background:{color};border-radius:{size // 3}px;")
    return dot


class CalendarView(QWidget):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._shows = CardShowRepository(conn)
        self._service = CalendarService(conn)
        self._worker: _RefreshWorker | None = None
        self._auto_refreshed = False
        self._states: list[str] = list(config.get_pref("show_states", _DEFAULT_STATES))
        if config.get_pref("show_states") is None:
            config.set_pref("show_states", self._states)

        today = date.today()
        self._year, self._month = today.year, today.month
        self._selected = today.isoformat()
        self._conflict_ids: set[int] = set()

        t = theme.active()
        self._prev = self._nav_btn("chevron-left", self._go_prev)
        self._next = self._nav_btn("chevron-right", self._go_next)
        self._month_lbl = QLabel("")
        self._month_lbl.setObjectName("pageTitle")
        self._month_lbl.setMinimumWidth(190)
        self._today_btn = QPushButton("Today")
        self._today_btn.clicked.connect(self._go_today)

        self._states_btn = QToolButton()
        self._states_btn.setPopupMode(QToolButton.InstantPopup)
        self._states_btn.setMenu(self._build_states_menu())

        self._refresh_btn = QPushButton("Refresh shows")
        self._refresh_btn.clicked.connect(lambda: self._refresh(self._states))

        self._add_btn = QPushButton("  Add entry")
        self._add_btn.setObjectName("primary")
        self._add_btn.setIcon(icons.icon("plus", t.accent_on, 15))
        self._add_btn.clicked.connect(self._add_entry)

        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(self._prev)
        header.addWidget(self._month_lbl)
        header.addWidget(self._next)
        header.addWidget(self._today_btn)
        header.addStretch(1)
        header.addWidget(self._states_btn)
        header.addWidget(self._refresh_btn)
        header.addWidget(self._add_btn)

        self._banner = QLabel("")
        self._banner.setObjectName("banner")
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)

        self._grid = MonthGrid()
        self._grid.daySelected.connect(self._on_day_selected)

        self._agenda_body = QWidget()
        self._agenda_layout = QVBoxLayout(self._agenda_body)
        self._agenda_layout.setContentsMargins(0, 0, 0, 0)
        self._agenda_layout.setSpacing(0)
        agenda_scroll = QScrollArea()
        agenda_scroll.setWidgetResizable(True)
        agenda_scroll.setFrameShape(QFrame.NoFrame)
        agenda_scroll.setWidget(self._agenda_body)
        agenda_card = QFrame()
        agenda_card.setObjectName("agendaCard")
        av = QVBoxLayout(agenda_card)
        av.setContentsMargins(14, 12, 10, 12)
        ahead = QLabel("Agenda")
        ahead.setStyleSheet(f"color:{t.ink2};font-size:13px;font-weight:700;")
        av.addWidget(ahead)
        av.addWidget(agenda_scroll, 1)
        agenda_card.setMaximumWidth(360)

        gridcard = QFrame()
        gridcard.setObjectName("gridCard")
        gcv = QVBoxLayout(gridcard)
        gcv.setContentsMargins(14, 12, 14, 12)
        gcv.addWidget(self._grid)

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(gridcard, 3)
        body.addWidget(agenda_card, 2)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(12)
        outer.addLayout(header)
        outer.addWidget(self._banner)
        outer.addLayout(body, 1)

        self._restyle()
        self._update_states_button()
        self.reload()

    # -- styling ------------------------------------------------------------

    def _restyle(self) -> None:
        t = theme.active()
        self.setStyleSheet(
            f"QFrame#gridCard, QFrame#agendaCard{{background:{t.surface};"
            f"border:1px solid {t.line};border-radius:14px;}}"
            f"QLabel#banner{{background:{_rgba(t.neg, 0.10)};"
            f"border:1px solid {_rgba(t.neg, 0.32)};border-radius:12px;"
            f"padding:9px 13px;color:{t.ink};}}"
        )

    def _nav_btn(self, icon_name: str, slot) -> QToolButton:
        b = QToolButton()
        b.setIcon(icons.icon(icon_name, theme.active().ink2, 18))
        b.setIconSize(QSize(18, 18))
        b.setAutoRaise(True)
        b.clicked.connect(slot)
        return b

    # -- month navigation ---------------------------------------------------

    def _go_prev(self) -> None:
        self._month -= 1
        if self._month < 1:
            self._month, self._year = 12, self._year - 1
        self.reload()

    def _go_next(self) -> None:
        self._month += 1
        if self._month > 12:
            self._month, self._year = 1, self._year + 1
        self.reload()

    def _go_today(self) -> None:
        today = date.today()
        self._year, self._month = today.year, today.month
        self._selected = today.isoformat()
        self.reload()

    def _on_day_selected(self, iso: str) -> None:
        self._selected = iso
        self.reload()

    def _update_month_label(self) -> None:
        self._month_lbl.setText(f"{_cal.month_name[self._month]} {self._year}")

    # -- states -------------------------------------------------------------

    def _build_states_menu(self) -> QMenu:
        menu = QMenu(self)
        for code, name in US_STATES:
            action = menu.addAction(f"{name} ({code})")
            action.setCheckable(True)
            action.setChecked(code in self._states)
            action.toggled.connect(
                lambda checked, c=code: self._on_state_toggled(c, checked)
            )
        return menu

    def _update_states_button(self) -> None:
        self._states_btn.setText(
            "States: " + (", ".join(sorted(self._states)) or "none")
        )

    def _on_state_toggled(self, code: str, checked: bool) -> None:
        if checked and code not in self._states:
            self._states.append(code)
        elif not checked and code in self._states:
            self._states.remove(code)
        config.set_pref("show_states", self._states)
        self._update_states_button()
        if checked:
            self._refresh([code])
        else:
            self._shows.delete_for_state(code)
            self._conn.commit()
            self.reload()

    # -- data ---------------------------------------------------------------

    def refresh(self) -> None:  # called by the main window on nav switch
        self.reload()

    def reload(self) -> None:
        self._restyle()
        grid = MonthGrid.grid_dates(self._year, self._month)
        gstart, gend = grid[0].isoformat(), grid[-1].isoformat()
        shows = [
            s
            for s in self._shows.list_for_states(self._states)
            if gstart <= s.starts_on <= gend
        ]
        entries = self._service.entries_overlapping(gstart, gend)
        conflict_ids, conflict_dates = find_conflicts(shows, entries)
        self._conflict_ids = conflict_ids

        bars: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
        for e in entries:
            color = e.color or _DEFAULT_ENTRY_COLOR
            cur = max(date.fromisoformat(e.starts_on), grid[0])
            last = min(date.fromisoformat(e.ends_on), grid[-1])
            while cur <= last:
                iso = cur.isoformat()
                label = e.title if (iso == e.starts_on or cur.weekday() == 6) else ""
                bars[iso].append((color, label, True))
                cur += timedelta(days=1)
        for s in shows:
            color = _status_color(s.status) or theme.active().ink3
            bars[s.starts_on].append((color, s.name, False))

        self._grid.render_month(
            self._year,
            self._month,
            self._selected,
            bars,
            conflict_dates,
            date.today().isoformat(),
        )
        self._render_agenda(shows, entries, conflict_ids)
        self._update_banner(conflict_dates)
        self._update_month_label()

        if (
            not shows
            and self._states
            and not self._auto_refreshed
            and _db_file(self._conn) is not None
        ):
            self._auto_refreshed = True
            self._refresh(self._states)

    def _update_banner(self, conflict_dates: set[str]) -> None:
        if not conflict_dates:
            self._banner.setVisible(False)
            return
        n = len(conflict_dates)
        pretty = datetime.strptime(min(conflict_dates), "%Y-%m-%d").strftime("%b %d")
        plural = "s" if n != 1 else ""
        self._banner.setText(
            f"⚠  {n} conflict{plural} this month — e.g. {pretty}: a show you "
            "plan to attend overlaps a personal entry."
        )
        self._banner.setVisible(True)

    # -- agenda -------------------------------------------------------------

    def _clear_agenda(self) -> None:
        while self._agenda_layout.count():
            it = self._agenda_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def _render_agenda(self, shows, entries, conflict_ids: set[int]) -> None:
        self._clear_agenda()
        t = theme.active()
        events = [("show", s.starts_on, s.start_time or "", s) for s in shows]
        events += [("entry", e.starts_on, "", e) for e in entries]
        events.sort(key=lambda x: (x[1], x[2]))
        if not events:
            empty = QLabel("Nothing this month.\nUse “+ Add entry” to block dates.")
            empty.setStyleSheet(f"color:{t.ink3};padding:12px 4px;")
            self._agenda_layout.addWidget(empty)
            self._agenda_layout.addStretch(1)
            return
        last = None
        for kind, d, _time, obj in events:
            if d != last:
                self._agenda_layout.addWidget(self._date_header(d))
                last = d
            if kind == "show":
                self._agenda_layout.addWidget(
                    self._show_row(obj, obj.source_id in conflict_ids)
                )
            else:
                self._agenda_layout.addWidget(self._entry_row(obj))
        self._agenda_layout.addStretch(1)

    def _date_header(self, iso: str) -> QLabel:
        t = theme.active()
        pretty = datetime.strptime(iso, "%Y-%m-%d").strftime("%a · %b %d")
        lbl = QLabel(pretty.upper())
        lbl.setStyleSheet(
            f"color:{t.ink3};font-size:10.5px;font-weight:700;letter-spacing:.06em;"
            "padding:10px 2px 4px;"
        )
        return lbl

    def _row_frame(self, accent: str | None = None):
        t = theme.active()
        row = QFrame()
        h = QHBoxLayout(row)
        h.setContentsMargins(6, 6, 6, 6)
        h.setSpacing(9)
        border = f"border-left:2px solid {accent};" if accent else ""
        row.setStyleSheet(
            f"QFrame{{{border}}}QFrame:hover{{background:{t.surface2};border-radius:8px;}}"
        )
        return row, h

    def _show_row(self, show: CardShow, conflict: bool) -> QFrame:
        t = theme.active()
        color = _status_color(show.status) or t.ink3
        row, h = self._row_frame(t.neg if conflict else None)
        h.addWidget(_color_dot(color), 0, Qt.AlignTop)
        mid = QVBoxLayout()
        mid.setSpacing(1)
        nm = QLabel(show.name)
        nm.setStyleSheet(f"color:{t.ink};font-weight:600;font-size:12.5px;")
        nm.setWordWrap(True)
        mid.addWidget(nm)
        where = ", ".join(x for x in (show.city, show.state) if x)
        meta = " · ".join(x for x in (where, show.start_time) if x) or "—"
        if conflict:
            meta += "  ⚠ overlaps a personal entry"
        sub = QLabel(meta)
        sub.setStyleSheet(f"color:{t.neg if conflict else t.ink3};font-size:10.5px;")
        sub.setWordWrap(True)
        mid.addWidget(sub)
        h.addLayout(mid, 1)
        h.addLayout(self._status_buttons(show))
        return row

    def _status_buttons(self, show: CardShow):
        box = QHBoxLayout()
        box.setSpacing(3)
        edge = theme.active().ink
        for value, title in _STATUS_ORDER:
            color = _status_color(value)
            active = show.status == value
            b = QToolButton()
            b.setToolTip(title)
            b.setFixedSize(18, 18)
            border = f"2px solid {edge}" if active else "1px solid transparent"
            fill = color if active else _rgba(color, 0.22)
            b.setStyleSheet(
                f"QToolButton{{background:{fill};border:{border};border-radius:5px;}}"
            )
            b.clicked.connect(
                lambda _=False, s=value, was=active, sid=show.source_id: (
                    self._set_status(sid, None if was else s)
                )
            )
            box.addWidget(b)
        return box

    def _entry_row(self, entry: CalendarEntry) -> QFrame:
        t = theme.active()
        color = entry.color or _DEFAULT_ENTRY_COLOR
        row, h = self._row_frame(color)
        h.addWidget(_color_dot(color), 0, Qt.AlignTop)
        mid = QVBoxLayout()
        mid.setSpacing(1)
        nm = QLabel(entry.title)
        nm.setStyleSheet(f"color:{t.ink};font-weight:600;font-size:12.5px;")
        nm.setWordWrap(True)
        mid.addWidget(nm)
        bits = [_KIND_LABEL.get(entry.kind, entry.kind).lower()]
        if entry.ends_on != entry.starts_on:
            through = datetime.strptime(entry.ends_on, "%Y-%m-%d").strftime("%b %d")
            bits.append(f"through {through}")
        elif not entry.all_day and entry.start_time:
            bits.append(entry.start_time)
        sub = QLabel(" · ".join(bits))
        sub.setStyleSheet(f"color:{t.ink3};font-size:10.5px;")
        mid.addWidget(sub)
        h.addLayout(mid, 1)
        edit = QToolButton()
        edit.setIcon(icons.icon("pencil", t.ink3, 14))
        edit.setToolTip("Edit / delete")
        edit.setAutoRaise(True)
        edit.clicked.connect(lambda _=False, e=entry: self._edit_entry(e))
        h.addWidget(edit, 0, Qt.AlignTop)
        return row

    # -- actions ------------------------------------------------------------

    def _set_status(self, source_id: int, status: str | None) -> None:
        self._shows.set_status(source_id, status)
        self._conn.commit()
        self.reload()

    def _add_entry(self) -> None:
        dlg = EntryDialog(self, default_date=self._selected)
        if dlg.exec() == QDialog.Accepted:
            self._service.add_entry(dlg.result_entry())
            self.reload()

    def _edit_entry(self, entry: CalendarEntry) -> None:
        dlg = EntryDialog(self, entry=entry)
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.deleted:
            if entry.id is not None:
                self._service.delete_entry(entry.id)
        else:
            self._service.update_entry(dlg.result_entry())
        self.reload()

    # -- refresh ------------------------------------------------------------

    def _refresh(self, states: list[str]) -> None:
        if not states:
            return
        db = _db_file(self._conn)
        if db is None:  # in-memory (tests) — synchronous
            CardShowService(self._conn).refresh(states)
            self.reload()
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._refresh_btn.setEnabled(False)
        self._worker = _RefreshWorker(db, states)
        self._worker.done.connect(self._on_refreshed)
        self._worker.start()

    def _on_refreshed(self, _summary) -> None:
        self._refresh_btn.setEnabled(True)
        self._worker = None
        self.reload()
