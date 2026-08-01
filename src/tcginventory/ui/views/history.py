"""History view — full transaction log with detail drill-down (F-VW-3).

Styled to the Ember Duo theme: the log renders type/status as pills and money as
mono, sign-tinted; selecting a row opens a summary + a per-card table below.
Searchable by **card** or counterparty. Void/Edit act on the selected row.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import (
    QModelIndex,
    QRectF,
    QSize,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.repositories import (
    ItemRepository,
    LotConsumptionRepository,
    LotRepository,
    TransactionLineRepository,
    TransactionRepository,
)
from tcginventory.domain.enums import Direction, TransactionStatus
from tcginventory.domain.models import Item
from tcginventory.services.exceptions import ServiceError
from tcginventory.services.reporting import ReportingService
from tcginventory.services.transactions import TransactionService
from tcginventory.ui import icons, theme
from tcginventory.ui.formatting import money, percent
from tcginventory.ui.models.history_model import HistoryRow, HistoryTableModel

_TYPES = ["All", "purchase", "sale", "trade", "adjustment"]
_TYPE_COL, _STATUS_COL = 1, 5

# Bottom-pane columns describing each card in the selected transaction.
_DETAIL_HEADERS = [
    "SKU",
    "Name",
    "Set",
    "#",
    "Purchase Price",
    "Notes",
    "Cond/Grade",
    "Language",
]


def _cond_grade(item: Item) -> str:
    if item.is_graded:
        company = item.grading_company.value if item.grading_company else "?"
        return f"{company} {item.grade or ''}".strip()
    return item.condition.value if item.condition else ""


def _alpha(hex_color: str, a: float) -> QColor:
    c = QColor(hex_color)
    c.setAlphaF(a)
    return c


def _type_color(value: str) -> str:
    t = theme.active()
    return {
        "purchase": t.info,
        "sale": t.pos,
        "trade": t.accent_ink,
        "adjustment": t.ink3,
    }.get(value, t.ink3)


class _PillDelegate(QStyledItemDelegate):
    """Paints the Type and Status columns as rounded, colour-coded pills."""

    def sizeHint(self, option, index) -> QSize:
        if index.column() not in (_TYPE_COL, _STATUS_COL):
            return super().sizeHint(option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        label = (opt.text or "").capitalize()
        # left margin 8 + pad 9·2 + dot 6 + gap 6 + text + right margin 8
        width = 46 + opt.fontMetrics.horizontalAdvance(label)
        return QSize(width, opt.fontMetrics.height() + 10)

    def paint(self, painter, option, index) -> None:
        col = index.column()
        if col not in (_TYPE_COL, _STATUS_COL):
            super().paint(painter, option, index)
            return

        # Draw the themed cell background (selection / zebra) with no text via the
        # style directly — calling super().paint() would re-run initStyleOption and
        # restore the label under our pill — then overlay the pill.
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)
        if not text:
            return

        t = theme.active()
        voided = index.siblingAtColumn(_STATUS_COL).data() == "voided"
        if col == _TYPE_COL:
            fg = t.ink3 if voided else _type_color(text)
            fill = _alpha(t.surface2 if voided else fg, 1.0 if voided else 0.15)
            border = None
            dot = fg
        else:  # status — a quieter ghost pill
            fg = t.ink3 if text == "voided" else t.ink2
            fill = QColor(t.surface)
            border = QColor(t.line if text == "voided" else t.line2)
            dot = QColor(t.neg if text == "voided" else t.pos)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        fm = opt.fontMetrics
        label = text.capitalize()
        dot_d, pad_x, gap = 6, 9, 6
        text_w = fm.horizontalAdvance(label)
        pill_h = fm.height() + 4
        pill_w = pad_x * 2 + dot_d + gap + text_w
        x = option.rect.left() + 8
        y = option.rect.center().y() - pill_h / 2
        rect = QRectF(x, y, pill_w, pill_h)
        path = QPainterPath()
        path.addRoundedRect(rect, pill_h / 2, pill_h / 2)
        painter.fillPath(path, QColor(fill))
        if border is not None:
            painter.setPen(QPen(border, 1))
            painter.drawPath(path)
        cy = rect.center().y()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(dot))
        painter.drawEllipse(QRectF(x + pad_x, cy - dot_d / 2, dot_d, dot_d))
        painter.setPen(QColor(fg))
        painter.setFont(opt.font)
        painter.drawText(
            QRectF(x + pad_x + dot_d + gap, y, text_w + 2, pill_h),
            int(Qt.AlignVCenter | Qt.AlignLeft),
            label,
        )
        painter.restore()


class _HistoryFilterProxy(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.setSortRole(Qt.UserRole)
        self._type = "All"
        self._text = ""

    def set_type(self, value: str) -> None:
        self._type = value
        self.invalidateFilter()

    def set_text(self, text: str) -> None:
        self._text = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        row = self.sourceModel().row_at(source_row)
        txn = row.transaction
        if self._type != "All" and txn.type.value != self._type:
            return False
        if self._text:
            # Match either the counterparty or any card in the transaction.
            haystack = f"{txn.counterparty or ''} {row.items_label}".lower()
            if self._text not in haystack:
                return False
        return True


class _VoidDialog(QDialog):
    """Confirm reversing a posted transaction — reverses effects, keeps history."""

    def __init__(self, txn, cash: str, margin: str, items: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Void transaction")
        t = theme.active()

        title = QLabel("Void this transaction?")
        title.setStyleSheet("font-size:15px; font-weight:700;")
        body = QLabel(
            "This reverses its effects — restoring the lots it drew down and "
            "backing out the cash — but keeps it visible in history."
        )
        body.setWordWrap(True)
        body.setStyleSheet(f"color:{t.ink2};")

        summary = QFrame()
        summary.setStyleSheet(
            f"background:{t.surface2}; border:1px solid {t.line}; border-radius:10px;"
        )
        s_layout = QVBoxLayout(summary)
        s_layout.setContentsMargins(11, 10, 11, 10)
        s_layout.setSpacing(6)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        chips.addWidget(_pill(txn.type.value.capitalize(), _type_color(txn.type.value)))
        chips.addWidget(_chip(txn.occurred_at, t.ink2))
        if cash:
            chips.addWidget(_chip(cash, _money_tone(txn.cash_delta_cents)))
        if margin:
            chips.addWidget(_chip(f"margin {margin}", t.ink2))
        chips.addStretch(1)
        s_layout.addLayout(chips)
        if items:
            item_lbl = QLabel(items)
            item_lbl.setStyleSheet(f"color:{t.ink2}; font-size:12px;")
            item_lbl.setWordWrap(True)
            s_layout.addWidget(item_lbl)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        confirm = QPushButton("Void transaction")
        confirm.clicked.connect(self.accept)
        confirm.setStyleSheet(
            f"background:{t.accent2}; color:#ffffff; border:none; "
            "border-radius:8px; padding:7px 15px; font-weight:600;"
        )
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)

        layout = QVBoxLayout(self)
        layout.setSpacing(13)
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addWidget(summary)
        layout.addLayout(buttons)
        self.setMinimumWidth(400)


def _money_tone(cents: int) -> str:
    t = theme.active()
    if cents > 0:
        return t.pos
    if cents < 0:
        return t.neg
    return t.ink3


def _pill(text: str, color: str) -> QLabel:
    """A filled, tinted category pill."""
    c = QColor(color)
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color:{color}; "
        f"background:rgba({c.red()},{c.green()},{c.blue()},0.15);"
        "border-radius:9px; padding:2px 9px; font-size:11px; font-weight:600;"
    )
    return lbl


def _chip(text: str, color: str) -> QLabel:
    """A ghost/outline chip (mono) for a value."""
    t = theme.active()
    lbl = QLabel(text)
    lbl.setFont(QFont(theme.mono_family()))
    lbl.setStyleSheet(
        f"color:{color}; background:{t.surface}; border:1px solid {t.line2};"
        "border-radius:9px; padding:2px 9px; font-size:11px;"
    )
    return lbl


def _empty_state(glyph: str, title: str, body: str, action: str) -> tuple:
    """A centred empty-state widget; returns (widget, body_label, action_button)."""
    t = theme.active()
    halo = QLabel()
    halo.setPixmap(icons.pixmap(glyph, t.accent_ink, 34))
    halo.setAlignment(Qt.AlignCenter)
    head = QLabel(title)
    head.setStyleSheet("font-size:16px; font-weight:700;")
    head.setAlignment(Qt.AlignCenter)
    text = QLabel(body)
    text.setStyleSheet(f"color:{t.ink2};")
    text.setWordWrap(True)
    text.setAlignment(Qt.AlignCenter)
    text.setMaximumWidth(420)
    button = QPushButton(action)
    button.setObjectName("primary")

    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setAlignment(Qt.AlignCenter)
    layout.setSpacing(11)
    layout.addWidget(halo)
    layout.addWidget(head)
    layout.addWidget(text, 0, Qt.AlignCenter)
    layout.addWidget(button, 0, Qt.AlignCenter)
    return box, text, button


class HistoryView(QWidget):
    edit_requested = Signal(int)  # transaction id to edit
    changed = Signal()  # a void changed the ledger
    new_transaction_requested = Signal()  # empty-state CTA → Transaction window

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._txns = TransactionRepository(conn)
        self._lines = TransactionLineRepository(conn)
        self._consumptions = LotConsumptionRepository(conn)
        self._lots = LotRepository(conn)
        self._items = ItemRepository(conn)
        self._reporting = ReportingService(conn)
        self._service = TransactionService(conn)

        self._model = HistoryTableModel()
        self._proxy = _HistoryFilterProxy()
        self._proxy.setSourceModel(self._model)

        title = QLabel("History")
        title.setObjectName("pageTitle")

        self._type = QComboBox()
        self._type.addItems(_TYPES)
        self._type.currentTextChanged.connect(self._on_filter_changed)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search cards or counterparty…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_changed)

        self._edit_btn = QPushButton("Edit…")
        self._edit_btn.clicked.connect(self._edit_selected)
        self._void_btn = QPushButton("Void…")
        self._void_btn.clicked.connect(self._void_selected)

        filter_bar = QHBoxLayout()
        filter_bar.addWidget(QLabel("Type:"))
        filter_bar.addWidget(self._type)
        filter_bar.addWidget(self._search, 1)
        filter_bar.addWidget(self._edit_btn)
        filter_bar.addWidget(self._void_btn)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(0, Qt.AscendingOrder)
        self._table.setItemDelegate(_PillDelegate(self._table))
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.selectionModel().selectionChanged.connect(self._show_detail)

        # Empty states swap in for the table (no transactions / no matches).
        self._empty, self._empty_body, empty_btn = _empty_state(
            "scroll-text",
            "No transactions yet",
            "Record a purchase, sale, or trade and it lands here — with inferred "
            "cost basis, realized margin, and a full per-card drill-down.",
            "Record a transaction",
        )
        empty_btn.clicked.connect(self.new_transaction_requested)
        self._no_match, self._no_match_body, clear_btn = _empty_state(
            "search",
            "No matching transactions",
            "Nothing matches these filters.",
            "Clear filters",
        )
        clear_btn.setObjectName("")
        clear_btn.clicked.connect(self._clear_filters)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._table)  # 0
        self._stack.addWidget(self._empty)  # 1
        self._stack.addWidget(self._no_match)  # 2

        # Bottom pane: transaction summary + a table of its cards.
        self._detail_header = QLabel("Select a transaction to see its items.")
        self._detail_header.setTextFormat(Qt.RichText)
        self._detail_header.setStyleSheet(
            f"padding:8px 4px; color:{theme.active().ink3};"
        )
        self._detail_table = QTableWidget(0, len(_DETAIL_HEADERS))
        self._detail_table.setHorizontalHeaderLabels(_DETAIL_HEADERS)
        self._detail_table.verticalHeader().setVisible(False)
        self._detail_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._detail_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._detail_table.setAlternatingRowColors(True)
        self._detail_table.horizontalHeader().setStretchLastSection(True)

        detail_box = QWidget()
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self._detail_header)
        detail_layout.addWidget(self._detail_table)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._stack)
        splitter.addWidget(detail_box)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(filter_bar)
        layout.addWidget(splitter)

        self.refresh()

    # -- filters ------------------------------------------------------------

    def _on_filter_changed(self, value: str) -> None:
        self._proxy.set_type(value)
        self._update_state()

    def _on_search_changed(self, text: str) -> None:
        self._proxy.set_text(text)
        self._update_state()

    def _clear_filters(self) -> None:
        self._type.setCurrentText("All")
        self._search.clear()

    def refresh(self) -> None:
        rows = [
            HistoryRow(
                transaction=txn,
                margin_cents=self._reporting.transaction_margin_cents(txn.id),
                proceeds_cents=self._reporting.transaction_proceeds_cents(txn.id),
                items_label=", ".join(self._item_labels(txn.id)),
            )
            for txn in self._txns.list_all()
        ]
        self._model.set_rows(rows)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setStretchLastSection(True)
        self._clear_detail()
        self._update_state()

    def _update_state(self) -> None:
        """Show the table, or an empty state (no transactions / no matches)."""
        if self._model.rowCount() == 0:
            self._stack.setCurrentWidget(self._empty)
        elif self._proxy.rowCount() == 0:
            self._no_match_body.setText(
                f"Nothing matches these filters. Clear them to see all "
                f"{self._model.rowCount()} transactions."
            )
            self._stack.setCurrentWidget(self._no_match)
        else:
            self._stack.setCurrentWidget(self._table)

    def _item_labels(self, transaction_id: int) -> list[str]:
        """['Articuno 161/159', 'Charizard 004/102', …] for a transaction."""
        labels = []
        for line in self._lines.list_for_transaction(transaction_id):
            item = self._items.get(line.item_id)
            name = item.name if item else f"item {line.item_id}"
            number = (item.card_number if item else None) or ""
            labels.append(f"{name} {number}".strip())
        return labels

    def _selected_transaction(self):
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        source = self._proxy.mapToSource(indexes[0])
        return self._model.row_at(source.row()).transaction

    def _edit_selected(self) -> None:
        txn = self._selected_transaction()
        if txn is None:
            QMessageBox.information(self, "Edit", "Select a transaction first.")
            return
        if txn.status is TransactionStatus.VOIDED:
            QMessageBox.information(self, "Edit", "That transaction is voided.")
            return
        self.edit_requested.emit(txn.id)

    def _void_selected(self) -> None:
        txn = self._selected_transaction()
        if txn is None:
            QMessageBox.information(self, "Void", "Select a transaction first.")
            return
        if txn.status is TransactionStatus.VOIDED:
            QMessageBox.information(self, "Void", "That transaction is already voided.")
            return
        row = self._selected_row()
        margin = ""
        if row and row.proceeds_cents:
            pct = percent(self._reporting.gross_margin_pct(txn.id))
            margin = f"{money(row.margin_cents)} ({pct})"
        dialog = _VoidDialog(
            txn,
            money(txn.cash_delta_cents) if txn.cash_delta_cents else "",
            margin,
            ", ".join(self._item_labels(txn.id)),
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self._service.void(txn.id)
        except ServiceError as exc:
            QMessageBox.warning(self, "Could not void", str(exc))
            return
        self.refresh()
        self.changed.emit()

    def _selected_row(self) -> HistoryRow | None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self._model.row_at(self._proxy.mapToSource(indexes[0]).row())

    def _clear_detail(self) -> None:
        t = theme.active()
        self._detail_header.setStyleSheet(f"padding:8px 4px; color:{t.ink3};")
        self._detail_header.setText("Select a transaction to see its items.")
        self._detail_table.setRowCount(0)

    def _show_detail(self) -> None:
        row = self._selected_row()
        if row is None:
            self._clear_detail()
            return
        txn = row.transaction
        t = theme.active()

        def span(text: str, color: str, bold: bool = False) -> str:
            weight = "font-weight:600;" if bold else ""
            return f'<span style="color:{color};{weight}">{text}</span>'

        parts = [
            span(txn.type.value.title(), _type_color(txn.type.value), bold=True),
            span(txn.occurred_at, t.ink2),
            span(
                txn.status.value.title(),
                t.ink3 if txn.status is TransactionStatus.VOIDED else t.ink2,
            ),
            "Cash "
            + span(money(txn.cash_delta_cents), _money_tone(txn.cash_delta_cents)),
        ]
        if txn.counterparty:
            parts.append(span(txn.counterparty, t.ink))
        if row.proceeds_cents:
            parts.append(
                "Margin "
                + span(
                    f"{money(row.margin_cents)} "
                    f"({percent(self._reporting.gross_margin_pct(txn.id))})",
                    _money_tone(row.margin_cents),
                )
            )
        sep = f'  <span style="color:{t.line2}">·</span>  '
        self._detail_header.setStyleSheet(f"padding:8px 4px; color:{t.ink2};")
        self._detail_header.setText(sep.join(parts))

        cells = self._detail_rows(txn.id)
        self._detail_table.setRowCount(len(cells))
        mono = QFont(theme.mono_family())
        for r, values in enumerate(cells):
            for c, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if c in (0, 4):  # SKU + price → mono
                    cell.setFont(mono)
                if c == 4:  # price → right-aligned
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 0 and text:  # SKU tinted like a link
                    cell.setForeground(QColor(t.accent_ink))
                self._detail_table.setItem(r, c, cell)
        self._detail_table.resizeColumnsToContents()
        self._detail_table.horizontalHeader().setStretchLastSection(True)

    def _detail_rows(self, transaction_id: int) -> list[list[str]]:
        """One row per lot in the transaction, with the requested columns."""
        rows: list[list[str]] = []
        for line in self._lines.list_for_transaction(transaction_id):
            item = self._items.get(line.item_id)
            if item is None:
                continue
            if line.direction is Direction.IN:
                lots = self._lots.list_for_acquired_line(line.id)
                pairs = [(lot.sku or "", lot.unit_cost_cents) for lot in lots]
            else:  # OUT — draw price basis from the lots it consumed
                pairs = []
                for cons in self._consumptions.list_for_line(line.id):
                    lot = self._lots.get(cons.lot_id)
                    sku = (lot.sku if lot else "") or ""
                    pairs.append((sku, cons.unit_cost_cents))
            if not pairs:  # fallback so the card still shows
                pairs = [("", line.unit_price_cents)]
            for sku, price_cents in pairs:
                rows.append(
                    [
                        sku,
                        item.name,
                        item.set_name or "",
                        item.card_number or "",
                        money(price_cents),
                        item.notes or "",
                        _cond_grade(item),
                        item.language.value,
                    ]
                )
        return rows
