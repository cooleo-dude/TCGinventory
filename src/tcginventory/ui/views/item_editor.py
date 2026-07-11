"""Item editor dialog — maintain an existing item (F-IT-3/4/5/6).

Items are created automatically by transactions; this only *edits* them: fix
identity typos (name/set/number/condition/language/grading), refresh market
value/links, set location/notes, toggle the review flag. Delete is guarded —
blocked while any lot or line references the item (use an adjustment-out
instead), matching the ratified soft-delete rule.

Editing identity fields does not rewrite existing lot SKUs (those are minted at
acquisition and remain stable lookup keys).
"""

from __future__ import annotations

import sqlite3

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
)

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import (
    ItemRepository,
    LotRepository,
    TransactionLineRepository,
)
from tcginventory.domain.enums import (
    Category,
    Condition,
    GradingCompany,
    Language,
)


class ItemEditorDialog(QDialog):
    def __init__(self, conn: sqlite3.Connection, item_id: int, parent=None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._items = ItemRepository(conn)
        self._item = self._items.get(item_id)
        item = self._item
        self.setWindowTitle(f"Edit — {item.name if item else 'item'}")

        self._name = QLineEdit(item.name if item else "")

        self._category = QComboBox()
        for cat in Category:
            self._category.addItem(cat.value.capitalize(), cat.value)
        if item:
            self._select(self._category, item.category.value)

        self._set_code = QLineEdit(item.set_code or "" if item else "")
        self._set_name = QLineEdit(item.set_name or "" if item else "")
        self._card_number = QLineEdit(item.card_number or "" if item else "")

        self._language = QComboBox()
        for lang in Language:
            self._language.addItem(lang.value, lang.value)
        if item:
            self._select(self._language, item.language.value)

        self._graded = QCheckBox("Graded")
        self._graded.toggled.connect(self._toggle_graded)

        self._condition = QComboBox()
        for cond in Condition:
            self._condition.addItem(cond.value, cond.value)
        if item and item.condition:
            self._select(self._condition, item.condition.value)

        self._company = QComboBox()
        for company in GradingCompany:
            self._company.addItem(company.value, company.value)
        if item and item.grading_company:
            self._select(self._company, item.grading_company.value)
        self._grade = QLineEdit(item.grade or "" if item else "")
        self._cert = QLineEdit(item.cert_number or "" if item else "")

        self._market = QDoubleSpinBox()
        self._market.setPrefix("$")
        self._market.setDecimals(2)
        self._market.setMaximum(10_000_000.0)
        self._market.setGroupSeparatorShown(True)
        if item and item.market_value_cents is not None:
            self._market.setValue(item.market_value_cents / 100)

        self._location = QLineEdit(item.location or "" if item else "")
        self._tcgplayer = QLineEdit(item.tcgplayer_url or "" if item else "")
        self._ebay = QLineEdit(item.ebay_url or "" if item else "")
        self._notes = QLineEdit(item.notes or "" if item else "")
        self._review = QCheckBox("Mark for review (price is manual, poll-protected)")
        if item:
            self._review.setChecked(item.review)

        form = QFormLayout(self)
        form.addRow("Name:", self._name)
        form.addRow("Category:", self._category)
        form.addRow("Set code:", self._set_code)
        form.addRow("Set:", self._set_name)
        form.addRow("Card #:", self._card_number)
        form.addRow("Language:", self._language)
        form.addRow("", self._graded)
        form.addRow("Condition:", self._condition)
        form.addRow("Grading company:", self._company)
        form.addRow("Grade:", self._grade)
        form.addRow("Cert #:", self._cert)
        form.addRow("Market value:", self._market)
        form.addRow("Location:", self._location)
        form.addRow("TCGplayer URL:", self._tcgplayer)
        form.addRow("eBay URL:", self._ebay)
        form.addRow("Notes:", self._notes)
        form.addRow("", self._review)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        delete = QPushButton("Delete…")
        delete.clicked.connect(self._delete)
        buttons.addButton(delete, QDialogButtonBox.DestructiveRole)
        form.addRow(buttons)

        self._toggle_graded(item.is_graded if item else False)
        if item:
            self._graded.setChecked(item.is_graded)

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        pos = combo.findData(value)
        if pos >= 0:
            combo.setCurrentIndex(pos)

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
        item.language = Language(self._language.currentData())
        item.is_graded = graded
        item.condition = None if graded else Condition(self._condition.currentData())
        item.grading_company = (
            GradingCompany(self._company.currentData()) if graded else None
        )
        item.grade = (self._grade.text().strip() or None) if graded else None
        item.cert_number = (self._cert.text().strip() or None) if graded else None
        item.market_value_cents = round(self._market.value() * 100)
        item.market_value_asof = now_iso()[:10]
        item.location = self._location.text().strip() or None
        item.tcgplayer_url = self._tcgplayer.text().strip() or None
        item.ebay_url = self._ebay.text().strip() or None
        item.notes = self._notes.text().strip() or None
        item.review = self._review.isChecked()
        self._items.update(item)
        self._conn.commit()
        self.accept()

    def _delete(self) -> None:
        if self._item is None:
            return
        lots = LotRepository(self._conn).list_for_item(self._item.id)
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
