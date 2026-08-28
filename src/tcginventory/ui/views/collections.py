"""Personal Collection — collecting projects as a block grid and a checklist.

Two states in one view, mirroring ``ui/views/operations.py``'s proven shape: a
grid of blocks over an in-window navigator, and a detail page per collection.
Where Inventory is one row per lot and can only draw what is held, this draws
what is **not** held — a slot for every card in the project.

No SQL and no money math here (``UI-NO-SQL``): rows come from
:class:`~tcginventory.services.collections.CollectionService`, cents are
formatted by :mod:`tcginventory.ui.formatting`, and the import goes through
:class:`~tcginventory.services.inventory.InventoryService`.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from tcginventory.domain.enums import Intent
from tcginventory.domain.models import Collection
from tcginventory.services.collections import (
    FILLED,
    IMPORTABLE,
    MISSING,
    SLOT_BY_NUMBER,
    SLOT_MASTER,
    TEMPLATE_CUSTOM,
    TEMPLATES,
    TEMPLATES_BY_ID,
    CollectionService,
    Slot,
    art_gap,
    summarise,
)
from tcginventory.services.inventory import InventoryService
from tcginventory.ui import card_thumbs, icons, theme
from tcginventory.ui.formatting import money
from tcginventory.ui.models.collection_model import (
    CARD_COL,
    FILTER_ALL,
    FILTER_DUPLICATES,
    FILTER_FILLED,
    FILTER_IMPORTABLE,
    FILTER_MISSING,
    NUMBER_COL,
    SLOT_ROLE,
    CollectionFilterProxy,
    CollectionSlotModel,
)
from tcginventory.ui.pager import Pager
from tcginventory.ui.spinner import BusyChip
from tcginventory.ui.views._art_warm import ArtWarmWorker
from tcginventory.ui.views._worker import retire
from tcginventory.ui.views.inventory import _Chip, _Segmented

_COLUMNS = 4  # blocks per row before wrapping


def _eyebrow(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("pcEyebrow")
    return label


class _NewCollectionDialog(QDialog):
    """Name a project, pick how it decides what belongs, and how it counts.

    The selector list comes from the catalog, so a template with nothing behind
    it says so instead of offering an empty picker — an artist run before the
    card database has been re-pulled, for instance.
    """

    def __init__(self, service: CollectionService, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New collection")
        self._service = service

        self.name = QLineEdit()
        self.name.setPlaceholderText("Base Set, Arita run, chase cards…")
        self.template = QComboBox()
        for t in TEMPLATES:
            self.template.addItem(t.label, t.id)
        self.selector = QComboBox()
        self.selector.setEditable(False)
        self.slot_mode = QComboBox()
        self.slot_mode.addItem("One slot per collector number", SLOT_BY_NUMBER)
        self.slot_mode.addItem("Master set — one slot per printing", SLOT_MASTER)
        self._note = QLabel()
        self._note.setObjectName("pcNote")
        self._note.setWordWrap(True)

        form = QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("Template", self.template)
        form.addRow("Covering", self.selector)
        form.addRow("Counting", self.slot_mode)
        form.addRow(self._note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._ok = buttons.button(QDialogButtonBox.Ok)

        self.template.currentIndexChanged.connect(self._reload_selector)
        self.name.textChanged.connect(self._revalidate)
        self.selector.currentIndexChanged.connect(self._revalidate)
        self._reload_selector()

    def _reload_selector(self) -> None:
        template = TEMPLATES_BY_ID[self.template.currentData()]
        self.selector.clear()
        if template.selector_kind == "none":
            self.selector.setEnabled(False)
            self._note.setText("You will add cards by hand.")
        else:
            options = self._service.selector_options(template.id)
            self.selector.addItems(options)
            self.selector.setEnabled(bool(options))
            if not options:
                kind = template.label.lower()
                self._note.setText(
                    f"The card database has nothing to build a {kind} collection "
                    "from yet. Run Operations → Update card database, then try "
                    "again."
                )
            elif template.english_only:
                self._note.setText(
                    f"{template.blurb} English cards only — Japanese printings carry "
                    "neither a set total nor an illustrator."
                )
            else:
                self._note.setText(template.blurb)
        self._revalidate()

    def _revalidate(self) -> None:
        template = TEMPLATES_BY_ID[self.template.currentData()]
        ready = bool(self.name.text().strip()) and (
            template.selector_kind == "none" or self.selector.count() > 0
        )
        self._ok.setEnabled(ready)

    def to_collection(self) -> Collection:
        template = self.template.currentData()
        return Collection(
            name=self.name.text().strip(),
            template=template,
            selector=(
                None if template == TEMPLATE_CUSTOM else self.selector.currentText()
            ),
            slot_mode=self.slot_mode.currentData(),
        )


class _Block(QFrame):
    """One collection as the grid presents it: what it is, and how far along."""

    def __init__(self, collection: Collection, progress, on_open) -> None:
        super().__init__()
        self.collection = collection
        complete = progress.total > 0 and progress.filled == progress.total
        self.setObjectName("pcBlockDone" if complete else "pcBlock")
        self.setCursor(Qt.PointingHandCursor)
        self._on_open = on_open
        t = theme.active()
        template = TEMPLATES_BY_ID.get(collection.template)

        box = QVBoxLayout(self)
        box.setContentsMargins(14, 12, 14, 13)
        box.setSpacing(7)

        head = QHBoxLayout()
        head.setSpacing(9)
        glyph = QLabel()
        glyph.setFixedSize(28, 28)
        glyph.setAlignment(Qt.AlignCenter)
        glyph.setObjectName("pcIcon")
        if template is not None:
            glyph.setPixmap(icons.pixmap(template.icon, t.accent, 15))
        head.addWidget(glyph, 0, Qt.AlignTop)
        title = QLabel(collection.name)
        title.setObjectName("pcBlockTitle")
        title.setWordWrap(True)
        head.addWidget(title, 1)
        box.addLayout(head)

        sub = QLabel(
            f"{template.label if template else collection.template}"
            + (f" · {collection.selector}" if collection.selector else "")
            + (" · master set" if collection.slot_mode == SLOT_MASTER else "")
        )
        sub.setObjectName("pcBlockSub")
        sub.setWordWrap(True)
        box.addWidget(sub)

        bar = QFrame()
        bar.setObjectName("pcBar")
        bar.setFixedHeight(6)
        fill = QFrame(bar)
        fill.setObjectName("pcBarDone" if complete else "pcBarFill")
        pct = progress.filled / progress.total if progress.total else 0.0
        # Painted on resize rather than with a layout: a one-child bar is simpler
        # to size directly than to constrain.
        bar.resizeEvent = lambda _e, f=fill, b=bar, p=pct: f.setGeometry(
            0, 0, int(b.width() * p), b.height()
        )
        box.addWidget(bar)

        foot = QHBoxLayout()
        count = QLabel(
            f"<b>{progress.filled}</b> of {progress.total}"
            if progress.total
            else "no slots yet"
        )
        count.setObjectName("pcCount")
        foot.addWidget(count)
        foot.addStretch(1)
        pct_label = QLabel("Complete" if complete else f"{round(progress.percent)}%")
        pct_label.setObjectName("pcPctDone" if complete else "pcPct")
        foot.addWidget(pct_label)
        box.addLayout(foot)

        if progress.importable:
            hint = QLabel(f"{progress.importable} in inventory, not brought in")
            hint.setObjectName("pcImportHint")
            hint.setWordWrap(True)
            box.addWidget(hint)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.LeftButton:
            self._on_open(self.collection)
        super().mouseReleaseEvent(event)


# One grid cell. Card proportions (5:7), big enough for the art to be the thing
# you read rather than a swatch beside a number. Fixed rather than
# viewport-derived so the thumbnail cache has one size to hold per card instead
# of a new decode every time the window is dragged.
_CELL_W = 150
_CELL_ART_H = card_thumbs.height_for(_CELL_W)
_CELL_H = _CELL_ART_H + 34  # art plus the caption strip
_CELL_GAP = 12


class _SlotCell(QFrame):
    """One position in the checklist, as a card.

    Missing slots still get a cell — that is the whole point of a collection —
    drawn as a dashed outline with the collector number, so a gap in a set reads
    as a gap rather than as absence.
    """

    def __init__(self, slot: Slot, on_import) -> None:
        super().__init__()
        self.slot = slot
        self.setObjectName(
            {
                FILLED: "pcCellFilled",
                IMPORTABLE: "pcCellImportable",
            }.get(slot.state, "pcCellMissing")
        )
        self.setFixedSize(_CELL_W, _CELL_H)
        self._on_import = on_import

        box = QVBoxLayout(self)
        box.setContentsMargins(4, 4, 4, 3)
        box.setSpacing(2)

        art = QLabel()
        art.setFixedHeight(_CELL_ART_H - 8)
        art.setAlignment(Qt.AlignCenter)
        art.setObjectName("pcCellArt")
        # Owned or not, the art is the card's — a missing slot showing its own
        # picture is exactly what makes a checklist worth looking at. Only the
        # frame says whether you have it.
        pixmap = card_thumbs.thumbnail(slot.card_id, _CELL_W - 16)
        if pixmap is not None:
            art.setPixmap(pixmap)
            if slot.state == MISSING:
                art.setObjectName("pcCellArtMissing")  # dimmed by the stylesheet
        else:
            art.setText(slot.number or "?")
            art.setObjectName("pcCellArtNone")
        box.addWidget(art, 1)

        caption = QLabel(self._caption(slot))
        caption.setObjectName("pcCellCaption")
        caption.setAlignment(Qt.AlignCenter)
        box.addWidget(caption)

        self.setToolTip(self._tooltip(slot))
        if slot.state == IMPORTABLE:
            self.setCursor(Qt.PointingHandCursor)

    @staticmethod
    def _caption(slot: Slot) -> str:
        number = slot.number or "?"
        if slot.qty > 1:
            return f"{number}  ×{slot.qty}"
        return number

    @staticmethod
    def _tooltip(slot: Slot) -> str:
        detail = f"{slot.name} · {slot.number or '?'}"
        if slot.treatment:
            detail += f" · {slot.treatment}"
        elif slot.treatments_unknown:
            detail += " · printings unknown"
        if slot.state == FILLED:
            detail += "\nIn this collection"
        elif slot.state == IMPORTABLE:
            detail += "\nIn inventory as resale — click to bring in"
        else:
            detail += "\nNot owned"
        if slot.market_value_cents:
            detail += f"\n{money(slot.market_value_cents)}"
        return detail

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.LeftButton and self.slot.state == IMPORTABLE:
            self._on_import(self.slot)
        super().mouseReleaseEvent(event)


class CollectionsView(QWidget):
    """The Personal Collection screen: block grid, and a checklist per project."""

    changed = Signal()  # the ledger moved (an import changed a lot's intent)

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._service = CollectionService(conn)
        self._inventory = InventoryService(conn)
        self._current: Collection | None = None
        self._nav_order: list[Collection] = []
        # True while we move the navigator ourselves; see _sync_nav_to.
        self._syncing_nav = False
        self._view_mode = "grid"
        # One art-warm worker at a time, with retired ones parked until they
        # actually stop (WORKER-RETIRE-NOT-REPLACE: overwriting a running QThread
        # aborts the process).
        self._warm: ArtWarmWorker | None = None
        self._parked_warms: list = []
        self.setStyleSheet(_qss(theme.active()))

        self._pages = QStackedWidget()
        self._pages.addWidget(self._build_grid())
        self._pages.addWidget(self._build_detail())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._pages)

    # -- the grid -----------------------------------------------------------

    def _build_grid(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(21, 17, 21, 0)
        col.setSpacing(0)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Personal Collection")
        title.setObjectName("pcTitle")
        titles.addWidget(title)
        self._grid_sub = QLabel()
        self._grid_sub.setObjectName("pcSubtitle")
        titles.addWidget(self._grid_sub)
        head.addLayout(titles)
        head.addStretch(1)
        new = QPushButton("New collection")
        new.setObjectName("pcPrimary")
        new.setIcon(icons.icon("plus", theme.active().accent_on, 14))
        new.clicked.connect(self._new_collection)
        head.addWidget(new, 0, Qt.AlignBottom)
        col.addLayout(head)

        self._facts = QFrame()
        self._facts.setObjectName("pcFacts")
        self._facts_row = QHBoxLayout(self._facts)
        self._facts_row.setContentsMargins(0, 0, 0, 0)
        self._facts_row.setSpacing(0)
        col.addSpacing(15)
        col.addWidget(self._facts)

        self._blocks_host = QWidget()
        self._blocks = QGridLayout(self._blocks_host)
        self._blocks.setContentsMargins(0, 16, 0, 20)
        self._blocks.setSpacing(13)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self._blocks_host)
        col.addWidget(scroll, 1)

        self._empty = QLabel(
            "No collections yet. A collection is a project — a set you are "
            "completing, every card one artist drew, or a handful you pick "
            "yourself. Unlike Inventory, it shows the cards you do <i>not</i> "
            "own yet."
        )
        self._empty.setObjectName("pcEmpty")
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignTop)
        col.addWidget(self._empty)
        return page

    # -- the detail page ----------------------------------------------------

    def _build_detail(self) -> QWidget:
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self._nav = QListWidget()
        self._nav.setObjectName("pcNav")
        self._nav.setFrameShape(QFrame.NoFrame)
        self._nav.setFixedWidth(206)
        self._nav.setCursor(Qt.PointingHandCursor)
        self._nav.currentRowChanged.connect(self._nav_picked)
        row.addWidget(self._nav)

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(21, 17, 21, 0)
        col.setSpacing(0)

        back = QPushButton("All collections")
        back.setObjectName("pcGhost")
        back.setIcon(icons.icon("chevron-left", theme.active().ink2, 14))
        back.clicked.connect(lambda: self._pages.setCurrentIndex(0))
        back.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        col.addWidget(back)
        col.addSpacing(12)

        self._detail_title = QLabel()
        self._detail_title.setObjectName("pcTitle")
        col.addWidget(self._detail_title)
        self._detail_meta = QLabel()
        self._detail_meta.setObjectName("pcSubtitle")
        self._detail_meta.setWordWrap(True)
        col.addWidget(self._detail_meta)

        col.addSpacing(13)
        progress_row = QHBoxLayout()
        progress_row.setSpacing(11)
        self._detail_progress = QLabel()
        self._detail_progress.setObjectName("pcProgress")
        progress_row.addWidget(self._detail_progress)
        # Appears only if a warm is still running after a moment — see _warm_art.
        self._busy = BusyChip()
        progress_row.addWidget(self._busy)
        progress_row.addStretch(1)
        col.addLayout(progress_row)

        col.addSpacing(12)

        # One model and one proxy behind both views, so a chip or a sort applies
        # to whichever is showing and they can never disagree. (Ticket 64.)
        self._model = CollectionSlotModel(self)
        self._proxy = CollectionFilterProxy(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.sort(NUMBER_COL, Qt.AscendingOrder)

        controls = QHBoxLayout()
        controls.setSpacing(9)
        self._view_toggle = _Segmented([("Grid", "grid"), ("List", "list")])
        self._view_toggle.changed.connect(self._set_view_mode)
        controls.addWidget(self._view_toggle)
        self._chips: dict[str, _Chip] = {}
        for label, key in (
            ("All", FILTER_ALL),
            ("In collection", FILTER_FILLED),
            ("Importable", FILTER_IMPORTABLE),
            ("Missing", FILTER_MISSING),
            ("Duplicates", FILTER_DUPLICATES),
        ):
            chip = _Chip(label)
            chip.setChecked(key == FILTER_ALL)
            chip.clicked.connect(lambda _=False, k=key: self._set_state_filter(k))
            self._chips[key] = chip
            controls.addWidget(chip)
        controls.addStretch(1)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter this collection…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(210)
        self._search.textChanged.connect(self._proxy.set_text_filter)
        controls.addWidget(self._search)
        col.addLayout(controls)
        col.addSpacing(9)

        # Grid: one bounded page of large cells, so art decoding is capped by the
        # page size rather than by the collection's length.
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(_CELL_GAP)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self._table = QTableView()
        self._table.setObjectName("pcTable")
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(NUMBER_COL, Qt.AscendingOrder)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setSectionResizeMode(
            CARD_COL, QHeaderView.Stretch
        )
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.doubleClicked.connect(self._table_activated)

        self._view_stack = QStackedWidget()
        self._view_stack.addWidget(self._grid_host)
        self._view_stack.addWidget(self._table)
        col.addWidget(self._view_stack, 1)

        self._pager = Pager()
        self._pager.page_changed.connect(lambda _p: self._render_grid_page())
        col.addWidget(self._pager)

        self._legend = QLabel()
        self._legend.setObjectName("pcNote")
        self._legend.setWordWrap(True)
        col.addWidget(self._legend)
        col.addSpacing(14)
        row.addWidget(body, 1)
        return page

    # -- the two views ------------------------------------------------------

    def _set_view_mode(self, mode: str) -> None:
        self._view_mode = mode
        self._view_stack.setCurrentIndex(0 if mode == "grid" else 1)
        # The pager belongs to the grid; the table scrolls, as tables do.
        self._pager.setVisible(mode == "grid" and self._pager.pages > 1)
        if mode == "grid":
            self._render_grid_page()

    def _set_state_filter(self, key: str) -> None:
        for other, chip in self._chips.items():
            chip.setChecked(other == key)
        self._proxy.set_state_filter(key)

    def _cells_per_page(self) -> int:
        """How many cells fit, from the actual viewport width and height."""
        width = self._view_stack.width() or self.width() or 900
        height = self._view_stack.height() or 460
        cols = max(1, (width + _CELL_GAP) // (_CELL_W + _CELL_GAP))
        rows = max(1, (height + _CELL_GAP) // (_CELL_H + _CELL_GAP))
        return cols * rows

    def _render_grid_page(self) -> None:
        """Draw the current page of the *filtered, sorted* rows."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Detach now, delete later: paging rebuilds this grid, and
                # deleteLater alone leaves the previous page's cells parented and
                # visible until the event loop runs.
                widget.setParent(None)
                widget.deleteLater()
        per = self._cells_per_page()
        total = self._proxy.rowCount()
        pages = max(1, (total + per - 1) // per)
        if pages != self._pager.pages:
            self._pager.set_pages(pages)
        page = min(self._pager.page, pages - 1)
        cols = max(1, (self._view_stack.width() + _CELL_GAP) // (_CELL_W + _CELL_GAP))
        for offset, slot in enumerate(self._proxy.visible_slots(page * per, per)):
            self._grid.addWidget(
                _SlotCell(slot, self._import_slot), offset // cols, offset % cols
            )
        self._pager.setVisible(self._view_mode == "grid" and pages > 1)

    def _table_activated(self, index) -> None:
        """Double-clicking an importable row brings that copy in, as a cell click
        does — the two views offer the same action."""
        slot = index.data(SLOT_ROLE)
        if slot is not None and slot.state == IMPORTABLE:
            self._import_slot(slot)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        # Page size follows the window, so re-paginate rather than clip.
        if getattr(self, "_view_mode", "grid") == "grid" and self._pages.currentIndex():
            self._render_grid_page()

    # -- data ---------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild both states. Called by MainWindow when the data version moves."""
        collections = self._service.collections.list_all()
        progress = {c.id: self._service.progress_for(c) for c in collections}

        self._rebuild_facts(collections, progress)
        self._rebuild_blocks(collections, progress)
        self._rebuild_nav(collections, progress)
        self._empty.setVisible(not collections)
        self._facts.setVisible(bool(collections))

        if self._current is not None:
            current = self._service.collections.get(self._current.id)
            if current is None:
                self._current = None
                self._pages.setCurrentIndex(0)
            else:
                self._current = current
                self._show_detail(current)

    def _rebuild_facts(self, collections, progress) -> None:
        while self._facts_row.count():
            item = self._facts_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        filled = sum(p.filled for p in progress.values())
        total = sum(p.total for p in progress.values())
        importable = sum(p.importable for p in progress.values())
        value = sum(p.value_cents for p in progress.values())
        importable_value = sum(p.importable_value_cents for p in progress.values())
        done = sum(1 for p in progress.values() if p.total and p.filled == p.total)
        # One global number, computed once here rather than per collection — see
        # CollectionService.unmatched_items. Shown only when there are any.
        unmatched = self._service.unmatched_items()
        facts = [
            ("Slots filled", f"{filled:,}", f"of {total:,}"),
            ("Collections", str(len(collections)), f"{done} complete"),
            ("In collection", money(value), "at market"),
            ("Importable", f"{importable:,}", money(importable_value)),
        ]
        if unmatched:
            facts.append(("Unmatched", f"{unmatched:,}", "no card in database"))
        for label, value_text, note in facts:
            self._facts_row.addWidget(_fact(label, value_text, note))

    def _rebuild_blocks(self, collections, progress) -> None:
        while self._blocks.count():
            item = self._blocks.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._grid_sub.setText(
            f"{len(collections)} project{'s' if len(collections) != 1 else ''}"
            " · templates fill themselves from the card database"
        )
        for index, collection in enumerate(collections):
            block = _Block(collection, progress[collection.id], self._open)
            self._blocks.addWidget(block, index // _COLUMNS, index % _COLUMNS)
        self._blocks.setRowStretch(self._blocks.rowCount(), 1)

    def _rebuild_nav(self, collections, progress) -> None:
        self._syncing_nav = True
        try:
            self._nav.clear()
            self._nav_order = list(collections)
            for collection in collections:
                p = progress[collection.id]
                self._nav.addItem(f"{collection.name}   {round(p.percent)}%")
        finally:
            self._syncing_nav = False

    # -- navigation ---------------------------------------------------------

    def _open(self, collection: Collection) -> None:
        """Show one collection, from a block click or the in-window navigator."""
        self._current = collection
        self._pages.setCurrentIndex(1)
        # Move the navigator first, then render — so if Qt does emit a row change
        # despite the guard, the *last* render is still the collection asked for.
        self._sync_nav_to(collection)
        self._show_detail(collection)
        # Also warm on open: collections created before this existed have cold
        # art, and a catalog re-pull can introduce cards with new images.
        self._warm_art(collection)

    def _sync_nav_to(self, collection: Collection) -> None:
        """Highlight a collection's row without treating it as a click.

        Guarded with an explicit flag rather than ``blockSignals``, which does
        **not** cover this case: ``currentRowChanged`` is driven by the list's
        selection model, so moving ``currentRow`` off its initial -1 emits a
        change for row 0 even with the widget's own signals blocked. That fired
        ``_nav_picked(0)`` after the render and left the page showing the first
        collection while the navigator highlighted the one clicked.
        """
        row = next(
            (i for i, c in enumerate(self._nav_order) if c.id == collection.id), -1
        )
        if row < 0:
            return
        self._syncing_nav = True
        try:
            self._nav.setCurrentRow(row)
        finally:
            self._syncing_nav = False

    def _nav_picked(self, row: int) -> None:
        if self._syncing_nav:
            return
        order = getattr(self, "_nav_order", [])
        if 0 <= row < len(order):
            self._current = order[row]
            self._show_detail(order[row])

    def _show_detail(self, collection: Collection) -> None:
        slots = self._service.slots_for(collection)
        p = summarise(slots)
        template = TEMPLATES_BY_ID.get(collection.template)
        self._detail_title.setText(collection.name)
        bits = [template.label if template else collection.template]
        if collection.selector:
            bits.append(collection.selector)
        bits.append(
            "master set — one slot per printing"
            if collection.slot_mode == SLOT_MASTER
            else "one slot per collector number"
        )
        self._detail_meta.setText(" · ".join(bits))
        self._detail_progress.setText(
            f"<b>{p.filled}</b> of {p.total} slots &nbsp;·&nbsp; "
            f"{round(p.percent)}% &nbsp;·&nbsp; {money(p.value_cents)} in collection"
        )

        # Hand the slots to the model; both views read them through the proxy, so
        # neither the sort nor the filter has to be applied twice.
        self._model.set_slots(slots)
        self._pager.set_pages(1, page=0)  # a new collection starts on page 1
        self._render_grid_page()

        notes = [
            f"{p.filled} in the collection · {p.importable} owned as resale "
            f"(click to bring in) · {p.missing} not owned"
        ]
        if p.unknown_treatments:
            notes.append(
                f"{p.unknown_treatments} card(s) have no printing data, so their "
                "slots are shown as one and the master-set total is a floor, not "
                "a count."
            )
        # The unmatched count is portfolio-wide, not per-collection: it used to be
        # recomputed here on every click into a collection, which was the whole of
        # the reported lag. It now lives on the grid page's fact strip, computed
        # once per refresh. (Ticket 64.)
        self._legend.setText("  —  ".join(notes))

    # -- the import ---------------------------------------------------------

    def _import_slot(self, slot: Slot) -> None:
        """Bring one owned copy into the collection.

        A resale lot becomes personal; a lot of more than one is **split** so the
        rest stay as stock. Both go through ``InventoryService``, which audits —
        this view does no ledger writing of its own.
        """
        if not slot.lot_ids:
            return
        lot_id = slot.lot_ids[0]
        try:
            moved = self._inventory.split_lot_for_intent(
                lot_id, 1, Intent.PERSONAL, note=f"brought into “{self._current.name}”"
            )
        except Exception as exc:  # a refusal is a message, not a crash
            QMessageBox.warning(self, "Cannot bring this copy in", str(exc))
            self._conn.rollback()
            return
        self._conn.commit()
        if (
            self._current is not None
            and self._current.template == TEMPLATE_CUSTOM
            and self._current.id is not None
        ):
            self._service.collections.add_card(
                self._current.id, slot.card_id, slot.treatment
            )
            self._conn.commit()
        if moved is not None:
            self.window().statusBar().showMessage(
                f"Kept one {slot.name} for the collection; the rest stay as stock.",
                6000,
            ) if hasattr(self.window(), "statusBar") else None
        self.changed.emit()
        self.refresh()

    # -- background art warm -------------------------------------------------

    # Below this, a warm is not worth telling anyone about: it finishes before a
    # spinner would finish fading in. Measured, most collections need 0-4 images.
    _WARM_ANNOUNCE_AT = 3
    # Long enough that a quick warm never flashes a spinner at anyone.
    _WARM_REVEAL_MS = 400

    def _warm_art(self, collection: Collection | None) -> int:
        """Fetch any missing card art for ``collection`` in the background.

        Returns how many images were queued, so callers and tests can tell the
        difference between "nothing to do" and "started".

        Cards the catalog has **no image URL for** are not work and are not
        queued: on the real catalog they are the large majority of missing art
        (a 5,805-slot rarity run had 224 without art, 220 of them with no URL),
        and queueing them would retry a permanent gap on every open.
        """
        if collection is None:
            return 0
        gap = art_gap(self._service.slots_for(collection))
        if not gap.fetchable:
            return 0
        # Never overwrite a running worker; park the old one until it stops.
        retire(self._warm, self._parked_warms)
        worker = ArtWarmWorker(gap.fetchable)
        self._warm = worker
        total = len(gap.fetchable)
        worker.progress.connect(self._on_warm_progress)
        worker.finished_with.connect(self._on_warm_finished)
        if total >= self._WARM_ANNOUNCE_AT:
            self._busy.begin(
                f"Fetching card art · 0 of {total}", delay_ms=self._WARM_REVEAL_MS
            )
        worker.start()
        return total

    def _on_warm_progress(self, done: int, total: int) -> None:
        if total >= self._WARM_ANNOUNCE_AT:
            self._busy.update_text(f"Fetching card art · {done} of {total}")

    def _on_warm_finished(self, fetched: int, failed: int) -> None:
        self._busy.end()
        if not fetched:
            return
        # New files on disk; the thumbnail cache holds "no art" as a decoded
        # absence, so it has to be dropped before the grid will show them.
        card_thumbs.clear()
        if self._pages.currentIndex() == 1:
            self._render_grid_page()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Stop a warm rather than leaving a thread downloading after teardown."""
        retire(self._warm, self._parked_warms)
        super().closeEvent(event)

    def _new_collection(self) -> None:
        dialog = _NewCollectionDialog(self._service, self)
        if dialog.exec() != QDialog.Accepted:
            return
        created = self._service.collections.add(dialog.to_collection())
        self._conn.commit()
        self.refresh()
        # Warm the new collection's art now rather than letting it fill in over
        # the network the first time it is opened.
        self._warm_art(created)


def _fact(label: str, value: str, note: str) -> QWidget:
    box = QFrame()
    box.setObjectName("pcFact")
    col = QVBoxLayout(box)
    col.setContentsMargins(16, 10, 16, 11)
    col.setSpacing(1)
    key = QLabel(label.upper())
    key.setObjectName("pcFactKey")
    col.addWidget(key)
    val = QLabel(value)
    val.setObjectName("pcFactValue")
    col.addWidget(val)
    sub = QLabel(note)
    sub.setObjectName("pcFactNote")
    col.addWidget(sub)
    return box


def _qss(t) -> str:
    """Every colour from a theme token — no literals (``THEME-TOKENS``)."""
    return f"""
    QLabel#pcTitle {{ font-size:20px; font-weight:600; color:{t.ink}; }}
    QLabel#pcSubtitle {{ font-size:12px; color:{t.ink3}; }}
    QLabel#pcEyebrow {{ font-size:10px; font-weight:600; letter-spacing:1px;
        color:{t.ink3}; }}
    QLabel#pcNote {{ font-size:11.5px; color:{t.ink3}; }}
    QLabel#pcEmpty {{ font-size:13px; color:{t.ink2}; }}
    QLabel#pcProgress {{ font-size:13px; color:{t.ink2}; }}

    QPushButton#pcPrimary {{ background:{t.accent}; color:{t.accent_on};
        border:none; border-radius:6px; padding:7px 13px; font-weight:600; }}
    QPushButton#pcPrimary:hover {{ background:{t.accent_ink}; }}
    QPushButton#pcGhost {{ background:{t.surface2}; color:{t.ink};
        border:1px solid {t.line2}; border-radius:6px; padding:6px 11px; }}
    QPushButton#pcGhost:hover {{ border-color:{t.ink3}; }}

    QFrame#pcFacts {{ background:{t.surface}; border:1px solid {t.line};
        border-radius:9px; }}
    QFrame#pcFact {{ border-right:1px solid {t.line}; }}
    QLabel#pcFactKey {{ font-size:9.5px; font-weight:600; letter-spacing:1px;
        color:{t.ink3}; }}
    QLabel#pcFactValue {{ font-size:17px; font-weight:500; color:{t.ink}; }}
    QLabel#pcFactNote {{ font-size:10.5px; color:{t.ink3}; }}

    QFrame#pcBlock, QFrame#pcBlockDone {{ background:{t.surface};
        border:1px solid {t.line}; border-radius:9px; }}
    QFrame#pcBlock:hover, QFrame#pcBlockDone:hover {{ border-color:{t.accent}; }}
    QLabel#pcBlockTitle {{ font-size:14px; font-weight:600; color:{t.ink}; }}
    QLabel#pcBlockSub {{ font-size:11px; color:{t.ink3}; }}
    QLabel#pcCount {{ font-size:12px; color:{t.ink2}; }}
    QLabel#pcPct {{ font-size:12px; font-weight:600; color:{t.accent_ink}; }}
    QLabel#pcPctDone {{ font-size:12px; font-weight:600; color:{t.pos}; }}
    QLabel#pcImportHint {{ font-size:10.5px; color:{t.info}; }}
    QLabel#pcIcon {{ background:{t.surface2}; border:1px solid {t.line};
        border-radius:7px; }}
    QFrame#pcBar {{ background:{t.surface2}; border:1px solid {t.line};
        border-radius:3px; }}
    QFrame#pcBarFill {{ background:{t.accent}; border-radius:3px; }}
    QFrame#pcBarDone {{ background:{t.pos}; border-radius:3px; }}

    QListWidget#pcNav {{ background:{t.surface}; border-right:1px solid {t.line};
        color:{t.ink2}; font-size:12.5px; outline:none; }}
    QListWidget#pcNav::item {{ padding:7px 14px; }}
    QListWidget#pcNav::item:selected {{ background:{t.accent_soft};
        color:{t.ink}; border-left:2px solid {t.accent}; }}

    /* Checklist cells. The frame carries the state; the art is the card's own
       whether owned or not, because a picture of what is missing is the point. */
    QFrame#pcCellFilled {{ background:{t.accent_soft};
        border:1px solid {t.accent}; border-radius:6px; }}
    QFrame#pcCellImportable {{ background:{t.surface2};
        border:1px solid {t.info}; border-radius:6px; }}
    QFrame#pcCellMissing {{ background:{t.surface};
        border:1px dashed {t.line2}; border-radius:6px; }}
    QFrame#pcCellFilled:hover, QFrame#pcCellImportable:hover,
    QFrame#pcCellMissing:hover {{ border-color:{t.accent}; }}
    QLabel#pcCellArtNone {{ color:{t.ink3}; font-size:12px; font-weight:600; }}
    QFrame#pcCellFilled QLabel#pcCellCaption {{ color:{t.ink};
        font-size:10.5px; font-weight:600; }}
    QFrame#pcCellImportable QLabel#pcCellCaption {{ color:{t.info};
        font-size:10.5px; font-weight:600; }}
    QFrame#pcCellMissing QLabel#pcCellCaption {{ color:{t.ink3};
        font-size:10.5px; }}

    QTableView#pcTable {{ background:{t.surface}; alternate-background-color:{t.zebra};
        gridline-color:{t.line}; color:{t.ink2}; border:1px solid {t.line};
        border-radius:8px; font-size:12.5px; }}
    QTableView#pcTable::item:selected {{ background:{t.accent_soft};
        color:{t.ink}; }}
    QTableView#pcTable QHeaderView::section {{ background:{t.surface2};
        color:{t.ink3}; border:none; border-bottom:1px solid {t.line};
        padding:6px 10px; font-size:10px; font-weight:600; }}
    """
