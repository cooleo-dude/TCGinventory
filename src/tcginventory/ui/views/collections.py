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
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from tcginventory.domain.enums import Intent
from tcginventory.domain.models import Collection
from tcginventory.services.collections import (
    FILLED,
    IMPORTABLE,
    SLOT_BY_NUMBER,
    SLOT_MASTER,
    TEMPLATE_CUSTOM,
    TEMPLATES,
    TEMPLATES_BY_ID,
    CollectionService,
    Slot,
    summarise,
)
from tcginventory.services.inventory import InventoryService
from tcginventory.ui import icons, theme
from tcginventory.ui.formatting import money

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


class _SlotTile(QFrame):
    """One position in the checklist — owned, importable, or an empty slot."""

    def __init__(self, slot: Slot, on_import) -> None:
        super().__init__()
        self.slot = slot
        self.setObjectName(
            {
                FILLED: "pcSlotFilled",
                IMPORTABLE: "pcSlotImportable",
            }.get(slot.state, "pcSlotMissing")
        )
        self.setFixedSize(52, 72)
        self._on_import = on_import
        label = QLabel(slot.number or "?", self)
        label.setObjectName("pcSlotNumber")
        label.setAlignment(Qt.AlignCenter)
        label.setGeometry(0, 0, 52, 72)
        label.setWordWrap(True)

        detail = f"{slot.name} · {slot.number or '?'}"
        if slot.treatment:
            detail += f" · {slot.treatment}"
        elif slot.treatments_unknown:
            detail += " · printings unknown"
        if slot.state == FILLED:
            detail += "\nIn this collection"
        elif slot.state == IMPORTABLE:
            detail += "\nIn inventory as resale — click to bring in"
            self.setCursor(Qt.PointingHandCursor)
        else:
            detail += "\nNot owned"
        if slot.market_value_cents:
            detail += f"\n{money(slot.market_value_cents)}"
        self.setToolTip(detail)

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
        self._detail_progress = QLabel()
        self._detail_progress.setObjectName("pcProgress")
        col.addWidget(self._detail_progress)

        col.addSpacing(12)
        col.addWidget(_eyebrow("Checklist"))
        col.addSpacing(7)
        self._slots_host = QWidget()
        self._slots = QGridLayout(self._slots_host)
        self._slots.setContentsMargins(0, 0, 0, 0)
        self._slots.setSpacing(6)
        self._slots.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self._slots_host)
        col.addWidget(scroll, 1)

        self._legend = QLabel()
        self._legend.setObjectName("pcNote")
        self._legend.setWordWrap(True)
        col.addWidget(self._legend)
        col.addSpacing(14)
        row.addWidget(body, 1)
        return page

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
        facts = [
            ("Slots filled", f"{filled:,}", f"of {total:,}"),
            ("Collections", str(len(collections)), f"{done} complete"),
            ("In collection", money(value), "at market"),
            ("Importable", f"{importable:,}", money(importable_value)),
        ]
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

        while self._slots.count():
            item = self._slots.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        per_row = 14
        for index, slot in enumerate(slots):
            self._slots.addWidget(
                _SlotTile(slot, self._import_slot), index // per_row, index % per_row
            )

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
        unmatched = self._service.unmatched_items(collection.language)
        if unmatched:
            notes.append(
                f"{unmatched} owned card(s) match no card in the database, so they "
                "cannot fill a slot. They are still in Inventory."
            )
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

    def _new_collection(self) -> None:
        dialog = _NewCollectionDialog(self._service, self)
        if dialog.exec() != QDialog.Accepted:
            return
        self._service.collections.add(dialog.to_collection())
        self._conn.commit()
        self.refresh()


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

    QFrame#pcSlotFilled {{ background:{t.accent_soft};
        border:1px solid {t.accent}; border-radius:4px; }}
    QFrame#pcSlotImportable {{ background:{t.surface2};
        border:1px solid {t.info}; border-radius:4px; }}
    QFrame#pcSlotMissing {{ background:transparent;
        border:1px dashed {t.line2}; border-radius:4px; }}
    QFrame#pcSlotFilled QLabel#pcSlotNumber {{ color:{t.ink};
        font-size:10px; font-weight:600; }}
    QFrame#pcSlotImportable QLabel#pcSlotNumber {{ color:{t.info};
        font-size:10px; font-weight:600; }}
    QFrame#pcSlotMissing QLabel#pcSlotNumber {{ color:{t.ink3}; font-size:10px; }}
    """
