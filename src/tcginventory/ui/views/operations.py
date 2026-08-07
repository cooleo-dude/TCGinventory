"""Operations — the maintenance hub (F-OP-*).

Every job that keeps the ledger current used to live in the File menu, where a
label couldn't say what it did or when it last ran, and results arrived as modal
popups that were dismissed and lost. This screen owns them instead: a tile per
operation with a plain description, its own status, and inline progress; an
Activity list of what actually happened; and a fact strip describing the install.

The screen is drawn from :mod:`tcginventory.services.operations` — the registry
says *what* exists, this module says *how to run it*. A future operation is one
registry entry plus one runner in :meth:`OperationsView._runner`.

Long jobs run on the worker threads that used to live in ``main_window`` (price
refresh, catalog pull, market sweep); the window now only hosts the screen.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from tcginventory import config
from tcginventory.services import importexport
from tcginventory.services import operations as ops
from tcginventory.ui import icons, theme
from tcginventory.ui.views._worker import TaskWorker

_COLUMNS = 2  # tiles per row (a "wide" tile spans both)
_WIDE = {"backup.database", "automation.daily_prices"}

# A line under each heading, so a section says what it is for rather than
# leaving the tiles to imply it.
_SECTION_NOTES = {
    "data_in": "Spreadsheets in — cards you own, or purchases with their cost basis.",
    "data_out": (
        "Copies you can keep: readable exports, and the backup a restore reads."
    ),
    "prices": "What your cards are worth, and the history behind the trends.",
    "reference": "The card database and show listings everything else looks up.",
    "automation": "What runs on its own, with the app closed.",
    "maintenance": "Checks and housekeeping for the database itself.",
}


def _mono(size: float, bold: bool = False) -> QFont:
    font = QFont(theme.mono_family())
    font.setPointSizeF(size)
    font.setBold(bold)
    return font


def _eyebrow(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("opEyebrow")
    return label


def _micro(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("opMicro")
    return label


# ---------------------------------------------------------------------------
# workers (relocated from main_window — the screen that shows them owns them)
# ---------------------------------------------------------------------------


class _PriceRefreshWorker(QThread):
    """Runs a price refresh off the UI thread with its own DB connection."""

    done = Signal(object)  # RefreshSummary

    def __init__(self, db_path) -> None:
        super().__init__()
        self._db_path = db_path

    def run(self) -> None:  # pragma: no cover - exercised via the UI
        import logging

        from tcginventory.data.db import open_database
        from tcginventory.services.pricing import PricingService, RefreshSummary

        try:
            conn = open_database(self._db_path)
            try:
                summary = PricingService(conn).refresh()
            finally:
                conn.close()
        except Exception as exc:  # never crash the thread
            summary = RefreshSummary(errors=[str(exc)])
        logging.getLogger(__name__).info(
            "price refresh: priced=%d unmapped=%d errors=%s",
            summary.priced,
            len(summary.unmapped),
            summary.errors,
        )
        self.done.emit(summary)


class _CatalogUpdateWorker(QThread):
    """Pulls the reference card catalog off the UI thread (its own connection)."""

    done = Signal(object)  # CatalogUpdateSummary

    def __init__(self, db_path, language: str = "EN") -> None:
        super().__init__()
        self._db_path = db_path
        self._language = language

    def run(self) -> None:  # pragma: no cover - exercised via the UI
        import logging

        from tcginventory.data.db import open_database
        from tcginventory.services.catalog import (
            CatalogUpdateSummary,
            update_catalog,
            update_catalog_ja,
        )

        pull = update_catalog_ja if self._language == "JP" else update_catalog
        try:
            conn = open_database(self._db_path)
            try:
                summary = pull(conn)
            finally:
                conn.close()
        except Exception as exc:  # never crash the thread
            summary = CatalogUpdateSummary(errors=[str(exc)])
        logging.getLogger(__name__).info(
            "catalog update: sets=%d cards=%d errors=%d",
            summary.sets,
            summary.cards,
            len(summary.errors),
        )
        self.done.emit(summary)


class _MarketSweepWorker(QThread):
    """Price the whole catalog into the price-history DB, off the UI thread."""

    done = Signal(object)  # SweepSummary

    def __init__(self, db_path) -> None:
        super().__init__()
        self._db_path = db_path

    def run(self) -> None:  # pragma: no cover - exercised via the UI
        import logging

        from tcginventory.data.backup import (
            backup_price_history,
            timestamp_for_filename,
        )
        from tcginventory.data.db import open_database
        from tcginventory.data.pricehistory import open_history
        from tcginventory.services.marketprices import SweepSummary, sweep

        try:
            conn = open_database(self._db_path)
            hist = open_history()
            try:
                summary = sweep(conn, hist)
                backup_price_history(
                    hist, config.data_dir() / "backups", timestamp_for_filename()
                )
            finally:
                hist.close()
                conn.close()
        except Exception as exc:
            summary = SweepSummary(errors=[str(exc)])
        logging.getLogger(__name__).info(
            "market sweep: sets=%d priced=%d snapshots=%d errors=%d",
            summary.sets,
            summary.priced,
            summary.snapshots,
            len(summary.errors),
        )
        self.done.emit(summary)


# ---------------------------------------------------------------------------
# tiles
# ---------------------------------------------------------------------------


class _Section(QFrame):
    """One titled group of operations, in a frame of its own.

    The screen used to run title-grid-title-grid with nothing between, so a heading
    read as loose text above a field of tiles rather than as the top of a section.
    Each section now carries its own surface, a heading, a one-line summary of what
    the section is for, and a rule under it, which is also what makes the in-page
    navigator's jumps land somewhere legible.
    """

    def __init__(self, group: ops.Group, blurb: str = "") -> None:
        super().__init__()
        self.group = group
        self.setObjectName("opSection")
        box = QVBoxLayout(self)
        box.setContentsMargins(15, 13, 15, 15)
        box.setSpacing(10)

        head = QVBoxLayout()
        head.setSpacing(2)
        title = QLabel(group.label)
        title.setObjectName("opSectionTitle")
        head.addWidget(title)
        if blurb:
            note = QLabel(blurb)
            note.setObjectName("opSectionNote")
            note.setWordWrap(True)
            head.addWidget(note)
        box.addLayout(head)

        rule = QFrame()
        rule.setObjectName("opRule")
        rule.setFixedHeight(1)
        box.addWidget(rule)

        self.grid = QGridLayout()
        self.grid.setSpacing(11)
        box.addLayout(self.grid)


class _SectionNav(QListWidget):
    """The list of sections down the left: click to jump, and it follows scrolling."""

    def __init__(self, on_pick) -> None:
        super().__init__()
        self.setObjectName("opNav")
        self.setFrameShape(QFrame.NoFrame)
        self.setFixedWidth(210)
        self.setCursor(Qt.PointingHandCursor)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.currentRowChanged.connect(on_pick)

    def show_current(self, row: int) -> None:
        """Highlight a row without treating it as a click."""
        if row == self.currentRow() or not (0 <= row < self.count()):
            return
        self.blockSignals(True)
        try:
            self.setCurrentRow(row)
        finally:
            self.blockSignals(False)


class _Tile(QFrame):
    """One operation, as the window presents it: what it does, when it last ran,
    and the control that runs it."""

    def __init__(self, op: ops.Operation, on_run) -> None:
        super().__init__()
        self.op = op
        self.setObjectName("opTileDanger" if op.danger else "opTile")
        t = theme.active()
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(7)

        head = QHBoxLayout()
        head.setSpacing(10)
        glyph = QLabel()
        glyph.setObjectName("opIcon")
        glyph.setFixedSize(30, 30)
        glyph.setAlignment(Qt.AlignCenter)
        glyph.setPixmap(icons.pixmap(op.icon, t.accent, 16))
        head.addWidget(glyph, 0, Qt.AlignTop)
        title = QLabel(op.title)
        title.setObjectName("opTitle")
        head.addWidget(title, 1, Qt.AlignVCenter)
        self._pill = QLabel("")
        self._pill.setAlignment(Qt.AlignCenter)
        self._pill.setFixedHeight(20)
        head.addWidget(self._pill, 0, Qt.AlignTop)
        box.addLayout(head)

        blurb = QLabel(op.blurb)
        blurb.setObjectName("opDesc")
        blurb.setWordWrap(True)
        box.addWidget(blurb)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        stat = QVBoxLayout()
        stat.setSpacing(1)
        stat.addWidget(_micro(op.caption))
        self._value = QLabel("—")
        self._value.setObjectName("opStat")
        self._value.setFont(_mono(10.5))
        stat.addWidget(self._value)
        foot.addLayout(stat)
        foot.addStretch(1)

        self.button = QPushButton(op.verb or "On")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.setObjectName(
            "opToggle"
            if op.kind == ops.KIND_TOGGLE
            else ("opPrimary" if op.primary else "opAction")
        )
        if op.kind == ops.KIND_TOGGLE:
            self.button.setCheckable(True)
            self.button.setMinimumWidth(64)
        self.button.clicked.connect(lambda: on_run(op))
        foot.addWidget(self.button)
        box.addLayout(foot)

        # Progress rail along the bottom edge, shown only while running.
        self.bar = QProgressBar()
        self.bar.setObjectName("opBar")
        self.bar.setRange(0, 0)  # indeterminate: the services report no percentage
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.bar.setVisible(False)
        box.addWidget(self.bar)

    def show_status(self, status: ops.Status) -> None:
        # A status can rename the button when the action depends on the state
        # ("Set up" becomes "Switch to CPU environment" once a GPU one exists).
        self._verb = status.verb or self.op.verb
        if self.op.kind != ops.KIND_TOGGLE:
            self.button.setText(self._verb)
        self._value.setText(status.value)
        self._value.setToolTip(status.detail or "")
        self._pill.setText(
            {
                ops.READY: "Ready",
                ops.RUNNING: "Running",
                ops.STALE: "Stale",
                ops.NEVER: "Never run",
                ops.ON: "On",
                ops.OFF: "Off",
            }.get(status.state, "")
        )
        self._pill.setObjectName(f"pill_{status.state}")
        self._pill.setStyleSheet("")  # force a re-evaluation of the id selector
        if self.op.kind == ops.KIND_TOGGLE:
            on = status.state == ops.ON
            self.button.setChecked(on)
            self.button.setText("On" if on else "Off")
        else:
            # Nothing to do right now (already built, nothing to remove): grey the
            # button and let the tooltip say why, rather than letting the click
            # start work that would be a no-op or a mistake.
            self.button.setEnabled(status.actionable)
            self.button.setToolTip(
                (status.detail or "") if not status.actionable else ""
            )
        self._repolish()

    def set_running(self, running: bool) -> None:
        self.bar.setVisible(running)
        self.button.setEnabled(not running)
        if running:
            self.button.setText("Running…")
            self._pill.setText("Running")
            self._pill.setObjectName("pill_running")
            self._repolish()
        else:
            self.button.setText(getattr(self, "_verb", None) or self.op.verb or "On")

    def _repolish(self) -> None:
        for widget in (self, self._pill, self.button):
            widget.style().unpolish(widget)
            widget.style().polish(widget)


# ---------------------------------------------------------------------------
# the view
# ---------------------------------------------------------------------------


class OperationsView(QWidget):
    """The maintenance hub. Emits ``changed`` when an operation altered the
    ledger, so the main window can refresh the read screens."""

    changed = Signal()

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._tiles: dict[str, _Tile] = {}
        self._workers: dict[str, QThread] = {}
        self.setObjectName("opPage")
        self.setStyleSheet(_qss(theme.active()))

        body = QWidget()
        body.setObjectName("opBody")
        col = QVBoxLayout(body)
        col.setContentsMargins(16, 14, 16, 16)
        col.setSpacing(13)

        title = QLabel("Operations")
        title.setObjectName("pageTitle")
        sub = QLabel(
            "Everything that maintains the app — bring data in, get it out, keep "
            "prices and the card database current, and train the recognition models."
        )
        sub.setObjectName("opSub")
        heading = QVBoxLayout()
        heading.setSpacing(3)
        heading.addWidget(title)
        heading.addWidget(sub)
        col.addLayout(heading)

        # "While you were away": the nightly task runs with the app closed, so
        # its results would otherwise be invisible until someone read Activity.
        self._digest = QFrame()
        self._digest.setObjectName("opDigest")
        self._digest_box = QVBoxLayout(self._digest)
        self._digest_box.setContentsMargins(16, 11, 16, 12)
        self._digest_box.setSpacing(4)
        self._digest.setVisible(False)
        col.addWidget(self._digest)

        self._system = QFrame()
        self._system.setObjectName("opSystem")
        self._system_row = QHBoxLayout(self._system)
        self._system_row.setContentsMargins(16, 11, 16, 11)
        self._system_row.setSpacing(18)
        col.addWidget(self._system)

        # A group with no operations renders nothing at all — that is what keeps a
        # reserved group free until its first entry lands.
        self._sections: list[_Section] = []
        for group in ops.GROUPS:
            members = ops.in_group(group.id)
            if not members:
                continue
            section = _Section(group, _SECTION_NOTES.get(group.id, ""))
            row = column = 0
            # A lone tile spans the section rather than leaving a column empty.
            solo = len(members) == 1
            for op in members:
                tile = _Tile(op, self._run)
                self._tiles[op.id] = tile
                if solo or op.id in _WIDE:
                    if column:
                        row, column = row + 1, 0
                    section.grid.addWidget(tile, row, 0, 1, _COLUMNS)
                    row, column = row + 1, 0
                else:
                    section.grid.addWidget(tile, row, column)
                    column += 1
                    if column >= _COLUMNS:
                        row, column = row + 1, 0
            for c in range(_COLUMNS):
                section.grid.setColumnStretch(c, 1)
            col.addWidget(section)
            self._sections.append(section)

        self._activity = QFrame()
        self._activity.setObjectName("opActivity")
        self._activity_box = QVBoxLayout(self._activity)
        self._activity_box.setContentsMargins(16, 12, 16, 14)
        self._activity_box.setSpacing(6)
        col.addWidget(self._activity)
        col.addStretch(1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setWidget(body)

        # The navigator: ten sections is more than a screenful, so the screen needs
        # a way to get to one without hunting. It also tracks where you are.
        self._nav = _SectionNav(self._jump_to_section)
        for section in self._sections:
            self._nav.addItem(section.group.label)
        self._scroll.verticalScrollBar().valueChanged.connect(self._follow_scroll)
        self._nav.show_current(0)

        nav_column = QVBoxLayout()
        nav_column.setContentsMargins(12, 16, 0, 16)
        nav_column.setSpacing(8)
        nav_column.addWidget(_eyebrow("On this screen"))
        nav_column.addWidget(self._nav, 1)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addLayout(nav_column)
        row.addWidget(self._scroll, 1)

        self.refresh()

    # -- the in-page navigator ----------------------------------------------

    def _jump_to_section(self, row: int) -> None:
        """Scroll a section's heading to the top of the view."""
        if not (0 <= row < len(self._sections)):
            return
        section = self._sections[row]
        bar = self._scroll.verticalScrollBar()
        bar.setValue(max(0, section.y() - 8))

    def _follow_scroll(self, value: int) -> None:
        """Keep the navigator on whichever section is at the top of the view."""
        current = 0
        for index, section in enumerate(self._sections):
            if section.y() - 24 <= value:
                current = index
            else:
                break
        self._nav.show_current(current)

    # -- rendering ----------------------------------------------------------

    def refresh(self) -> None:
        """Nav hook: re-read every status, the fact strip and Activity."""
        for op_id, tile in self._tiles.items():
            if op_id in self._workers:
                continue  # mid-run: leave the running state alone
            tile.show_status(ops.status_for(self._conn, tile.op))
        self._render_digest()
        self._render_system()
        self._render_activity()

    def _render_digest(self) -> None:
        self._clear_layout(self._digest_box)
        summary = ops.digest(self._conn)
        if summary is None:
            self._digest.setVisible(False)
            return
        self._digest.setVisible(True)
        self._digest.setProperty("bad", bool(summary.failures))
        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel(summary.headline)
        title.setObjectName("opDigestTitle")
        head.addWidget(title)
        head.addStretch(1)
        dismiss = QPushButton("Dismiss")
        dismiss.setObjectName("opGhost")
        dismiss.setCursor(Qt.PointingHandCursor)
        dismiss.clicked.connect(lambda: self._dismiss_digest(summary.newest))
        head.addWidget(dismiss)
        self._digest_box.addLayout(head)
        for line in summary.lines:
            label = QLabel(f"·  {line}")
            label.setObjectName("opDigestLine")
            label.setWordWrap(True)
            self._digest_box.addWidget(label)
        for widget in (self._digest, title):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _dismiss_digest(self, run_id: int) -> None:
        ops.acknowledge_digest(run_id)
        self._render_digest()

    def _render_system(self) -> None:
        self._clear_layout(self._system_row)
        t = theme.active()
        for i, fact in enumerate(ops.system_facts(self._conn)):
            if i:
                rule = QFrame()
                rule.setFixedWidth(1)
                rule.setStyleSheet(f"background:{t.line};")
                self._system_row.addWidget(rule)
            cell = QVBoxLayout()
            cell.setSpacing(1)
            cell.addWidget(_micro(fact.caption))
            value = QLabel(fact.value)
            value.setFont(_mono(11, bold=True))
            value.setObjectName("opFactPos" if fact.tone == "pos" else "opFact")
            cell.addWidget(value)
            self._system_row.addLayout(cell)
        self._system_row.addStretch(1)
        folder = QPushButton("  Open data folder")
        folder.setObjectName("opGhost")
        folder.setIcon(icons.icon("folder-open", t.ink2, 14))
        folder.setIconSize(QSize(14, 14))
        folder.setCursor(Qt.PointingHandCursor)
        folder.setToolTip(str(config.data_dir()))
        folder.clicked.connect(self._open_data_folder)
        self._system_row.addWidget(folder, 0, Qt.AlignVCenter)

    def _render_activity(self) -> None:
        self._clear_layout(self._activity_box)
        head = QHBoxLayout()
        head.addWidget(_eyebrow("Activity"))
        head.addStretch(1)
        self._activity_box.addLayout(head)

        lines = ops.activity(self._conn)
        if not lines:
            empty = QLabel(
                "Nothing yet. Runs show up here — including the daily background "
                "update and the automatic startup backup."
            )
            empty.setObjectName("opLogEmpty")
            empty.setWordWrap(True)
            self._activity_box.addWidget(empty)
            return
        for when, message, ok in lines:
            row = QHBoxLayout()
            row.setSpacing(9)
            dot = QLabel()
            dot.setObjectName("dotPos" if ok else "dotNeg")
            dot.setFixedSize(6, 6)
            row.addWidget(dot, 0, Qt.AlignVCenter)
            stamp = QLabel(when)
            stamp.setObjectName("opStamp")
            stamp.setFont(_mono(9.5))
            stamp.setFixedWidth(104)
            row.addWidget(stamp)
            text = QLabel(message)
            text.setObjectName("opLog")
            text.setWordWrap(True)
            row.addWidget(text, 1)
            self._activity_box.addLayout(row)

    def _clear_layout(self, layout) -> None:
        """Empty a layout *now*.

        ``deleteLater`` alone leaves the widget parented and painting until the
        event loop gets round to it, so a re-render (Activity redraws after every
        run) can briefly show the old rows on top of the new ones. Unparenting
        first takes them off screen immediately.
        """
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    # -- running ------------------------------------------------------------

    def _run(self, op: ops.Operation) -> None:
        runner = self._runner(op.id)
        if runner is None:  # pragma: no cover - registry/runner mismatch
            QMessageBox.warning(self, op.title, "That operation isn't wired up yet.")
            return
        runner()

    def _runner(self, op_id: str):
        """The seam a new operation plugs into: registry entry + runner here."""
        return {
            "import.items": self._import_items,
            "import.purchases": self._import_purchases,
            "export.inventory": self._export_inventory,
            "export.full": self._export_all,
            "backup.database": self._backup_database,
            "prices.refresh": self.refresh_prices,
            "prices.market_sweep": self._market_sweep,
            "catalog.update_en": lambda: self._update_catalog("EN"),
            "catalog.update_jp": lambda: self._update_catalog("JP"),
            "shows.refresh": lambda: self._task("shows.refresh", _refresh_shows),
            "db.integrity_check": lambda: self._task(
                "db.integrity_check", _integrity_check
            ),
            "db.vacuum": lambda: self._task("db.vacuum", _vacuum),
            "automation.daily_prices": self._toggle_auto_updates,
        }.get(op_id)

    def _log(self, op_id: str, ok: bool, summary: str, *, notify: bool = False) -> None:
        ops.record(self._conn, op_id, ok=ok, summary=summary)
        # Refresh the whole group, not just this tile: operations that sit
        # together tend to gate each other — building the training environment
        # is exactly what makes "Remove" available, and vice versa.
        finished = ops.BY_ID.get(op_id)
        for tile in self._tiles.values():
            if tile.op.id == op_id or (finished and tile.op.group == finished.group):
                if tile.op.id not in self._workers:
                    tile.show_status(ops.status_for(self._conn, tile.op))
        self._render_activity()
        self._render_system()
        if notify:
            self.changed.emit()

    def _db_file(self) -> str | None:
        """The backing SQLite file, or None for an in-memory DB (tests)."""
        for _seq, name, file in self._conn.execute("PRAGMA database_list"):
            if name == "main":
                return file or None
        return None

    def _open_data_folder(self) -> None:
        from PySide6.QtCore import QUrl

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.data_dir())))

    # -- dialog operations --------------------------------------------------

    def _export_inventory(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export inventory", "inventory.csv", "CSV (*.csv)"
        )
        if not path:
            return
        importexport.export_inventory_csv(self._conn, path)
        self._log("export.inventory", True, f"saved to {_short(path)}")

    def _export_all(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Export full backup folder")
        if not directory:
            return
        importexport.export_all(self._conn, directory)
        importexport.export_items_csv(self._conn, f"{directory}/items_readable.csv")
        importexport.export_inventory_csv(
            self._conn, f"{directory}/inventory_readable.csv"
        )
        importexport.export_transactions_csv(
            self._conn, f"{directory}/transactions_readable.csv"
        )
        self._log("export.full", True, f"wrote CSVs to {_short(directory)}")

    def _backup_database(self) -> None:
        from tcginventory.data.backup import backup_database

        default = str(config.data_dir() / "inventory-backup.db")
        path, _ = QFileDialog.getSaveFileName(
            self, "Backup database", default, "SQLite database (*.db)"
        )
        if not path:
            return
        try:
            backup_database(self._conn, path)
        except Exception as exc:  # pragma: no cover - surfaced to the user
            QMessageBox.warning(self, "Backup failed", str(exc))
            self._log("backup.database", False, str(exc))
            return
        self._log("backup.database", True, f"saved to {_short(path)}")

    def _import_items(self) -> None:
        self._import("import.items", "items", importexport.import_items)

    def _import_purchases(self) -> None:
        self._import("import.purchases", "purchases", importexport.import_purchases)

    def _import(self, op_id: str, mode: str, apply_fn) -> None:
        """Preview → confirm → apply. The preview is a real decision, so it stays
        a dialog; the outcome goes to Activity rather than another popup."""
        path, _ = QFileDialog.getOpenFileName(
            self, f"Import {mode} from CSV", "", "CSV (*.csv)"
        )
        if not path:
            return
        preview = importexport.preview_import(path, mode=mode)
        summary = f"{preview.ok_count} valid rows, {preview.error_count} error(s)."
        if preview.errors:
            summary += "\n\n" + "\n".join(preview.errors[:10])
            if preview.error_count > 10:
                summary += f"\n…and {preview.error_count - 10} more."
        if preview.ok_count == 0:
            QMessageBox.warning(self, "Nothing to import", summary)
            self._log(op_id, False, "nothing valid to import")
            return
        prompt = f"{summary}\n\nImport the {preview.ok_count} valid rows?"
        if QMessageBox.question(self, "Confirm import", prompt) != QMessageBox.Yes:
            return
        result = apply_fn(self._conn, path)
        self._log(
            op_id,
            True,
            f"{result.created} created, {result.matched} matched, "
            f"{result.posted} posted",
            notify=True,
        )

    # -- background operations ----------------------------------------------

    def refresh_prices(self, silent: bool = False) -> None:
        """Re-price owned cards. ``silent`` is the app-startup path, which runs
        the same way but without complaining when there's no file database."""
        self._start(
            "prices.refresh",
            lambda db: _PriceRefreshWorker(db),
            self._on_prices_refreshed,
            silent=silent,
        )

    def _market_sweep(self) -> None:
        self._start(
            "prices.market_sweep",
            lambda db: _MarketSweepWorker(db),
            self._on_market_swept,
        )

    def _update_catalog(self, language: str) -> None:
        op_id = "catalog.update_en" if language == "EN" else "catalog.update_jp"
        self._start(
            op_id,
            lambda db, lang=language: _CatalogUpdateWorker(db, lang),
            lambda summary, oid=op_id: self._on_catalog_updated(oid, summary),
        )

    def _task(self, op_id: str, task) -> None:
        """Run a plain ``task(conn) -> summary`` job in the background."""
        self._start(
            op_id,
            lambda db, fn=task: TaskWorker(db, fn),
            lambda payload, oid=op_id: self._on_task_done(oid, payload),
        )

    def _on_task_done(self, op_id: str, payload) -> None:
        ok, summary = payload
        if not ok:
            QMessageBox.warning(self, ops.BY_ID[op_id].title, summary)
        self._log(op_id, ok, summary, notify=ok)

    def _start(self, op_id: str, make_worker, on_done, *, silent: bool = False) -> None:
        if op_id in self._workers:
            return  # already running
        db_file = self._db_file()
        if not db_file:
            if not silent:
                QMessageBox.information(
                    self,
                    ops.BY_ID[op_id].title,
                    "Not available for an in-memory database.",
                )
            return
        tile = self._tiles.get(op_id)
        if tile is not None:
            tile.set_running(True)
        worker = make_worker(db_file)
        self._workers[op_id] = worker
        worker.done.connect(
            lambda payload, oid=op_id: self._finish(oid, on_done, payload)
        )
        worker.start()

    def _finish(self, op_id: str, on_done, payload) -> None:
        worker = self._workers.pop(op_id, None)
        if worker is not None:
            worker.deleteLater()
        tile = self._tiles.get(op_id)
        if tile is not None:
            tile.set_running(False)
        on_done(payload)

    def _on_prices_refreshed(self, summary) -> None:
        # Record the fresh collection value so the dashboard chart tracks the
        # tile (deduped per day).
        try:
            from tcginventory.services.reporting import ReportingService

            ReportingService(self._conn).record_collection_value()
        except Exception:
            pass  # best-effort; never block the refresh completion
        text = f"{summary.priced} priced"
        if summary.unmapped:
            text += f", {len(summary.unmapped)} skipped (no TCGplayer link)"
        if summary.errors:
            text += f" — errors: {'; '.join(summary.errors[:2])}"
        self._log("prices.refresh", not summary.errors, text, notify=True)

    def _on_market_swept(self, summary) -> None:
        if summary.errors and not summary.priced:
            QMessageBox.warning(
                self,
                "Market prices",
                "Update failed:\n" + "\n".join(summary.errors[:5]),
            )
            self._log("prices.market_sweep", False, summary.errors[0][:120])
            return
        self._log(
            "prices.market_sweep",
            True,
            f"{summary.sets} sets, {summary.priced:,} priced, "
            f"{summary.snapshots:,} changes recorded",
            notify=True,
        )

    def _on_catalog_updated(self, op_id: str, summary) -> None:
        if summary.errors and not summary.cards:
            QMessageBox.warning(
                self,
                "Card database",
                "Update failed:\n" + "\n".join(summary.errors[:5]),
            )
            self._log(op_id, False, summary.errors[0][:120])
            return
        text = f"{summary.sets} sets, {summary.cards:,} cards"
        if summary.errors:
            text += f" ({len(summary.errors)} set(s) skipped)"
        self._log(op_id, True, text, notify=True)

    # -- toggle -------------------------------------------------------------

    def _toggle_auto_updates(self) -> None:
        from tcginventory import scheduling

        op = ops.BY_ID["automation.daily_prices"]
        if not scheduling.is_supported():
            QMessageBox.information(
                self, op.title, "Automatic updates are only supported on Windows."
            )
            self._tiles[op.id].show_status(ops.status_for(self._conn, op))
            return
        want_on = not scheduling.is_enabled()
        try:
            scheduling.enable() if want_on else scheduling.disable()
        except Exception as exc:
            QMessageBox.warning(self, "Automatic updates", str(exc))
        else:
            self._settings().setValue("auto_update/enabled", want_on)
            if want_on:
                QMessageBox.information(
                    self,
                    "Automatic updates on",
                    "Prices will refresh once a day in the background (around "
                    f"{scheduling.DEFAULT_TIME}), even when the app is closed.",
                )
        self._tiles[op.id].show_status(ops.status_for(self._conn, op))

    @staticmethod
    def _settings():
        from PySide6.QtCore import QSettings

        return QSettings(config.APP_NAME, config.APP_NAME)


# ---------------------------------------------------------------------------
# background tasks — plain functions over a connection (see TaskWorker)
# ---------------------------------------------------------------------------


def _refresh_shows(conn) -> str:
    """Re-scrape the states the user follows (the same work the nightly task
    does), so the Calendar and Upcoming Shows are current on demand."""
    from tcginventory.services.cardshows import CardShowService

    states = list(config.get_pref("show_states", []))
    if not states:
        raise RuntimeError(
            "No states selected yet — choose them on the Calendar screen first."
        )
    summary = CardShowService(conn).refresh(states)
    conn.commit()
    text = f"{summary.fetched} shows across {', '.join(states)}"
    if summary.errors:
        text += f" ({len(summary.errors)} state(s) failed)"
    return text


def _integrity_check(conn) -> str:
    """SQLite's own page + foreign-key verification."""
    rows = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
    broken = [r for r in rows if r != "ok"]
    orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
    if broken or orphans:
        parts = []
        if broken:
            parts.append(f"{len(broken)} integrity problem(s): {broken[0]}")
        if orphans:
            parts.append(f"{len(orphans)} foreign-key orphan(s)")
        raise RuntimeError("; ".join(parts))
    return "no problems found — pages and foreign keys check out"


def _vacuum(conn) -> str:
    """Rebuild the file, reporting what it reclaimed."""
    path = config.db_path()
    try:
        before = path.stat().st_size
    except OSError:
        before = 0
    conn.execute("VACUUM")
    conn.commit()
    try:
        after = path.stat().st_size
    except OSError:
        after = before
    saved = max(0, before - after)
    if not saved:
        return f"already compact — {ops.human_bytes(after)}"
    return (
        f"reclaimed {ops.human_bytes(saved)} "
        f"({ops.human_bytes(before)} → {ops.human_bytes(after)})"
    )


def _short(path: str) -> str:
    """A path trimmed to its last two segments — enough to recognise, short
    enough for one Activity line."""
    from pathlib import Path

    parts = Path(path).parts
    return str(Path(*parts[-2:])) if len(parts) > 2 else str(path)


def _qss(t: theme.Tokens) -> str:
    return f"""
    #opPage, #opBody {{ background: {t.bg}; }}
    #opSub {{ color: {t.ink3}; font-size: 12px; }}
    #opEyebrow {{ color: {t.ink3}; font-size: 11px; font-weight: 700;
        letter-spacing: 2px; }}
    #opMicro {{ color: {t.ink3}; font-size: 9px; font-weight: 700;
        letter-spacing: .4px; }}

    #opSystem, #opActivity {{ background: {t.surface}; border: 1px solid {t.line};
        border-radius: 14px; }}

    /* A section is a surface of its own, so a heading reads as the top of
       something rather than as loose text above a field of tiles. */
    #opSection {{ background: {t.surface}; border: 1px solid {t.line};
        border-radius: 14px; }}
    #opSectionTitle {{ color: {t.ink}; font-size: 15px; font-weight: 700;
        letter-spacing: .2px; }}
    #opSectionNote {{ color: {t.ink3}; font-size: 11.5px; }}
    #opRule {{ background: {t.line}; border: none; }}

    /* The in-page navigator: quiet until you are on a section or hovering one. */
    #opNav {{ background: transparent; border: none; outline: none; }}
    #opNav::item {{ color: {t.ink3}; font-size: 12px; padding: 6px 10px;
        border-radius: 8px; border-left: 2px solid transparent; }}
    #opNav::item:hover {{ color: {t.ink}; background: {t.surface2}; }}
    #opNav::item:selected {{ color: {t.accent_ink}; background: {t.surface2};
        border-left: 2px solid {t.accent}; }}
    #opDigest {{ background: {t.surface}; border: 1px solid {t.line};
        border-left: 3px solid {t.accent}; border-radius: 12px; }}
    #opDigest[bad="true"] {{ border-left-color: {t.warn}; }}
    #opDigestTitle {{ color: {t.ink}; font-size: 12.5px; font-weight: 700; }}
    #opDigestLine {{ color: {t.ink2}; font-size: 11.5px; }}
    #opFact {{ color: {t.ink}; }}
    #opFactPos {{ color: {t.pos}; }}

    #opTile, #opTileDanger {{ background: {t.surface2}; border: 1px solid {t.line};
        border-radius: 12px; }}
    #opTileDanger {{ border-left: 3px solid {t.neg}; }}
    #opTile:hover, #opTileDanger:hover {{ border-color: {t.accent}; }}
    #opIcon {{ background: {t.accent_soft}; border-radius: 9px; }}
    #opTitle {{ color: {t.ink}; font-size: 13.5px; font-weight: 700; }}
    #opDesc {{ color: {t.ink2}; font-size: 12px; }}
    #opStat {{ color: {t.ink2}; }}

    #pill_ready, #pill_running, #pill_stale, #pill_never, #pill_on, #pill_off {{
        border-radius: 10px; padding: 1px 9px; font-size: 9.5px; font-weight: 700; }}
    #pill_ready {{ color: {t.ink3}; border: 1px solid {t.line2}; }}
    #pill_running {{ color: {t.accent_ink}; background: {t.accent_soft}; }}
    #pill_stale {{ color: {t.warn}; background: {_tint(t.warn)}; }}
    #pill_never {{ color: {t.ink3}; border: 1px dashed {t.line2}; }}
    #pill_on {{ color: {t.pos}; background: {_tint(t.pos)}; }}
    #pill_off {{ color: {t.ink3}; border: 1px solid {t.line2}; }}

    QPushButton#opAction {{ background: {t.surface}; border: 1px solid {t.line2};
        border-radius: 9px; color: {t.ink}; padding: 6px 14px; font-size: 12px;
        font-weight: 700; }}
    QPushButton#opAction:hover {{ border-color: {t.accent}; color: {t.accent_ink}; }}
    QPushButton#opAction:disabled {{ color: {t.ink3}; border-color: {t.line}; }}
    QPushButton#opPrimary {{ background: {t.accent}; color: {t.accent_on};
        border: none; border-radius: 9px; padding: 6px 16px; font-size: 12px;
        font-weight: 800; }}
    QPushButton#opPrimary:hover {{ background: {t.accent_ink}; }}
    QPushButton#opPrimary:disabled {{ background: {t.surface2}; color: {t.ink3}; }}
    QPushButton#opToggle {{ background: {t.surface}; border: 1px solid {t.line2};
        border-radius: 11px; color: {t.ink3}; padding: 5px 12px; font-size: 11px;
        font-weight: 700; }}
    QPushButton#opToggle:checked {{ background: {t.accent_soft};
        border-color: {t.accent}; color: {t.accent_ink}; }}
    QPushButton#opGhost {{ background: transparent; border: none; color: {t.ink3};
        font-size: 11px; font-weight: 700; padding: 4px 6px; }}
    QPushButton#opGhost:hover {{ color: {t.accent_ink}; }}

    #opBar {{ background: {t.line}; border: none; border-radius: 2px; }}
    #opBar::chunk {{ background: {t.accent}; border-radius: 2px; }}

    #dotPos {{ background: {t.pos}; border-radius: 3px; }}
    #dotNeg {{ background: {t.neg}; border-radius: 3px; }}
    #opStamp {{ color: {t.ink3}; }}
    #opLog {{ color: {t.ink2}; font-size: 11.5px; }}
    #opLogEmpty {{ color: {t.ink3}; font-size: 12px; }}
    """


def _tint(color: str, alpha: float = 0.16) -> str:
    from PySide6.QtGui import QColor

    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"
