"""Unified Transaction screen — the write UI (F-TX-*, F-PU/SA/TR-*).

One bidirectional screen records every purchase, sale, and trade: cards/cash you
give on the left, cards/cash you receive on the right. Cards are entered
**inline** — a ＋ adds an editable row, a per-row ✕ removes it; the receive
side's Name field autocompletes from the local card catalog and auto-fills
set/number. Columns are drag-resizable. Type is inferred live; a card's *value*
is derived from the whole deal (no per-card agreed value). Submit is blocked
until valid.

Also supports **edit mode**: pre-loaded from an existing transaction, Submit
performs an atomic void + re-post via :meth:`TransactionService.edit`.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QDate, QModelIndex, Qt, QThread, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStyledItemDelegate,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from tcginventory import config
from tcginventory.data.repositories import (
    CardCatalogRepository,
    ItemRepository,
    LotRepository,
    TransactionLineRepository,
    TransactionRepository,
)
from tcginventory.domain.enums import (
    Category,
    Condition,
    Direction,
    Edition,
    GradingCompany,
    HoloPattern,
    Intent,
    Language,
    TransactionStatus,
)
from tcginventory.domain.models import Item
from tcginventory.services.exceptions import ServiceError
from tcginventory.services.inventory import InventoryService
from tcginventory.services.reporting import ReportingService
from tcginventory.services.transactions import (
    IncomingCard,
    OutgoingCard,
    TransactionRequest,
    TransactionService,
)
from tcginventory.ui.card_preview import CardImagePane, install_hover_preview
from tcginventory.ui.formatting import money, percent

_GIVE_HEADERS = ["Item", "Qty", ""]
_GIVE_WIDTHS = [280, 55, 30]
_RECV_HEADERS = [
    "Name", "Category", "Set", "#", "Language", "Graded", "Condition",
    "Holo", "Edition", "Grading Co.", "Grade", "Cert #", "Market", "Qty",
    "Intent", "Review", "TCGplayer", "eBay", "Notes", "",
]
_RECV_WIDTHS = [
    150, 85, 100, 55, 65, 55, 80, 55, 90, 90, 55, 85, 85, 45, 85, 55, 120,
    100, 130, 30,
]


def money_spin() -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setPrefix("$")
    spin.setDecimals(2)
    spin.setMaximum(10_000_000.0)
    spin.setGroupSeparatorShown(True)
    return spin


def _cents(spin: QDoubleSpinBox) -> int:
    return round(spin.value() * 100)


def _combo(values, *, capitalize: bool = False) -> QComboBox:
    combo = QComboBox()
    for v in values:
        combo.addItem(v.value.capitalize() if capitalize else v.value, v.value)
    return combo


def _select(combo: QComboBox, value: str) -> None:
    pos = combo.findData(value)
    if pos >= 0:
        combo.setCurrentIndex(pos)


class _PriceLookupWorker(QThread):
    """Fetch a picked card's current tcgcsv market price off the UI thread."""

    done = Signal(object)  # (price_cents, tcgplayer_url) or None

    def __init__(self, set_name: str, number: str, name: str) -> None:
        super().__init__()
        self._set = set_name
        self._number = number
        self._name = name

    def run(self) -> None:
        from tcginventory.services.pricing import TcgcsvProvider

        try:
            result = TcgcsvProvider().price_for_card(
                self._set, self._number, self._name
            )
        except Exception:  # network/parse failure → just skip the auto-price
            result = None
        self.done.emit(result)


