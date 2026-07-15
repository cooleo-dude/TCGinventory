"""Combo-box delegate for editing a lot's Intent inline in the Inventory grid."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QStyledItemDelegate

_INTENTS = ["personal", "resale", "ripping"]


class IntentDelegate(QStyledItemDelegate):
    """Edit a lot's intent with a drop-down (personal / resale / ripping)."""

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        # Drop the list below the cell instead of overlaying the current value.
        combo.setStyleSheet("QComboBox { combobox-popup: 0; }")
        combo.addItems(_INTENTS)
        return combo

    def setEditorData(self, editor: QComboBox, index) -> None:
        pos = editor.findText(str(index.data(Qt.EditRole)))
        if pos >= 0:
            editor.setCurrentIndex(pos)

    def setModelData(self, editor: QComboBox, model, index) -> None:
        model.setData(index, editor.currentText(), Qt.EditRole)
