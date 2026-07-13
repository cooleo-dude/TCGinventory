"""Unified Transaction screen — the write UI (F-TX-*, F-PU/SA/TR-*).

One bidirectional screen records every purchase, sale, and trade: cards/cash you
give on the left, cards/cash you receive on the right. Cards are entered
**inline** — a ＋ adds an editable row, a per-row ✕ removes it; the receive
side's Name field autocompletes from the local card catalog and auto-fills
set/number. Type is inferred live; a card's *value* is derived from the whole
deal (no per-card agreed value). Submit is blocked until valid.

Also supports **edit mode**: pre-loaded from an existing transaction, Submit
performs an atomic void + re-post via :meth:`TransactionService.edit`.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QDate, QStringListModel, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

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
    Intent,
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
from tcginventory.ui.formatting import money, percent


def money_spin() -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setPrefix("$")
    spin.setDecimals(2)
    spin.setMaximum(10_000_000.0)
    spin.setGroupSeparatorShown(True)
    return spin


def _cents(spin: QDoubleSpinBox) -> int:
    return round(spin.value() * 100)


class _GiveRow:
    """Inline row for a card you give: pick an on-hand card + quantity."""

    def __init__(self, sellable: list[tuple[Item, int]], on_change) -> None:
        self._sellable = sellable
        self.combo = QComboBox()
        for item, available in sellable:
            label = item.name
            if item.set_name:
                label += f" ({item.set_name})"
            label += f" — {available} on hand"
            self.combo.addItem(label, item.id)
        self.qty = QSpinBox()
        self.qty.setMinimum(1)
        self.combo.currentIndexChanged.connect(self._sync_max)
        self.combo.currentIndexChanged.connect(lambda *_: on_change())
        self.qty.valueChanged.connect(lambda *_: on_change())
        self._sync_max()

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

    def display_name(self) -> str:
        return self.combo.currentText().split(" — ")[0]

    def to_card(self):
        item_id = self.combo.currentData()
        if item_id is None:
            return None
        return OutgoingCard(item_id=item_id, quantity=self.qty.value())


class _ReceiveRow:
    """Inline row for a card you receive: identity + market value, entered
    directly. Name autocompletes from the catalog and fills set/number."""

    def __init__(self, conn, completer_model, on_change) -> None:
        self._conn = conn
        self._on_change = on_change

        self.name = QLineEdit()
        self.name.setPlaceholderText("Card name")
        if completer_model is not None:
            comp = QCompleter(completer_model, self.name)
            comp.setCaseSensitivity(Qt.CaseInsensitive)
            comp.setFilterMode(Qt.MatchContains)
            self.name.setCompleter(comp)
            comp.activated.connect(lambda *_: self._autofill())
        self.name.editingFinished.connect(self._autofill)
        self.name.textChanged.connect(lambda *_: on_change())

        self.set = QLineEdit()
        self.number = QLineEdit()
        self.cond = QComboBox()
        for c in Condition:
            self.cond.addItem(c.value, c.value)
        self.market = money_spin()
        self.market.valueChanged.connect(lambda *_: on_change())
        self.qty = QSpinBox()
        self.qty.setMinimum(1)
        self.qty.valueChanged.connect(lambda *_: on_change())
        self.intent = QComboBox()
        for intent in Intent:
            self.intent.addItem(intent.value.capitalize(), intent.value)
        self.intent.setCurrentIndex(self.intent.findData(Intent.RESALE.value))

    def _autofill(self, *_) -> None:
        name = self.name.text().strip()
        if not name:
            return
        printings = CardCatalogRepository(self._conn).find_by_name(name)
        if printings:
            p = printings[0]  # best-effort; multi-printing picker is a follow-up
            if not self.set.text().strip():
                self.set.setText(p.set_name or "")
            if not self.number.text().strip():
                self.number.setText(p.number or "")
        self._on_change()

    def set_card(self, card: IncomingCard) -> None:
        item = card.item
        self.name.setText(item.name)
        self.set.setText(item.set_name or "")
        self.number.setText(item.card_number or "")
        if item.condition is not None:
            idx = self.cond.findData(item.condition.value)
            if idx >= 0:
                self.cond.setCurrentIndex(idx)
        self.market.setValue(card.market_value_cents / 100)
        self.qty.setValue(card.quantity)
        idx = self.intent.findData(card.intent.value)
        if idx >= 0:
            self.intent.setCurrentIndex(idx)

    def to_card(self):
        name = self.name.text().strip()
        if not name:
            return None
        item = Item(
            name=name,
            category=Category.SINGLE,
            set_name=self.set.text().strip() or None,
            card_number=self.number.text().strip() or None,
            condition=Condition(self.cond.currentData()),
        )
        return IncomingCard(
            item=item,
            quantity=self.qty.value(),
            market_value_cents=round(self.market.value() * 100),
            intent=Intent(self.intent.currentData()),
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
        self._edit_id: int | None = None
        self._catalog_model = self._build_catalog_model()

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
        sides = QHBoxLayout()
        sides.addWidget(self._build_give_side(), 1)
        sides.addWidget(self._build_receive_side(), 3)

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

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(sides)
        # The give/receive area occupies the upper half; the lower half is
        # reserved for a future panel.
        layout.addStretch(1)
        layout.addLayout(footer)

        self._update_summary()

    def _build_catalog_model(self) -> QStringListModel | None:
        names = CardCatalogRepository(self._conn).all_names()
        return QStringListModel(names) if names else None

    # -- side builders ------------------------------------------------------

    def _build_give_side(self) -> QGroupBox:
        box = QGroupBox("You give (out)")
        self._out_table = QTableWidget(0, 3)
        self._out_table.setHorizontalHeaderLabels(["Item", "Qty", ""])
        _tidy(self._out_table, stretch_col=0)

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

        header = QHBoxLayout()
        header.addWidget(add)
        header.addStretch(1)

        layout = QVBoxLayout(box)
        layout.addLayout(header)
        layout.addWidget(self._out_table)
        layout.addLayout(cash_row)
        return box

    def _build_receive_side(self) -> QGroupBox:
        box = QGroupBox("You receive (in)")
        self._in_table = QTableWidget(0, 8)
        self._in_table.setHorizontalHeaderLabels(
            ["Name", "Set", "#", "Cond/Grade", "Market", "Qty", "Intent", ""]
        )
        _tidy(self._in_table, stretch_col=0)

        add = QPushButton("＋")
        add.setToolTip("Add a card you're receiving")
        add.setFixedWidth(32)
        add.clicked.connect(self._add_receive_row)

        self._cash_in = money_spin()
        self._cash_in.valueChanged.connect(self._update_summary)
        cash_row = QHBoxLayout()
        cash_row.addWidget(QLabel("Cash they pay:"))
        cash_row.addWidget(self._cash_in)
        cash_row.addStretch(1)

        header = QHBoxLayout()
        header.addWidget(add)
        header.addStretch(1)

        layout = QVBoxLayout(box)
        layout.addLayout(header)
        layout.addWidget(self._in_table)
        layout.addLayout(cash_row)
        return box

    # -- add / remove -------------------------------------------------------

    def _add_give_row(self) -> _GiveRow | None:
        sellable = self._inventory.sellable_items()
        if not sellable:
            QMessageBox.information(
                self, "Nothing to give", "No sellable cards on hand."
            )
            return None
        row = _GiveRow(sellable, self._update_summary)
        self._give_rows.append(row)
        self._rebuild_tables()
        return row

    def _add_receive_row(self) -> _ReceiveRow:
        row = _ReceiveRow(self._conn, self._catalog_model, self._update_summary)
        self._receive_rows.append(row)
        self._rebuild_tables()
        row.name.setFocus()
        return row

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

    def _remove_at(self, kind: str, index: int) -> None:
        rows = self._give_rows if kind == "out" else self._receive_rows
        if 0 <= index < len(rows):
            del rows[index]
        self._rebuild_tables()

    def _delete_button(self, kind: str, index: int) -> QPushButton:
        btn = QPushButton("✕")
        btn.setFixedWidth(28)
        btn.setToolTip("Remove this card")
        btn.clicked.connect(lambda _=False, k=kind, i=index: self._remove_at(k, i))
        return btn

    # -- rendering ----------------------------------------------------------

    def _rebuild_tables(self) -> None:
        self._out_table.setRowCount(len(self._give_rows))
        for r, row in enumerate(self._give_rows):
            self._out_table.setCellWidget(r, 0, row.combo)
            self._out_table.setCellWidget(r, 1, row.qty)
            self._out_table.setCellWidget(r, 2, self._delete_button("out", r))

        self._in_table.setRowCount(len(self._receive_rows))
        for r, row in enumerate(self._receive_rows):
            self._in_table.setCellWidget(r, 0, row.name)
            self._in_table.setCellWidget(r, 1, row.set)
            self._in_table.setCellWidget(r, 2, row.number)
            self._in_table.setCellWidget(r, 3, row.cond)
            self._in_table.setCellWidget(r, 4, row.market)
            self._in_table.setCellWidget(r, 5, row.qty)
            self._in_table.setCellWidget(r, 6, row.intent)
            self._in_table.setCellWidget(r, 7, self._delete_button("in", r))
        self._update_summary()

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
        self._cash_in.setValue(0.0)
        self._cash_out.setValue(0.0)
        self._counterparty.clear()
        self._reference.clear()
        self._edit_id = None
        self._edit_banner.setText("")
        self._rebuild_tables()

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
        self._rebuild_tables()
        return True


def _tidy(table: QTableWidget, stretch_col: int = 0) -> None:
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setSectionResizeMode(QHeaderView.ResizeToContents)
    header.setSectionResizeMode(stretch_col, QHeaderView.Stretch)