class _GiveRow:
    """Inline row for a card you give: pick an on-hand card + quantity."""

    def __init__(
        self, conn, sellable: list[tuple[Item, int]], on_change, sink=None
    ) -> None:
        self._conn = conn
        self._sellable = sellable
        self._sink = sink
        self.combo = QComboBox()
        for item, available in sellable:
            label = item.name
            if item.set_name:
                label += f" ({item.set_name})"
            label += f" — {available} on hand"
            self.combo.addItem(label, item.id)
        # Type-to-search over our resale cards (the picker only lists resale
        # lots); the arrow still opens the full dropdown.
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.NoInsert)
        self.combo.lineEdit().setPlaceholderText("Type to search your resale cards…")
        completer = self.combo.completer()
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        install_hover_preview(self.combo, self._hover_card, sink)
        # Hovering an item in the open dropdown previews it in the pane too.
        self.combo.view().setMouseTracking(True)
        self.combo.view().entered.connect(self._on_dropdown_hover)
        self.qty = QSpinBox()
        self.qty.setMinimum(1)
        self.qty.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.combo.currentIndexChanged.connect(self._sync_max)
        self.combo.currentIndexChanged.connect(lambda *_: on_change())
        self.qty.valueChanged.connect(lambda *_: on_change())
        self._sync_max()

    def widgets(self) -> list[QWidget]:
        return [self.combo, self.qty]

    def _hover_card(self) -> tuple[str, str] | None:
        return self._image_for_index(self.combo.currentIndex())

    def _image_for_index(self, i: int) -> tuple[str, str] | None:
        if not (0 <= i < len(self._sellable)):
            return None
        item = self._sellable[i][0]
        return CardCatalogRepository(self._conn).image_for(
            item.name, item.set_name, item.card_number
        )

    def _on_dropdown_hover(self, index: QModelIndex) -> None:
        if self._sink is not None:
            res = self._image_for_index(index.row())
            self._sink.show_card(*(res if res else (None, None)))

    def _sync_max(self, *_) -> None:
        i = self.combo.currentIndex()
        if 0 <= i < len(self._sellable):
            self.qty.setMaximum(self._sellable[i][1])

    def set_item(self, item_id: int, quantity: int) -> None:
        idx = self.combo.findData(item_id)
        if idx >= 0:
            self.combo.setCurrentIndex(idx)
        self._sync_max()
        self.qty.setValue(quantity)

    def to_card(self):
        item_id = self.combo.currentData()
        if item_id is None:
            return None
        return OutgoingCard(item_id=item_id, quantity=self.qty.value())


class _MatchDelegate(QStyledItemDelegate):
    """Render autofill popup rows with the full "name · set #number" label.

    QCompleter shows *and* inserts its completion role, so the completion text
    is kept as the clean card name (what lands in the Name field) while this
    delegate paints the richer label from a separate role in the popup only.
    """

    def __init__(self, label_role: int, parent=None) -> None:
        super().__init__(parent)
        self._label_role = label_role

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        label = index.data(self._label_role)
        if label:
            option.text = label


