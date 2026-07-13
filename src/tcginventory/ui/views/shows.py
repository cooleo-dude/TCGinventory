"""Upcoming card-show calendar (embedded in the Dashboard).

A month calendar whose cells show the show name(s) for each day, beside a list
of the selected day's shows (click one to open its tcdb page). A state
multi-select and a "TCG only" toggle drive what's shown; both persist to the
JSON prefs so the daily background task refreshes the same states. Manual
refresh runs off the UI thread.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from PySide6.QtCore import QDate, QEvent, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QCalendarWidget,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSplitter,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tcginventory import config
from tcginventory.data.repositories import CardShowRepository
from tcginventory.domain.models import CardShow
from tcginventory.services.cardshows import US_STATES, CardShowService, is_tcg

_SHOW_DAY_BG = QColor("#ebdbb2")  # gruvbox light — subtle "has shows" tint
_DEFAULT_STATES = ["NJ"]
_URL_ROLE = Qt.UserRole + 1

# Show up to this many event names per day cell; the rest collapse into a
# "… N more shows not displayed" indicator line.
_MAX_CELL_SHOWS = 5
_CELL_FONT_PT = 7.5
_DAY_NUM_H = 15  # px reserved for the day-number line at the top of a cell


def _db_file(conn: sqlite3.Connection) -> str | None:
    for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
        if name == "main":
            return file or None
    return None


class _ShowRefreshWorker(QThread):
    """Refresh shows off the UI thread with its own DB connection."""

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


class ShowCalendar(QCalendarWidget):
    """Month calendar that paints each day's show name(s) into its cell.

    The whole month is always visible (rows fill the pane — never scrolls; use
    the header arrows to change month). Each cell shows as many event names as
    fit, up to ``_MAX_CELL_SHOWS``; any remainder collapses into a
    "… N more shows not displayed" indicator line.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.setGridVisible(True)
        self._by_date: dict[str, list[CardShow]] = {}

        # Keep the whole month visible — the grid stretches to fill; no scroll.
        # The internal view otherwise grabs the mouse wheel to change months;
        # intercept it so the wheel scrolls the Dashboard page instead and the
        # month only changes via the header arrows.
        self._view = self.findChild(QTableView, "qt_calendar_calendarview")
        if self._view is not None:
            self._view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self._view.installEventFilter(self)
            self._view.viewport().installEventFilter(self)

    def _enclosing_scroll_area(self) -> QAbstractScrollArea | None:
        w = self.parentWidget()
        while w is not None:
            if isinstance(w, QAbstractScrollArea):
                return w
            w = w.parentWidget()
        return None

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Wheel and self._view is not None and obj in (
            self._view,
            self._view.viewport(),
        ):
            area = self._enclosing_scroll_area()
            if area is not None:  # scroll the page instead of changing month
                QApplication.sendEvent(area.viewport(), event)
            return True  # never let the calendar navigate on wheel
        return super().eventFilter(obj, event)

    def wheelEvent(self, event) -> None:
        event.ignore()  # let the wheel bubble to the Dashboard scroll area

    @staticmethod
    def _cell_font():
        from PySide6.QtGui import QFont

        f = QFont()
        f.setPointSizeF(_CELL_FONT_PT)
        return f

    def set_shows(self, shows: list[CardShow]) -> None:
        by_date: dict[str, list[CardShow]] = {}
        for s in shows:
            by_date.setdefault(s.starts_on, []).append(s)
        self._by_date = by_date
        self.updateCells()

    def shows_on(self, d: QDate) -> list[CardShow]:
        return self._by_date.get(d.toString("yyyy-MM-dd"), [])

    def paintCell(self, painter, rect, d: QDate) -> None:
        shows = self.shows_on(d)
        if not shows:
            super().paintCell(painter, rect, d)
            return

        painter.save()
        selected = d == self.selectedDate()
        if selected:
            painter.fillRect(rect, self.palette().highlight())
            painter.setPen(self.palette().highlightedText().color())
        else:
            painter.fillRect(rect, _SHOW_DAY_BG)
            painter.setPen(QColor("#3c3836"))  # gruvbox dark fg for contrast

        painter.drawText(
            rect.adjusted(3, 1, -3, 0), Qt.AlignTop | Qt.AlignLeft, str(d.day())
        )

        painter.setFont(self._cell_font())
        fm = painter.fontMetrics()
        line_h = fm.height()
        top = rect.top() + _DAY_NUM_H
        width = rect.width() - 6
        x = rect.left() + 3

        # Show as many names as fit (cap 5); keep a line for the overflow note.
        capacity = max(1, (rect.bottom() - top) // line_h)
        n = min(len(shows), _MAX_CELL_SHOWS, capacity)
        overflow = len(shows) - n
        if overflow > 0 and n == capacity and n > 0:
            n -= 1  # free a line for the indicator so it's never clipped
            overflow = len(shows) - n

        for i in range(n):
            label = fm.elidedText(shows[i].name, Qt.ElideRight, width)
            painter.drawText(x, top + i * line_h + fm.ascent(), label)
        if overflow > 0:
            note = fm.elidedText(
                f"... {overflow} more shows not displayed", Qt.ElideRight, width
            )
            painter.drawText(x, top + n * line_h + fm.ascent(), note)
        painter.restore()


class ShowsView(QWidget):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._repo = CardShowRepository(conn)
        self._worker: _ShowRefreshWorker | None = None
        self._states: list[str] = list(config.get_pref("show_states", _DEFAULT_STATES))
        self._tcg_only: bool = bool(config.get_pref("shows_tcg_only", False))
        self._auto_refreshed = False
        # Persist the default so the headless daily task refreshes it too.
        if config.get_pref("show_states") is None:
            config.set_pref("show_states", self._states)

        # -- controls
        self._states_btn = QToolButton()
        self._states_btn.setText("States")
        self._states_btn.setPopupMode(QToolButton.InstantPopup)
        self._states_btn.setMenu(self._build_states_menu())

        self._tcg_toggle = QPushButton("TCG only")
        self._tcg_toggle.setCheckable(True)
        self._tcg_toggle.setChecked(self._tcg_only)
        self._tcg_toggle.toggled.connect(self._on_tcg_toggled)

        self._refresh_btn = QPushButton("Refresh shows")
        self._refresh_btn.clicked.connect(lambda: self._refresh(self._states))

        self._status = QLabel("")
        # palette(windowText) adapts to the theme (dark text on light, light on
        # dark) — palette(mid) was near-invisible in both.
        self._status.setStyleSheet("color: palette(windowText);")

        controls = QHBoxLayout()
        controls.addWidget(QLabel("<b>Card Shows</b>"))
        controls.addSpacing(12)
        controls.addWidget(self._states_btn)
        controls.addWidget(self._tcg_toggle)
        controls.addWidget(self._refresh_btn)
        controls.addWidget(self._status, 1)

        # -- calendar + day list
        self._calendar = ShowCalendar()
        # A modest floor keeps a full month readable on small windows; the
        # calendar otherwise grows with the section (it never scrolls — its rows
        # stretch to always show the whole month, more events per cell when tall).
        self._calendar.setMinimumHeight(340)
        self._calendar.selectionChanged.connect(self._update_day_list)
        self._calendar.clicked.connect(self._update_day_list)

        self._day_list = QListWidget()
        self._day_list.itemActivated.connect(self._open_show)
        self._day_list.itemClicked.connect(self._open_show)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._calendar)
        split.addWidget(self._day_list)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(split, 1)

        self._update_states_button()
        self.reload()

    # -- states menu --------------------------------------------------------

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
        if self._states:
            self._states_btn.setText("States: " + ", ".join(sorted(self._states)))
        else:
            self._states_btn.setText("States: none")

    def _on_state_toggled(self, code: str, checked: bool) -> None:
        if checked and code not in self._states:
            self._states.append(code)
        elif not checked and code in self._states:
            self._states.remove(code)
        config.set_pref("show_states", self._states)
        self._update_states_button()
        if checked:
            self._refresh([code])  # fetch the newly-added state
        else:
            self._repo.delete_for_state(code)  # drop its shows
            self._conn.commit()
            self.reload()

    def _on_tcg_toggled(self, on: bool) -> None:
        self._tcg_only = on
        config.set_pref("shows_tcg_only", on)
        self.reload()

    # -- data ---------------------------------------------------------------

    def _current_shows(self) -> list[CardShow]:
        shows = self._repo.list_for_states(self._states)
        if self._tcg_only:
            shows = [s for s in shows if is_tcg(s)]
        return shows

    def reload(self) -> None:
        shows = self._current_shows()
        self._calendar.set_shows(shows)
        # Jump to the first upcoming show's month for immediate context.
        upcoming = [s for s in shows if s.starts_on >= date.today().isoformat()]
        if upcoming:
            first = min(s.starts_on for s in upcoming)
            y, m, dd = (int(x) for x in first.split("-"))
            self._calendar.setSelectedDate(QDate(y, m, dd))
            self._calendar.setCurrentPage(y, m)
        self._update_day_list()
        self._status.setText(
            f"{len(shows)} show(s) in {', '.join(self._states) or '—'}"
        )
        # First open with no data for the selected states → populate once.
        if (
            not shows
            and self._states
            and not self._auto_refreshed
            and _db_file(self._conn) is not None
        ):
            self._auto_refreshed = True
            self._refresh(self._states)

    def _update_day_list(self, *_args) -> None:
        self._day_list.clear()
        for show in self._calendar.shows_on(self._calendar.selectedDate()):
            if self._tcg_only and not is_tcg(show):
                continue
            when = show.start_time or ""
            if show.end_time:
                when += f"–{show.end_time}"
            where = ", ".join(x for x in (show.venue, show.city) if x)
            text = f"{when}  {show.name}"
            if where:
                text += f"\n    {where}"
            item = QListWidgetItem(text.strip())
            item.setData(_URL_ROLE, show.detail_url)
            item.setToolTip("Click to open on tcdb.com")
            self._day_list.addItem(item)
        if self._day_list.count() == 0:
            self._day_list.addItem(QListWidgetItem("No shows on the selected date."))

    def _open_show(self, item: QListWidgetItem) -> None:
        url = item.data(_URL_ROLE)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    # -- refresh ------------------------------------------------------------

    def _refresh(self, states: list[str]) -> None:
        if not states:
            return
        db = _db_file(self._conn)
        if db is None:  # in-memory (tests/dev) — run synchronously
            CardShowService(self._conn).refresh(states)
            self.reload()
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._refresh_btn.setEnabled(False)
        self._status.setText("Refreshing shows…")
        self._worker = _ShowRefreshWorker(db, states)
        self._worker.done.connect(self._on_refreshed)
        self._worker.start()

    def _on_refreshed(self, summary) -> None:
        self._refresh_btn.setEnabled(True)
        self._worker = None
        self.reload()
        if summary.errors:
            self._status.setText("Refresh error: " + "; ".join(summary.errors)[:120])
