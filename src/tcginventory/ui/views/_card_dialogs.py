"""Add-card dialogs for the Transaction window (give / receive)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
)

from tcginventory.data.repositories import LotRepository
from tcginventory.domain.enums import (
    Category,
    Condition,
    GradingCompany,
    Intent,
    Language,
)
from tcginventory.domain.models import Item
from tcginventory.services.transactions import IncomingCard, OutgoingCard


def _money_spin() -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setPrefix("$")
    spin.setDecimals(2)
    spin.setMaximum(10_000_000.0)
    spin.setGroupSeparatorShown(True)
    return spin


class OutgoingCardDialog(QDialog):
    """Pick a card from the collection to give up (in-stock, non-ripping).

    A SKU box resolves an entered SKU straight to its card for quick pulling.
    """

    def __init__(self, sellable: list[tuple[Item, int]], conn, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add card to give")
        self._sellable = sellable
        self._conn = conn

        self._sku = QLineEdit()
        self._sku.setPlaceholderText("Type a SKU and press Enter…")
        self._sku.returnPressed.connect(self._lookup_sku)
        self._sku_status = QLineEdit()
        self._sku_status.setReadOnly(True)
        self._sku_status.setFrame(False)

        self._item = QComboBox()
        for item, available in sellable:
            label = f"{item.name}"
            if item.set_name:
                label += f" ({item.set_name})"
            label += f" — {available} on hand"
            self._item.addItem(label, item.id)
        self._item.currentIndexChanged.connect(self._sync_max)

        self._qty = QSpinBox()
        self._qty.setMinimum(1)

        self._agreed = _money_spin()

        form = QFormLayout(self)
        form.addRow("SKU lookup:", self._sku)
        form.addRow("", self._sku_status)
        form.addRow("Card:", self._item)
        form.addRow("Quantity:", self._qty)
        form.addRow("Agreed value (per unit):", self._agreed)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._sync_max()

    def _lookup_sku(self) -> None:
        sku = self._sku.text().strip()
        if not sku:
            return
        lot = LotRepository(self._conn).find_by_sku(sku)
        if lot is None:
            self._sku_status.setText(f"No card found for '{sku}'")
            return
        index = self._item.findData(lot.item_id)
        if index >= 0:
            self._item.setCurrentIndex(index)
            self._sku_status.setText(f"→ {self._item.currentText().split(' — ')[0]}")
        else:
            self._sku_status.setText("That card has no sellable (non-ripping) stock")

    def _sync_max(self) -> None:
        idx = self._item.currentIndex()
        if 0 <= idx < len(self._sellable):
            self._qty.setMaximum(self._sellable[idx][1])

    def result_card(self) -> tuple[OutgoingCard, str]:
        item_id = self._item.currentData()
        card = OutgoingCard(
            item_id=item_id,
            quantity=self._qty.value(),
            agreed_value_cents=round(self._agreed.value() * 100),
        )
        return card, self._item.currentText().split(" — ")[0]


class IncomingCardDialog(QDialog):
    """Enter a received card (identity + attributes + required market value)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add card to receive")

        self._name = QLineEdit()
        self._name.textChanged.connect(self._sync_ok)

        # Combos store the enum's *value string* as data (PySide converts our
        # str-subclass enums to plain str anyway) and show a capitalized label;
        # result_card() rebuilds the enum from the value.
        self._category = QComboBox()
        for cat in Category:
            self._category.addItem(cat.value.capitalize(), cat.value)

        self._set_name = QLineEdit()
        self._card_number = QLineEdit()

        self._language = QComboBox()
        for lang in Language:
            self._language.addItem(lang.value, lang.value)  # codes already caps

        self._graded = QCheckBox("Graded")
        self._graded.toggled.connect(self._toggle_graded)

        self._condition = QComboBox()
        for cond in Condition:
            self._condition.addItem(cond.value, cond.value)  # NM/LP… already caps

        self._company = QComboBox()
        for company in GradingCompany:
            self._company.addItem(company.value, company.value)  # PSA… already caps
        self._grade = QLineEdit()
        self._cert = QLineEdit()

        self._market = _money_spin()
        self._market.valueChanged.connect(self._sync_ok)

        self._quantity = QSpinBox()
        self._quantity.setMinimum(1)

        self._intent = QComboBox()
        for intent in Intent:
            self._intent.addItem(intent.value.capitalize(), intent.value)
        self._intent.setCurrentIndex(self._intent.findData(Intent.RESALE.value))

        self._review = QCheckBox("Mark for review (I'll set its price manually)")

        self._tcgplayer = QLineEdit()
        self._ebay = QLineEdit()
        self._notes = QLineEdit()

        form = QFormLayout(self)
        form.addRow("Name*:", self._name)
        form.addRow("Category:", self._category)
        form.addRow("Set:", self._set_name)
        form.addRow("Card #:", self._card_number)
        form.addRow("Language:", self._language)
        form.addRow("", self._graded)
        form.addRow("Condition:", self._condition)
        form.addRow("Grading company:", self._company)
        form.addRow("Grade:", self._grade)
        form.addRow("Cert #:", self._cert)
        form.addRow("Market value* (per unit):", self._market)
        form.addRow("Quantity:", self._quantity)
        form.addRow("Intent:", self._intent)
        form.addRow("", self._review)
        form.addRow("TCGplayer URL:", self._tcgplayer)
        form.addRow("eBay URL:", self._ebay)
        form.addRow("Notes:", self._notes)

        self._buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        form.addRow(self._buttons)

        self._toggle_graded(False)
        self._sync_ok()

    def _toggle_graded(self, graded: bool) -> None:
        # Condition greys out when graded; grade fields enable (Spec 02).
        self._condition.setEnabled(not graded)
        self._company.setEnabled(graded)
        self._grade.setEnabled(graded)
        self._cert.setEnabled(graded)

    def _sync_ok(self) -> None:
        valid = bool(self._name.text().strip()) and self._market.value() > 0
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(valid)

    def result_card(self) -> IncomingCard:
        graded = self._graded.isChecked()
        item = Item(
            name=self._name.text().strip(),
            category=Category(self._category.currentData()),
            set_name=self._set_name.text().strip() or None,
            card_number=self._card_number.text().strip() or None,
            language=Language(self._language.currentData()),
            condition=None if graded else Condition(self._condition.currentData()),
            is_graded=graded,
            grading_company=(
                GradingCompany(self._company.currentData()) if graded else None
            ),
            grade=self._grade.text().strip() or None if graded else None,
            cert_number=self._cert.text().strip() or None if graded else None,
            tcgplayer_url=self._tcgplayer.text().strip() or None,
            ebay_url=self._ebay.text().strip() or None,
            notes=self._notes.text().strip() or None,
        )
        return IncomingCard(
            item=item,
            quantity=self._quantity.value(),
            market_value_cents=round(self._market.value() * 100),
            intent=Intent(self._intent.currentData()),
            review=self._review.isChecked(),
        )
