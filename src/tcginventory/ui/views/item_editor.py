"""Item editor dialog — the Inventory row editor (F-IT-3/4/5/6, F-VW-2).

Opened by double-clicking an Inventory row. **Every editable field is on this one
screen**; nothing is behind a second dialog. It replaced a pair — a read-only
``ItemDetailDialog`` whose only way to change anything was an "Edit item…" button
that opened a separate form — because the read-only half duplicated the dossier
panel already sitting beside the grid.

Items are created automatically by transactions; this only *edits* them. Fix
identity typos (name/set/number/variant/condition/language/grading), refresh
market value and links, set location/notes, toggle review, and adjust the fields
that belong to **this copy** (its lot): intent, the market-value-at-acquisition
snapshot, source, notes, and the stolen/missing impairment. Delete is guarded —
blocked while any lot or line references the item (use an adjustment-out
instead), matching the ratified soft-delete rule.

Two scoping rules the layout has to make visible, because getting them wrong is
a silent data surprise rather than an error:

* A grid row is a **lot**, but most fields here live on the shared **item**. When
  a card has more than one copy, editing Identity / Pricing / Filing changes all
  of them — the section captions say so, and Variant points at "Separate this
  copy…" (ticket 52).
* Editing identity fields does not rewrite existing lot SKUs. Those are minted at
  acquisition and remain stable lookup keys, so SKU and Acquired are shown but
  not editable.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import (
    ItemRepository,
    LotRepository,
    TransactionLineRepository,
    TransactionRepository,
)
from tcginventory.domain.enums import (
    Category,
    Condition,
    Edition,
    GradingCompany,
    HoloPattern,
    Intent,
    Language,
)
from tcginventory.services.inventory import InventoryService
from tcginventory.services.review import with_review_note, without_review_note
from tcginventory.ui import icons, theme
from tcginventory.ui.formatting import money

_HOLO_LABEL = {"H": "Holo", "RH": "Reverse Holo"}


# ---------------------------------------------------------------------------
# small themed building blocks
# ---------------------------------------------------------------------------


def _caption(text: str, note: str | None = None) -> QWidget:
    """A section caption in the house style: uppercase, letter-spaced, ink3 —
    with an optional quieter note to its right (used for "applies to all N
    copies", which is the ticket-52 guard rail made visible)."""
    t = theme.active()
    box = QWidget()
    # Fixed height, or the caption box absorbs a stretched panel's slack and the
    # title floats down into the middle of its own section.
    box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    cap = QLabel(text.upper())
    cap.setStyleSheet(
        f"color:{t.ink3}; font-size:10px; font-weight:700; letter-spacing:1.2px;"
    )
    row.addWidget(cap)
    if note:
        hint = QLabel(note)
        hint.setStyleSheet(f"color:{t.ink3}; font-size:10px; font-style:italic;")
        row.addWidget(hint)
    row.addStretch(1)
    return box


def _section(caption: str, note: str | None = None) -> tuple[QFrame, QFormLayout]:
    """A titled card panel; returns (frame, the form layout to add rows to)."""
    t = theme.active()
    frame = QFrame()
    frame.setObjectName("editorSection")
    frame.setStyleSheet(
        f"QFrame#editorSection{{background:{t.surface2}; border:1px solid {t.line};"
        " border-radius:12px;}"
    )
    outer = QVBoxLayout(frame)
    outer.setContentsMargins(14, 12, 14, 13)
    outer.setSpacing(10)
    outer.addWidget(_caption(caption, note))
    form = QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setSpacing(8)
    form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    outer.addLayout(form)
    # Side-by-side panels share a grid row's height, so the shorter one has slack.
    # Park it at the bottom rather than letting it stretch the fields apart.
    outer.addStretch(1)
    return frame, form


def _row(
    form: QFormLayout, label: str, widget: QWidget, hint: str | None = None
) -> None:
    """One labelled field, optionally with a small explanatory line beneath it."""
    t = theme.active()
    lbl = QLabel(label)
    lbl.setStyleSheet(f"color:{t.ink2};")
    if hint is None:
        form.addRow(lbl, widget)
        return
    holder = QWidget()
    col = QVBoxLayout(holder)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(3)
    col.addWidget(widget)
    sub = QLabel(hint)
    sub.setWordWrap(True)
    sub.setStyleSheet(f"color:{t.ink3}; font-size:10px;")
    col.addWidget(sub)
    form.addRow(lbl, holder)


def _kpi(caption: str) -> tuple[QFrame, QLabel]:
    """A dashboard-style summary tile; returns (frame, value_label)."""
    t = theme.active()
    tile = QFrame()
    tile.setObjectName("editorTile")
    tile.setStyleSheet(
        f"QFrame#editorTile{{background:{t.surface2}; border:1px solid {t.line};"
        " border-radius:12px;}"
    )
    lay = QVBoxLayout(tile)
    lay.setContentsMargins(15, 9, 18, 10)
    lay.setSpacing(2)
    cap = QLabel(caption.upper())
    cap.setStyleSheet(
        f"color:{t.ink3}; font-size:10px; font-weight:700; letter-spacing:1px;"
    )
    value = QLabel("—")
    value.setFont(QFont(theme.mono_family()))
    value.setStyleSheet(f"color:{t.ink}; font-size:18px; font-weight:700;")
    lay.addWidget(cap)
    lay.addWidget(value)
    return tile, value


def _readonly_field(text: str) -> QLabel:
    """A value that is shown but deliberately not editable (SKU, Acquired)."""
    t = theme.active()
    lbl = QLabel(text or "—")
    lbl.setFont(QFont(theme.mono_family()))
    lbl.setStyleSheet(f"color:{t.ink2};")
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lbl


def _money_spin() -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setPrefix("$")
    spin.setDecimals(2)
    spin.setMaximum(10_000_000.0)
    spin.setGroupSeparatorShown(True)
    return spin


def _cents(spin: QDoubleSpinBox) -> int:
    """The one place a money spinbox becomes integer cents (MONEY-CENTS)."""
    return round(spin.value() * 100)


def _readonly_item(text: str, numeric: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    if numeric:
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return item


def _tidy(table: QTableWidget) -> None:
    """Ledger tables size to their content, height included.

    Stretching *every* column (what the old detail dialog did) gave SKU the same
    width as Source and left the numbers swimming; the slack goes to the last
    column instead, where it reads as margin. And the height is pinned rather
    than left Expanding, so a one-lot card gets a two-line table instead of the
    fixed void the old dialog left below it.
    """
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setAlternatingRowColors(True)
    header = table.horizontalHeader()
    for col in range(table.columnCount()):
        header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
    header.setStretchLastSection(True)
    row_h = 26
    table.verticalHeader().setDefaultSectionSize(row_h)
    wanted = header.height() + max(table.rowCount(), 1) * row_h + 4
    table.setFixedHeight(min(wanted, 8 * row_h + header.height()))


def _subline(item) -> str:
    """set · number · variant · lang · holo · edition — the identity at a glance.

    Mirrors the grid's ``subline_for``, but that one takes an InventoryRow and
    this dialog holds an Item.
    """
    if item is None:
        return ""
    parts: list[str] = []
    if item.set_name:
        parts.append(item.set_name)
    if item.card_number:
        parts.append(item.card_number)
    if item.variant:
        parts.append(item.variant)
    if item.language:
        parts.append(item.language.value)
    if item.is_graded:
        company = item.grading_company.value if item.grading_company else "?"
        parts.append(f"{company} {item.grade or ''}".strip())
    elif item.condition:
        parts.append(item.condition.value)
    if item.holo_pattern and item.holo_pattern.value != "NH":
        parts.append(_HOLO_LABEL.get(item.holo_pattern.value, item.holo_pattern.value))
    if item.edition and item.edition.value != "N/A":
        parts.append(item.edition.value)
    return "  ·  ".join(parts)


# ---------------------------------------------------------------------------
# the dialog
# ---------------------------------------------------------------------------


class ItemEditorDialog(QDialog):
    """Edit a card and, when opened from an Inventory row, that row's copy.

    ``lot_id`` is optional: without it the per-copy section is hidden and this is
    a plain item editor (which is how it is reached from a bare item, and how the
    editing tests construct it).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        item_id: int,
        lot_id: int | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._items = ItemRepository(conn)
        self._lots = LotRepository(conn)
        self._service = InventoryService(conn)
        self._item = self._items.get(item_id)
        self._lot = self._lots.get(lot_id) if lot_id is not None else None
        item = self._item
        t = theme.active()

        self.setWindowTitle(f"{item.name if item else 'Item'} — edit")
        # Wide rather than tall: the sections sit two to a row, so a ribbon-shaped
        # dialog is what the old flat form looked like. Qt clamps to the screen.
        self.resize(1080, 880)

        # How many copies share this item: decides whether the item-scoped
        # sections need the "applies to all N copies" warning.
        self._sibling_lots = self._lots.list_for_item(item_id) if item else []
        shared = len(self._sibling_lots)
        self._shared_note = (
            f"applies to all {shared} copies of this card" if shared > 1 else None
        )

        self._build_widgets(item)
        body = self._build_body(item)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)
        root.addLayout(self._build_header(item))
        root.addWidget(body, 1)
        root.addLayout(self._build_footer(t))

        # Ctrl+S saves, matching the Transaction window.
        QShortcut(QKeySequence.Save, self, self._save)

        # Graded state last: setChecked fires _toggle_graded, which greys the
        # condition combo and enables the grade fields.
        self._toggle_graded(item.is_graded if item else False)
        if item:
            self._graded.setChecked(item.is_graded)

    # -- widgets ------------------------------------------------------------

    def _build_widgets(self, item) -> None:
        """Every editable control, in one place. Attribute names are load-bearing:
        the editing tests drive ``_condition`` / ``_language`` / ``_graded`` /
        ``_company`` / ``_grade`` / ``_review`` directly."""
        self._name = QLineEdit(item.name if item else "")

        self._category = QComboBox()
        for cat in Category:
            self._category.addItem(cat.value.capitalize(), cat.value)
        if item:
            self._select(self._category, item.category.value)

        self._set_code = QLineEdit(item.set_code or "" if item else "")
        self._set_name = QLineEdit(item.set_name or "" if item else "")
        self._card_number = QLineEdit(item.card_number or "" if item else "")
        self._variant = QLineEdit(item.variant or "" if item else "")
        self._variant.setPlaceholderText("Base print")
        self._variant.setToolTip(
            'Which printing this is ("151 Metal Card"). Part of the card\'s '
            "identity, so changing it makes this a different item."
        )

        self._language = QComboBox()
        for lang in Language:
            self._language.addItem(lang.value, lang.value)
        if item:
            self._select(self._language, item.language.value)

        self._graded = QCheckBox("This card is graded")
        self._graded.toggled.connect(self._toggle_graded)

        self._condition = QComboBox()
        for cond in Condition:
            self._condition.addItem(cond.value, cond.value)
        if item and item.condition:
            self._select(self._condition, item.condition.value)
        # Connected *after* the initial select, so loading the dialog never
        # auto-flags review; editing to a non-NM condition afterwards does.
        self._condition.currentIndexChanged.connect(self._maybe_flag_review)

        self._holo = QComboBox()
        for pattern in HoloPattern:
            self._holo.addItem(pattern.value, pattern.value)
        if item and item.holo_pattern:
            self._select(self._holo, item.holo_pattern.value)

        self._edition = QComboBox()
        for ed in Edition:
            self._edition.addItem(ed.value, ed.value)
        if item and item.edition:
            self._select(self._edition, item.edition.value)

        self._company = QComboBox()
        for company in GradingCompany:
            self._company.addItem(company.value, company.value)
        if item and item.grading_company:
            self._select(self._company, item.grading_company.value)
        self._grade = QLineEdit(item.grade or "" if item else "")
        self._cert = QLineEdit(item.cert_number or "" if item else "")

        self._market = _money_spin()
        if item and item.market_value_cents is not None:
            self._market.setValue(item.market_value_cents / 100)

        self._location = QLineEdit(item.location or "" if item else "")
        self._tcgplayer = QLineEdit(item.tcgplayer_url or "" if item else "")
        self._ebay = QLineEdit(item.ebay_url or "" if item else "")
        # Blank → a derived eBay sold-listings search for this card is used
        # automatically. Show it as the placeholder so it's visible; typing a URL
        # overrides it.
        if item:
            from tcginventory.services.ebay import sold_search_url

            self._ebay.setPlaceholderText(sold_search_url(item))

        # Multi-line: notes are where a condition description or provenance goes,
        # and `without_review_note` now preserves line structure so toggling the
        # review flag no longer flattens them.
        self._notes = QPlainTextEdit(
            without_review_note(item.notes) or "" if item else ""
        )
        self._notes.setPlaceholderText("Provenance, flaws, anything worth keeping…")
        self._notes.setFixedHeight(74)

        self._review = QCheckBox("Mark for review (price is manual, poll-protected)")
        self._review.setToolTip(
            "Freeze this card's shown market price and keep auto-refresh from "
            "overwriting it — same as entering it with Mark for review."
        )
        if item:
            self._review.setChecked(item.review)

        # -- per-copy (lot) controls, only when opened from an Inventory row.
        lot = self._lot
        self._intent = QComboBox()
        for intent in Intent:
            self._intent.addItem(intent.value.capitalize(), intent.value)
        if lot:
            self._select(self._intent, lot.intent.value)

        self._lot_market = _money_spin()
        if lot:
            self._lot_market.setValue(lot.acquired_market_value_cents / 100)

        self._source = QLineEdit(lot.source or "" if lot else "")
        self._source.setPlaceholderText("Where this copy came from")
        self._lot_notes = QLineEdit(lot.notes or "" if lot else "")
        self._lot_notes.setPlaceholderText("Notes about this copy only")

        self._missing = QCheckBox("Stolen / missing")
        self._missing.setToolTip(
            "A reversible write-down: this copy stops counting as on hand and is "
            "valued at $0, but keeps its cost basis. Unticking restores it."
        )
        if lot:
            self._missing.setChecked(lot.missing)

    # -- layout -------------------------------------------------------------

    def _build_header(self, item) -> QVBoxLayout:
        t = theme.active()
        head = QVBoxLayout()
        head.setSpacing(3)

        name = QLabel(item.name if item else "Item not found")
        name.setStyleSheet(f"color:{t.ink}; font-size:17px; font-weight:700;")
        name.setWordWrap(True)
        head.addWidget(name)

        sub = QLabel(_subline(item))
        sub.setFont(QFont(theme.mono_family()))
        sub.setStyleSheet(f"color:{t.ink3}; font-size:11px;")
        sub.setWordWrap(True)
        head.addWidget(sub)

        if item is None:
            return head

        # The position tiles — the same four figures the dossier reads, so the
        # dialog opens on the numbers rather than a wall of inputs.
        pos = self._service.item_position(item.id)
        tiles = QHBoxLayout()
        tiles.setContentsMargins(0, 7, 0, 0)
        tiles.setSpacing(11)
        for caption, text, color in (
            ("On hand", str(pos.remaining_qty), t.ink),
            ("Cost basis", money(pos.cost_basis_cents), t.ink),
            ("Market", money(pos.market_value_cents), t.ink),
            (
                "Unrealized",
                money(pos.unrealized_pl_cents),
                t.pos
                if pos.unrealized_pl_cents > 0
                else (t.neg if pos.unrealized_pl_cents < 0 else t.ink),
            ),
        ):
            tile, value = _kpi(caption)
            value.setText(text)
            value.setStyleSheet(f"color:{color}; font-size:18px; font-weight:700;")
            tiles.addWidget(tile)
        tiles.addStretch(1)
        head.addLayout(tiles)
        return head

    def _build_body(self, item) -> QWidget:
        """The sections, two to a row, inside a scroll area so a small screen
        still reaches the footer."""
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        identity, form = _section("Identity", self._shared_note)
        _row(form, "Name", self._name)
        _row(form, "Category", self._category)
        _row(form, "Set code", self._set_code)
        _row(form, "Set", self._set_name)
        _row(form, "Card #", self._card_number)
        _row(
            form,
            "Variant",
            self._variant,
            "The printing this card is. Changing it here re-identifies every copy"
            " — to split one copy off, use “Separate this copy…” on its row."
            if self._shared_note
            else "The printing this card is, e.g. “151 Metal Card”.",
        )
        _row(form, "Language", self._language)
        grid.addWidget(identity, 0, 0)

        grading, form = _section("Grading & finish")
        form.addRow(self._graded)  # spans both columns — it gates the rest
        _row(form, "Condition", self._condition, "Greyed out while graded.")
        _row(form, "Holo pattern", self._holo)
        _row(form, "Edition", self._edition)
        _row(form, "Grading company", self._company)
        _row(form, "Grade", self._grade)
        _row(form, "Cert #", self._cert)
        grid.addWidget(grading, 0, 1)

        pricing, form = _section("Pricing & links", self._shared_note)
        _row(
            form,
            "Market value",
            self._market,
            "The shown current market price. Marking for review freezes it "
            "against the background price refresh.",
        )
        _row(form, "TCGplayer URL", self._tcgplayer)
        _row(
            form, "eBay URL", self._ebay, "Blank uses the derived sold-listings search."
        )
        if self._lot is not None:
            grid.addWidget(pricing, 1, 0)
            grid.addWidget(self._build_copy_section(), 1, 1)
        else:
            # No row to edit, so nothing sits beside it — take the full width
            # rather than leaving half the panel empty.
            grid.addWidget(pricing, 1, 0, 1, 2)

        filing, form = _section("Filing", self._shared_note)
        _row(form, "Location", self._location)
        _row(form, "Notes", self._notes)
        form.addRow(self._review)
        grid.addWidget(filing, 2, 0, 1, 2)

        if item is not None:
            grid.addWidget(self._build_ledger_section(item.id), 3, 0, 1, 2)
        # Slack goes below the last panel, not into stretching the sections.
        grid.setRowStretch(4, 1)

        inner = QWidget()
        inner.setLayout(grid)
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;}")
        inner.setStyleSheet("background:transparent;")
        return scroll

    def _build_copy_section(self) -> QFrame:
        lot = self._lot
        section, form = _section("This copy", "this row only")
        _row(form, "SKU", _readonly_field(lot.sku or ""))
        _row(
            form,
            "Acquired",
            _readonly_field(lot.acquired_at),
            "Minted at acquisition — SKU and date are stable lookup keys, and the"
            " date is what FIFO orders disposals by.",
        )
        _row(form, "Intent", self._intent)
        _row(
            form,
            "Market at purchase",
            self._lot_market,
            "A record fix only: correcting the acquisition snapshot does not "
            "re-run cost allocation, so cost basis and P/L are untouched.",
        )
        _row(form, "Source", self._source)
        _row(form, "Copy notes", self._lot_notes)
        _row(form, "", self._missing)
        return section

    def _build_ledger_section(self, item_id: int) -> QFrame:
        t = theme.active()
        frame = QFrame()
        frame.setObjectName("editorSection")
        frame.setStyleSheet(
            f"QFrame#editorSection{{background:{t.surface2}; border:1px solid {t.line};"
            " border-radius:12px;}"
        )
        col = QVBoxLayout(frame)
        col.setContentsMargins(14, 12, 14, 13)
        col.setSpacing(9)
        col.addWidget(_caption("Ledger", "read-only"))
        col.addWidget(_caption("Copies of this card"))
        col.addWidget(self._lots_table(item_id))
        col.addWidget(_caption("Transaction history"))
        col.addWidget(self._history_table(item_id))
        return frame

    def _build_footer(self, t) -> QHBoxLayout:
        footer = QHBoxLayout()
        footer.setSpacing(9)
        delete = QPushButton("  Delete…")
        delete.setIcon(icons.icon("trash-2", t.neg, 15))
        delete.setStyleSheet(f"color:{t.neg};")
        delete.setToolTip("Only possible while the card has no history")
        delete.clicked.connect(self._delete)
        footer.addWidget(delete)
        footer.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        save = QPushButton("Save")
        save.setObjectName("primary")  # the accent primary-button style
        save.setDefault(True)
        save.clicked.connect(self._save)
        footer.addWidget(save)
        return footer

    # -- ledger tables ------------------------------------------------------

    def _lots_table(self, item_id: int) -> QTableWidget:
        lots = self._sibling_lots or self._lots.list_for_item(item_id)
        headers = [
            "SKU",
            "Acquired",
            "Qty",
            "Remaining",
            "Unit Cost",
            "Mkt @ Acq",
            "Intent",
            "Source",
        ]
        table = QTableWidget(len(lots), len(headers))
        table.setHorizontalHeaderLabels(headers)
        for r, lot in enumerate(lots):
            table.setItem(r, 0, _readonly_item(lot.sku or ""))
            table.setItem(r, 1, _readonly_item(lot.acquired_at))
            table.setItem(r, 2, _readonly_item(str(lot.quantity), numeric=True))
            table.setItem(r, 3, _readonly_item(str(lot.remaining_qty), numeric=True))
            table.setItem(
                r, 4, _readonly_item(money(lot.unit_cost_cents), numeric=True)
            )
            table.setItem(
                r,
                5,
                _readonly_item(money(lot.acquired_market_value_cents), numeric=True),
            )
            table.setItem(r, 6, _readonly_item(lot.intent.value))
            table.setItem(r, 7, _readonly_item(lot.source or ""))
        _tidy(table)
        return table

    def _history_table(self, item_id: int) -> QTableWidget:
        lines = TransactionLineRepository(self._conn).list_for_item(item_id)
        txn_repo = TransactionRepository(self._conn)
        headers = ["Date", "Type", "Dir", "Qty", "Unit Price", "Realized", "Status"]
        table = QTableWidget(len(lines), len(headers))
        table.setHorizontalHeaderLabels(headers)
        for r, line in enumerate(lines):
            txn = txn_repo.get(line.transaction_id)
            table.setItem(r, 0, _readonly_item(txn.occurred_at if txn else ""))
            table.setItem(r, 1, _readonly_item(txn.type.value if txn else ""))
            table.setItem(r, 2, _readonly_item(line.direction.value))
            table.setItem(r, 3, _readonly_item(str(line.quantity), numeric=True))
            table.setItem(
                r, 4, _readonly_item(money(line.unit_price_cents), numeric=True)
            )
            table.setItem(
                r, 5, _readonly_item(money(line.realized_pl_cents), numeric=True)
            )
            table.setItem(r, 6, _readonly_item(txn.status.value if txn else ""))
        _tidy(table)
        return table

    # -- behaviour ----------------------------------------------------------

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        pos = combo.findData(value)
        if pos >= 0:
            combo.setCurrentIndex(pos)

    def _maybe_flag_review(self) -> None:
        # Sync review to condition (ungraded): worse-than-NM marks for review,
        # switching back to NM clears it.
        if not self._graded.isChecked():
            self._review.setChecked(self._condition.currentData() != Condition.NM.value)

    def _toggle_graded(self, graded: bool) -> None:
        # Condition greys out when graded; grade fields enable (Spec 02).
        self._condition.setEnabled(not graded)
        self._company.setEnabled(graded)
        self._grade.setEnabled(graded)
        self._cert.setEnabled(graded)

    def _save(self) -> None:
        if self._item is None:
            self.reject()
            return
        item = self._item
        graded = self._graded.isChecked()
        item.name = self._name.text().strip() or item.name
        item.category = Category(self._category.currentData())
        item.set_code = self._set_code.text().strip() or None
        item.set_name = self._set_name.text().strip() or None
        item.card_number = self._card_number.text().strip() or None
        item.variant = self._variant.text().strip() or None
        item.language = Language(self._language.currentData())
        item.is_graded = graded
        item.condition = None if graded else Condition(self._condition.currentData())
        item.holo_pattern = HoloPattern(self._holo.currentData())
        item.edition = Edition(self._edition.currentData())
        item.grading_company = (
            GradingCompany(self._company.currentData()) if graded else None
        )
        item.grade = (self._grade.text().strip() or None) if graded else None
        item.cert_number = (self._cert.text().strip() or None) if graded else None
        item.market_value_cents = _cents(self._market)
        item.market_value_asof = now_iso()[:10]
        item.location = self._location.text().strip() or None
        item.tcgplayer_url = self._tcgplayer.text().strip() or None
        item.ebay_url = self._ebay.text().strip() or None
        item.notes = self._notes.toPlainText().strip() or None
        item.review = self._review.isChecked()
        # Keep the review note in sync so it matches entry / inline-edit logic.
        item.notes = (
            with_review_note(item.notes)
            if item.review
            else without_review_note(item.notes)
        )
        self._items.update(item)
        self._save_lot()
        self._conn.commit()
        self.accept()

    def _save_lot(self) -> None:
        """Persist the per-copy fields. Caller commits.

        ``missing`` goes through the service rather than the repository: it is a
        reversible impairment that moves money, so it writes an append-only
        ``audit_log`` row (Invariant 9). Flipping ``lot.missing`` here would skip
        that.
        """
        lot = self._lot
        if lot is None:
            return
        lot.intent = Intent(self._intent.currentData())
        lot.acquired_market_value_cents = _cents(self._lot_market)
        lot.source = self._source.text().strip() or None
        lot.notes = self._lot_notes.text().strip() or None
        self._lots.update(lot)
        if self._missing.isChecked() != lot.missing:
            self._service.set_lot_missing(lot.id, self._missing.isChecked())

    def _delete(self) -> None:
        if self._item is None:
            return
        lots = self._lots.list_for_item(self._item.id)
        lines = TransactionLineRepository(self._conn).list_for_item(self._item.id)
        if lots or lines:
            QMessageBox.warning(
                self,
                "Cannot delete",
                "This item has transaction history. Use an adjustment-out to "
                "remove remaining units instead of deleting the item.",
            )
            return
        if (
            QMessageBox.question(self, "Delete item", f"Delete {self._item.name}?")
            == QMessageBox.Yes
        ):
            self._items.delete(self._item.id)
            self._conn.commit()
            self.accept()