class _ReceiveRow:
    """Inline row for a card you receive: the full card field set, entered
    directly. Name autocompletes from the catalog and fills set/number."""

    def __init__(self, conn, is_auto, on_change, on_pick=None, sink=None) -> None:
        self._conn = conn
        self._is_auto = is_auto  # () -> bool : is "Auto Detect Card" on?
        self._on_change = on_change
        self._on_pick = on_pick  # (row, CatalogCard) -> None : after a pick
        self._sink = sink  # preview target (Transaction's lower-left pane)
        self._matches: dict[str, object] = {}
        self._card_id: str | None = None  # set when a catalog card is applied
        self._image_url: str | None = None

        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Pikachu Celebrations 005/025")
        # Whole-string catalog search: type name (+set +number), pick a match.
        self._search_model = QStandardItemModel(self.name)
        self._completer = QCompleter(self._search_model, self.name)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.name.setCompleter(self._completer)
        self.name.textEdited.connect(self._on_name_edited)
        self._completer.activated[QModelIndex].connect(self._pick_match)
        self.name.textChanged.connect(lambda *_: on_change())
        # Hover the Name field to preview the card image; hover a match in the
        # autofill popup to preview it before picking. Previews land in the
        # Transaction window's lower-left pane (the sink).
        install_hover_preview(self.name, self._hover_card, self._sink)
        popup = self._completer.popup()
        popup.setMouseTracking(True)
        popup.entered.connect(self._on_match_hover)
        self._match_delegate = _MatchDelegate(self._LABEL_ROLE, popup)
        popup.setItemDelegate(self._match_delegate)
        install_hover_preview(popup, lambda: None, self._sink)  # clear on leave

        self.category = _combo(Category, capitalize=True)
        self.set = QLineEdit()
        self.number = QLineEdit()
        self.language = _combo(Language)
        self.graded = QCheckBox()
        self.graded.toggled.connect(self._toggle_graded)
        self.graded.toggled.connect(lambda *_: on_change())
        self.condition = _combo(Condition)
        # Auto-mark for review when a non-NM condition is chosen.
        self.condition.currentIndexChanged.connect(self._maybe_flag_review)
        self.holo = _combo(HoloPattern)  # default NH
        self.edition = _combo(Edition)  # default N/A
        self.company = _combo(GradingCompany)
        self.grade = QLineEdit()
        self.cert = QLineEdit()
        self.market = money_spin()
        self.market.setButtonSymbols(QAbstractSpinBox.NoButtons)  # no up/down arrows
        self.market.valueChanged.connect(lambda *_: on_change())
        self.qty = QSpinBox()
        self.qty.setMinimum(1)
        self.qty.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.qty.valueChanged.connect(lambda *_: on_change())
        self.intent = _combo(Intent, capitalize=True)
        _select(self.intent, Intent.RESALE.value)
        self.review = QCheckBox()
        self.tcgplayer = QLineEdit()
        self.ebay = QLineEdit()
        self.notes = QLineEdit()
        self._toggle_graded(False)

    def widgets(self) -> list[QWidget]:
        return [
            self.name, self.category, self.set, self.number, self.language,
            self.graded, self.condition, self.holo, self.edition, self.company,
            self.grade, self.cert, self.market, self.qty, self.intent,
            self.review, self.tcgplayer, self.ebay, self.notes,
        ]

    def _maybe_flag_review(self) -> None:
        """Sync review to condition (ungraded): a worse-than-NM condition marks
        the card for review; switching back to NM clears it."""
        if not self.graded.isChecked():
            self.review.setChecked(
                self.condition.currentData() != Condition.NM.value
            )

    def _toggle_graded(self, graded: bool) -> None:
        # Condition greys out when graded; grade fields enable (Spec 02).
        self.condition.setEnabled(not graded)
        self.company.setEnabled(graded)
        self.grade.setEnabled(graded)
        self.cert.setEnabled(graded)

    _CARD_ID_ROLE = Qt.UserRole + 1
    _LABEL_ROLE = Qt.UserRole + 2

    def _on_name_edited(self, text: str) -> None:
        """Query the catalog as the user types (when Auto Detect is on) and show
        the matches in the popup."""
        if not self._is_auto() or len(text.strip()) < 2:
            return
        matches = CardCatalogRepository(self._conn).search(text, limit=25)
        self._matches = {c.card_id: c for c in matches}
        self._search_model.clear()
        for card in matches:
            # Completion text is the clean name (what QCompleter inserts into the
            # Name field); the popup shows the fuller label via _MatchDelegate.
            item = QStandardItem(card.name)
            label = (
                f"{card.name}  ·  {card.set_name or '?'}  #{card.full_number or '?'}"
            )
            item.setData(card.card_id, self._CARD_ID_ROLE)
            item.setData(label, self._LABEL_ROLE)
            self._search_model.appendRow(item)
        if matches:
            self._completer.complete()

    def _on_match_hover(self, index: QModelIndex) -> None:
        """Preview a card image as the pointer moves over autofill matches."""
        card = self._matches.get(index.data(self._CARD_ID_ROLE))
        if card is not None and self._sink is not None:
            self._sink.show_card(card.card_id, getattr(card, "image_small", None))

    def _hover_card(self) -> tuple[str, str] | None:
        if self._card_id and self._image_url:
            return self._card_id, self._image_url
        name = self.name.text().strip()
        if not name:
            return None
        return CardCatalogRepository(self._conn).image_for(
            name, self.set.text().strip(), self.number.text().strip()
        )

    def _pick_match(self, index: QModelIndex) -> None:
        card = self._matches.get(index.data(self._CARD_ID_ROLE))
        if card is not None:
            self.apply_catalog_card(card)
            if self._on_pick is not None:
                self._on_pick(self, card)

    def apply_catalog_card(self, card) -> None:
        """Populate as many fields as possible from a chosen catalog card."""
        self._card_id = card.card_id
        self._image_url = getattr(card, "image_small", None)
        self.name.setText(card.name)
        self.set.setText(card.set_name or "")
        self.number.setText(card.full_number or card.number or "")
        _select(self.category, Category.SINGLE.value)  # catalog cards are singles
        _select(self.language, Language.EN.value)  # dataset is English
        # Keep the chosen card showing in the pane after the popup closes.
        if self._sink is not None and self._image_url:
            self._sink.show_card(self._card_id, self._image_url)
        self._on_change()

    def set_card(self, card: IncomingCard) -> None:
        item = card.item
        self.name.setText(item.name)
        _select(self.category, item.category.value)
        self.set.setText(item.set_name or "")
        self.number.setText(item.card_number or "")
        _select(self.language, item.language.value)
        self.graded.setChecked(item.is_graded)
        if item.condition is not None:
            _select(self.condition, item.condition.value)
        if item.holo_pattern is not None:
            _select(self.holo, item.holo_pattern.value)
        if item.edition is not None:
            _select(self.edition, item.edition.value)
        if item.grading_company is not None:
            _select(self.company, item.grading_company.value)
        self.grade.setText(item.grade or "")
        self.cert.setText(item.cert_number or "")
        self.market.setValue(card.market_value_cents / 100)
        self.qty.setValue(card.quantity)
        _select(self.intent, card.intent.value)
        self.review.setChecked(getattr(card, "review", False))
        self.tcgplayer.setText(item.tcgplayer_url or "")
        self.ebay.setText(item.ebay_url or "")
        self.notes.setText(item.notes or "")

    def to_card(self):
        name = self.name.text().strip()
        if not name:
            return None
        graded = self.graded.isChecked()
        item = Item(
            name=name,
            category=Category(self.category.currentData()),
            set_name=self.set.text().strip() or None,
            card_number=self.number.text().strip() or None,
            language=Language(self.language.currentData()),
            condition=None if graded else Condition(self.condition.currentData()),
            holo_pattern=HoloPattern(self.holo.currentData()),
            edition=Edition(self.edition.currentData()),
            is_graded=graded,
            grading_company=(
                GradingCompany(self.company.currentData()) if graded else None
            ),
            grade=(self.grade.text().strip() or None) if graded else None,
            cert_number=(self.cert.text().strip() or None) if graded else None,
            tcgplayer_url=self.tcgplayer.text().strip() or None,
            ebay_url=self.ebay.text().strip() or None,
            notes=self.notes.text().strip() or None,
        )
        return IncomingCard(
            item=item,
            quantity=self.qty.value(),
            market_value_cents=round(self.market.value() * 100),
            intent=Intent(self.intent.currentData()),
            review=self.review.isChecked(),
        )


