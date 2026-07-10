"""Unified Transaction window — the write UI (F-TX-*, F-PU/SA/TR-*).

One bidirectional screen records every purchase, sale, and trade: cards/cash you
give on the left, cards/cash you receive on the right. The ``type`` is inferred
live. A summary footer shows consideration, net cash, and estimated margin, and
Submit is blocked until the transaction is valid (incoming market values are
required by the add dialog; oversell is blocked by the preview).

Also supports **edit mode**: pre-loaded from an existing transaction, Submit
performs an atomic void + re-post via :meth:`TransactionService.edit`.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.repositories import (
    ItemRepository,
    LotRepository,
    TransactionLineRepository,
    TransactionRepository,
)
from tcginventory.domain.enums import Direction, Intent, TransactionStatus
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
from tcginventory.ui.views._card_dialogs import IncomingCardDialog, OutgoingCardDialog


def money_spin() -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setPrefix("$")
    spin.setDecimals(2)
    spin.setMaximum(10_000_000.0)
    spin.setGroupSeparatorShown(True)
    return spin


def _cents(spin: QDoubleSpinBox) -> int:
    return round(spin.value() * 100)


class TransactionView(QWidget):
    posted = Signal()

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._service = TransactionService(conn)
        self._inventory = InventoryService(conn)
        self._reporting = ReportingService(conn)

        self._outgoing: list[OutgoingCard] = []
        self._out_names: list[str] = []
        self._incoming: list[IncomingCard] = []
        self._edit_id: int | None = None

        self._badge = QLabel("—")
        self._badge.setStyleSheet("font-size: 16px; font-weight: 700;")
        self._edit_banner = QLabel("")
        self._edit_banner.setStyleSheet("color: #b26a00; font-weight: 600;")

        # Header
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

        # Sides
        sides = QHBoxLayout()
        sides.addWidget(self._build_give_side(), 1)
        sides.addWidget(self._build_receive_side(), 1)

        # Footer
        self._summary = QLabel("")
        self._summary.setTextFormat(Qt.RichText)
        self._submit = QPushButton("Submit")
        self._submit.clicked.connect(self.submit)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear)
        footer = QHBoxLayout()
        footer.addWidget(self._summary, 1)
        footer.addWidget(clear_btn)
        footer.addWidget(self._submit)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(sides, 1)
        layout.addLayout(footer)

        self._update_summary()

    # -- side builders ------------------------------------------------------

    def _build_give_side(self) -> QGroupBox:
        box = QGroupBox("You give (out)")
        self._out_table = QTableWidget(0, 3)
        self._out_table.setHorizontalHeaderLabels(["Item", "Qty", "Agreed value"])
        _tidy(self._out_table)

        add = QPushButton("Add card to give…")
        add.clicked.connect(self._add_outgoing)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(
            lambda: self._remove(self._out_table, self._outgoing, self._out_names)
        )

        self._cash_out = money_spin()
        self._cash_out.valueChanged.connect(self._update_summary)
        cash_row = QHBoxLayout()
        cash_row.addWidget(QLabel("Cash you pay:"))
        cash_row.addWidget(self._cash_out)
        cash_row.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)

        layout = QVBoxLayout(box)
        layout.addWidget(self._out_table)
        layout.addLayout(buttons)
        layout.addLayout(cash_row)
        return box

    def _build_receive_side(self) -> QGroupBox:
        box = QGroupBox("You receive (in)")
        self._in_table = QTableWidget(0, 6)
        self._in_table.setHorizontalHeaderLabels(
            ["Name", "Set", "#", "Cond/Grade", "Market", "Intent"]
        )
        _tidy(self._in_table)

        add = QPushButton("Add card to receive…")
        add.clicked.connect(self._add_incoming)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(lambda: self._remove(self._in_table, self._incoming))

        self._cash_in = money_spin()
        self._cash_in.valueChanged.connect(self._update_summary)
        cash_row = QHBoxLayout()
        cash_row.addWidget(QLabel("Cash they pay:"))
        cash_row.addWidget(self._cash_in)
        cash_row.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)

        layout = QVBoxLayout(box)
        layout.addWidget(self._in_table)
        layout.addLayout(buttons)
        layout.addLayout(cash_row)
        return box

    # -- add / remove -------------------------------------------------------

    def _add_outgoing(self) -> None:
        sellable = self._inventory.sellable_items()
        if not sellable:
            QMessageBox.information(
                self, "Nothing to give", "No sellable cards on hand."
            )
            return
        dialog = OutgoingCardDialog(sellable, parent=self)
        if dialog.exec():
            card, name = dialog.result_card()
            self.add_outgoing(card, name)

    def _add_incoming(self) -> None:
        dialog = IncomingCardDialog(parent=self)
        if dialog.exec():
            self.add_incoming(dialog.result_card())

    def add_outgoing(self, card: OutgoingCard, display_name: str) -> None:
        self._outgoing.append(card)
        self._out_names.append(display_name)
        self._rebuild_tables()

    def add_incoming(self, card: IncomingCard) -> None:
        self._incoming.append(card)
        self._rebuild_tables()

    def _remove(self, table, cards, names=None) -> None:
        row = table.currentRow()
        if 0 <= row < len(cards):
            del cards[row]
            if names is not None:
                del names[row]
            self._rebuild_tables()

    # -- rendering ----------------------------------------------------------

    def _rebuild_tables(self) -> None:
        self._out_table.setRowCount(len(self._outgoing))
        for r, (card, name) in enumerate(
            zip(self._outgoing, self._out_names, strict=True)
        ):
            self._out_table.setItem(r, 0, _cell(name))
            self._out_table.setItem(r, 1, _cell(str(card.quantity), numeric=True))
            self._out_table.setItem(
                r, 2, _cell(money(card.agreed_value_cents), numeric=True)
            )

        self._in_table.setRowCount(len(self._incoming))
        for r, card in enumerate(self._incoming):
            item = card.item
            self._in_table.setItem(r, 0, _cell(item.name))
            self._in_table.setItem(r, 1, _cell(item.set_name or ""))
            self._in_table.setItem(r, 2, _cell(item.card_number or ""))
            self._in_table.setItem(r, 3, _cell(_cond_grade(item)))
            self._in_table.setItem(
                r, 4, _cell(money(card.market_value_cents), numeric=True)
            )
            self._in_table.setItem(r, 5, _cell(card.intent.value))
        self._update_summary()

    def build_request(self) -> TransactionRequest:
        return TransactionRequest(
            occurred_at=self._date.text().strip(),
            outgoing=list(self._outgoing),
            incoming=list(self._incoming),
            cash_in_cents=_cents(self._cash_in),
            cash_out_cents=_cents(self._cash_out),
            counterparty=self._counterparty.text().strip() or None,
            reference=self._reference.text().strip() or None,
        )

    def _update_summary(self) -> None:
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

        can_submit = preview.inferred_type is not None and preview.feasible
        self._submit.setEnabled(can_submit)

    # -- submit / edit ------------------------------------------------------

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
        self._outgoing.clear()
        self._out_names.clear()
        self._incoming.clear()
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
        # Net cash → single-sided (preserves net consideration & cash delta).
        if txn.cash_delta_cents >= 0:
            self._cash_in.setValue(txn.cash_delta_cents / 100)
        else:
            self._cash_out.setValue(-txn.cash_delta_cents / 100)

        items = ItemRepository(self._conn)
        lots = LotRepository(self._conn)
        for line in TransactionLineRepository(self._conn).list_for_transaction(txn.id):
            item = items.get(line.item_id)
            if line.direction is Direction.OUT:
                name = item.name if item else f"item {line.item_id}"
                self._outgoing.append(
                    OutgoingCard(
                        item_id=line.item_id,
                        quantity=line.quantity,
                        agreed_value_cents=line.unit_price_cents,
                    )
                )
                self._out_names.append(name)
            else:
                line_lots = lots.list_for_acquired_line(line.id)
                mv = line_lots[0].acquired_market_value_cents if line_lots else 0
                intent = line_lots[0].intent if line_lots else Intent.RESALE
                self._incoming.append(
                    IncomingCard(
                        item=item,
                        quantity=line.quantity,
                        market_value_cents=mv,
                        intent=intent,
                    )
                )
        self._rebuild_tables()
        return True


def _cond_grade(item) -> str:
    if item.is_graded:
        company = item.grading_company.value if item.grading_company else "?"
        return f"{company} {item.grade or ''}".strip()
    return item.condition.value if item.condition else ""


def _cell(text: str, numeric: bool = False) -> QTableWidgetItem:
    cell = QTableWidgetItem(text)
    cell.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    if numeric:
        cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return cell


def _tidy(table: QTableWidget) -> None:
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.horizontalHeader().setStretchLastSection(True)