class TransactionView(QWidget):
    posted = Signal()

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._service = TransactionService(conn)
        self._inventory = InventoryService(conn)
        self._reporting = ReportingService(conn)

        self._give_rows: list[_GiveRow] = []
        self._receive_rows: list[_ReceiveRow] = []
        self._price_workers: list[_PriceLookupWorker] = []
        self._edit_id: int | None = None
        # Hovering any card in this window shows its image here, not at the
        # cursor (parked in the reserved lower-left region).
        self._preview_pane = CardImagePane()

        self._badge = QLabel("—")
        self._badge.setStyleSheet("font-size: 16px; font-weight: 700;")
        self._badge.setToolTip("Type is inferred from which sides carry cards")
        self._badge.setAccessibleName("Inferred transaction type")
        self._edit_banner = QLabel("")
        self._edit_banner.setStyleSheet("color: #b26a00; font-weight: 600;")

        self._date = QLineEdit(QDate.currentDate().toString("yyyy-MM-dd"))
        self._counterparty = QLineEdit()
        self._reference = QLineEdit()
        header = QFormLayout()
        header.addRow("Date (YYYY-MM-DD):", self._date)
        header.addRow("Counterparty:", self._counterparty)
        header.addRow("Reference:", self._reference)

        top = QHBoxLayout()
        top.addLayout(header, 1)
        badge_box = QVBoxLayout()
        badge_box.addWidget(QLabel("This will record:"))
        badge_box.addWidget(self._badge)
        badge_box.addWidget(self._edit_banner)
        top.addLayout(badge_box)

        # Give is narrow (2 fields) vs the wider receive side.
        sides_widget = QWidget()
        sides = QHBoxLayout(sides_widget)
        sides.setContentsMargins(0, 0, 0, 0)
        sides.addWidget(self._build_give_side(), 1)
        sides.addWidget(self._build_receive_side(), 4)

        self._summary = QLabel("")
        self._summary.setTextFormat(Qt.RichText)
        self._submit = QPushButton("Submit")
        self._submit.setToolTip("Post this transaction (Ctrl+S)")
        self._submit.clicked.connect(self.submit)
        self._save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self._save_shortcut.activated.connect(self._submit_via_shortcut)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear)
        footer = QHBoxLayout()
        footer.addWidget(self._summary, 1)
        footer.addWidget(clear_btn)
        footer.addWidget(self._submit)

        # Lower area: card-image preview parked in the left corner; the rest is
        # reserved for later.
        lower_widget = QWidget()
        lower = QHBoxLayout(lower_widget)
        lower.setContentsMargins(0, 0, 0, 0)
        preview_box = QVBoxLayout()
        preview_box.addWidget(QLabel("Card preview"))
        preview_box.addWidget(self._preview_pane)
        preview_box.addStretch(1)
        # Preview column mirrors the give:receive 1:4 split, so the card image
        # is as wide as the "You give" panel (and shrinks with it).
        lower.addLayout(preview_box, 1)
        lower.addStretch(4)

        # Drag the divider to grow the give/receive tables vertically (or the
        # preview area).
        body = QSplitter(Qt.Vertical)
        body.addWidget(sides_widget)
        body.addWidget(lower_widget)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 1)
        body.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(body, 1)
        layout.addLayout(footer)

        self._update_summary()

    # -- side builders ------------------------------------------------------

    def _build_give_side(self) -> QGroupBox:
        box = QGroupBox("You give (out)")
        self._out_table = QTableWidget(0, len(_GIVE_HEADERS))
        self._out_table.setHorizontalHeaderLabels(_GIVE_HEADERS)
        _tidy(self._out_table, _GIVE_WIDTHS)

        add = QPushButton("＋")
        add.setToolTip("Add a card you're giving")
        add.setFixedWidth(32)
        add.clicked.connect(self._add_give_row)

        self._cash_out = money_spin()
        self._cash_out.valueChanged.connect(self._update_summary)
        cash_row = QHBoxLayout()
        cash_row.addWidget(QLabel("Cash you pay:"))
        cash_row.addWidget(self._cash_out)
        cash_row.addStretch(1)

        head = QHBoxLayout()
        head.addWidget(add)
        head.addStretch(1)

        layout = QVBoxLayout(box)
        layout.addLayout(head)
        layout.addWidget(self._out_table)
        layout.addLayout(cash_row)
        return box

    def _build_receive_side(self) -> QGroupBox:
        box = QGroupBox("You receive (in)")
        self._in_table = QTableWidget(0, len(_RECV_HEADERS))
        self._in_table.setHorizontalHeaderLabels(_RECV_HEADERS)
        _tidy(self._in_table, _RECV_WIDTHS)

        add = QPushButton("＋")
        add.setToolTip("Add a card you're receiving")
        add.setFixedWidth(32)
        add.clicked.connect(self._add_receive_row)

        self._auto_detect_cb = QCheckBox("Auto Detect Card")
        self._auto_detect_cb.setToolTip(
            "As you type a card (name, set, and/or number), search the local "
            "card database and pick a match to auto-fill its fields."
        )
        self._auto_detect_cb.setChecked(
            bool(config.get_pref("receive_auto_detect", True))
        )
        self._auto_detect_cb.toggled.connect(self._on_auto_detect_toggled)
        # Hint when Auto Detect is on but the catalog hasn't been pulled yet.
        self._catalog_hint = QLabel("")
        self._catalog_hint.setStyleSheet("color: #b26a00;")

        self._cash_in = money_spin()
        self._cash_in.valueChanged.connect(self._update_summary)
        cash_row = QHBoxLayout()
        cash_row.addWidget(QLabel("Cash they pay:"))
        cash_row.addWidget(self._cash_in)
        cash_row.addStretch(1)

        head = QHBoxLayout()
        head.addWidget(add)
        head.addWidget(self._catalog_hint)
        head.addStretch(1)
        head.addWidget(self._auto_detect_cb)  # top-right of the receive panel

        layout = QVBoxLayout(box)
        layout.addLayout(head)
        layout.addWidget(self._in_table)
        layout.addLayout(cash_row)
        self._update_catalog_hint()
        return box

    def _on_auto_detect_toggled(self, on: bool) -> None:
        config.set_pref("receive_auto_detect", on)
        self._update_catalog_hint()

    def _update_catalog_hint(self) -> None:
        empty = CardCatalogRepository(self._conn).count() == 0
        if self._auto_detect_cb.isChecked() and empty:
            self._catalog_hint.setText(
                "Card database is empty — run File ▸ Update card database"
            )
        else:
            self._catalog_hint.setText("")

    # -- add / remove (insert/remove rows — never a full-table rebuild, which
    #    would destroy surviving rows' cell widgets on shrink) --------------

    def _place_row(self, table, index, row, kind) -> None:
        for col, widget in enumerate(row.widgets()):
            table.setCellWidget(index, col, widget)
        table.setCellWidget(
            index, table.columnCount() - 1, self._delete_button(kind, row)
        )

    def _delete_button(self, kind: str, row) -> QPushButton:
        btn = QPushButton("✕")
        btn.setFixedWidth(28)
        btn.setToolTip("Remove this card")
        btn.clicked.connect(lambda _=False, r=row, k=kind: self._remove_row(k, r))
        return btn

    def _remove_row(self, kind: str, row) -> None:
        rows = self._give_rows if kind == "out" else self._receive_rows
        table = self._out_table if kind == "out" else self._in_table
        if row in rows:
            index = rows.index(row)
            rows.remove(row)
            table.removeRow(index)  # keeps other rows' widgets intact
        self._update_summary()

    def _add_give_row(self) -> _GiveRow | None:
        sellable = self._inventory.sellable_items()
        if not sellable:
            QMessageBox.information(
                self, "Nothing to give", "No sellable cards on hand."
            )
            return None
        row = _GiveRow(
            self._conn, sellable, self._update_summary, sink=self._preview_pane
        )
        self._give_rows.append(row)
        index = self._out_table.rowCount()
        self._out_table.insertRow(index)
        self._place_row(self._out_table, index, row, "out")
        self._update_summary()
        return row

    def _add_receive_row(self) -> _ReceiveRow:
        row = _ReceiveRow(
            self._conn,
            lambda: self._auto_detect_cb.isChecked(),
            self._update_summary,
            on_pick=self._request_price,
            sink=self._preview_pane,
        )
        self._receive_rows.append(row)
        index = self._in_table.rowCount()
        self._in_table.insertRow(index)
        self._place_row(self._in_table, index, row, "in")
        row.name.setFocus()
        self._update_summary()
        return row

    def _request_price(self, row, card) -> None:
        """After a catalog pick, fetch the card's current tcgcsv market price
        (off-thread) and fill Market — the same source the inventory uses."""
        if not card.set_name or not card.number:
            return
        worker = _PriceLookupWorker(card.set_name, card.number, card.name)
        worker.done.connect(
            lambda result, r=row, w=worker: self._on_price_ready(r, w, result)
        )
        self._price_workers.append(worker)
        worker.start()

    def _on_price_ready(self, row, worker, result) -> None:
        if worker in self._price_workers:
            self._price_workers.remove(worker)
        if result is None or row not in self._receive_rows:
            return
        cents, url = result
        row.market.setValue(cents / 100)
        if url and not row.tcgplayer.text().strip():
            row.tcgplayer.setText(url)
        self._update_summary()

    # Test/programmatic API (used by the lifecycle tests).
    def add_outgoing(self, card: OutgoingCard, display_name: str = "") -> None:
        row = self._add_give_row()
        if row is not None:
            row.set_item(card.item_id, card.quantity)
            self._update_summary()

    def add_incoming(self, card: IncomingCard) -> None:
        row = self._add_receive_row()
        row.set_card(card)
        self._update_summary()

    # -- request / summary --------------------------------------------------

    def build_request(self) -> TransactionRequest:
        outgoing = [c for c in (r.to_card() for r in self._give_rows) if c]
        incoming = [c for c in (r.to_card() for r in self._receive_rows) if c]
        return TransactionRequest(
            occurred_at=self._date.text().strip(),
            outgoing=outgoing,
            incoming=incoming,
            cash_in_cents=_cents(self._cash_in),
            cash_out_cents=_cents(self._cash_out),
            counterparty=self._counterparty.text().strip() or None,
            reference=self._reference.text().strip() or None,
        )

    def _update_summary(self, *_args) -> None:
        preview = self._service.preview(self.build_request())
        self._badge.setText(
            preview.inferred_type.value.upper() if preview.inferred_type else "—"
        )
        margin_txt = (
            f"{money(preview.realized_pl_cents)}" if preview.proceeds_cents else "—"
        )
        pct = (
            percent(100.0 * preview.realized_pl_cents / preview.proceeds_cents)
            if preview.proceeds_cents
            else ""
        )
        parts = [
            f"Consideration given: <b>{money(preview.consideration_given_cents)}</b>",
            f"Value received: <b>{money(preview.value_received_cents)}</b>",
            f"Net cash: <b>{money(preview.net_cash_cents)}</b>",
            f"Est. margin: <b>{margin_txt}</b> {pct}",
        ]
        if not preview.feasible:
            parts.append(
                "<span style='color:#c62828'>⚠ oversell — reduce quantity</span>"
            )
        self._summary.setText(" &nbsp;·&nbsp; ".join(parts))
        self._submit.setEnabled(
            preview.inferred_type is not None and preview.feasible
        )

    # -- submit / edit ------------------------------------------------------

    def _submit_via_shortcut(self) -> None:
        if self._submit.isEnabled():
            self.submit()

    def submit(self) -> None:
        request = self.build_request()
        try:
            if self._edit_id is not None:
                old_margin = self._reporting.transaction_margin_cents(self._edit_id)
                result = self._service.edit(self._edit_id, request)
                self._show_success(
                    result, edited_from=self._edit_id, old_margin=old_margin
                )
            else:
                result = self._service.post(request)
                self._show_success(result)
        except (ServiceError, ValueError) as exc:
            QMessageBox.warning(self, "Could not post", str(exc))
            return
        self.clear()
        self.posted.emit()

    def _show_success(self, result, edited_from=None, old_margin=None) -> None:
        lines = [f"Recorded {result.transaction.type.value.upper()}."]
        if edited_from is not None:
            lines.append(f"(edit of transaction {edited_from} — original voided)")
        if result.lots:
            lines.append("\nInferred cost of each card received:")
            for lot in result.lots:
                item = self._service.items.get(lot.item_id)
                name = item.name if item else f"item {lot.item_id}"
                lines.append(
                    f"  • {name}: {money(lot.unit_cost_cents)} × {lot.quantity}"
                )
        if result.realized_pl_cents:
            lines.append(
                f"\nMargin on cards given up: {money(result.realized_pl_cents)}"
            )
        if old_margin is not None and old_margin != result.realized_pl_cents:
            lines.append(
                f"\n⚠ Realized P/L changed: {money(old_margin)} → "
                f"{money(result.realized_pl_cents)}"
            )
        QMessageBox.information(self, "Success", "\n".join(lines))

    def clear(self) -> None:
        self._give_rows.clear()
        self._receive_rows.clear()
        self._out_table.setRowCount(0)
        self._in_table.setRowCount(0)
        self._cash_in.setValue(0.0)
        self._cash_out.setValue(0.0)
        self._counterparty.clear()
        self._reference.clear()
        self._edit_id = None
        self._edit_banner.setText("")
        self._update_summary()

    def refresh(self) -> None:
        self._update_summary()

    # -- edit pre-load ------------------------------------------------------

    def load_transaction(self, transaction_id: int) -> bool:
        """Pre-fill the form from an existing transaction for editing."""
        txns = TransactionRepository(self._conn)
        txn = txns.get(transaction_id)
        if txn is None or txn.status is TransactionStatus.VOIDED:
            QMessageBox.warning(
                self, "Cannot edit", "Transaction is missing or voided."
            )
            return False

        self.clear()
        self._edit_id = transaction_id
        self._edit_banner.setText(f"Editing transaction {transaction_id}")
        self._date.setText(txn.occurred_at[:10])
        self._counterparty.setText(txn.counterparty or "")
        self._reference.setText(txn.reference or "")
        if txn.cash_delta_cents >= 0:
            self._cash_in.setValue(txn.cash_delta_cents / 100)
        else:
            self._cash_out.setValue(-txn.cash_delta_cents / 100)

        items = ItemRepository(self._conn)
        lots = LotRepository(self._conn)
        for line in TransactionLineRepository(self._conn).list_for_transaction(txn.id):
            item = items.get(line.item_id)
            if line.direction is Direction.OUT:
                self.add_outgoing(
                    OutgoingCard(item_id=line.item_id, quantity=line.quantity)
                )
            elif item is not None:
                line_lots = lots.list_for_acquired_line(line.id)
                mv = line_lots[0].acquired_market_value_cents if line_lots else 0
                intent = line_lots[0].intent if line_lots else Intent.RESALE
                self.add_incoming(
                    IncomingCard(
                        item=item,
                        quantity=line.quantity,
                        market_value_cents=mv,
                        intent=intent,
                    )
                )
        return True


def _tidy(table: QTableWidget, widths: list[int]) -> None:
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setSectionResizeMode(QHeaderView.Interactive)  # drag to resize
    header.setMinimumSectionSize(22)
    for col, width in enumerate(widths):
        table.setColumnWidth(col, width)
