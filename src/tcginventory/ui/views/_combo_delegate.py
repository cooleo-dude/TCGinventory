"""Generic drop-down delegate for editing an enum-valued grid column inline."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QStyledItemDelegate


class ComboDelegate(QStyledItemDelegate):
    """Edit a cell with a fixed set of options; commits the chosen value string."""

    def __init__(self, options: list[str], parent=None) -> None:
        super().__init__(parent)
        self._options = options

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        # Drop the option list *below* the cell instead of the native popup that
        # overlays the current value.
        combo.setStyleSheet("QComboBox { combobox-popup: 0; }")
        combo.addItems(self._options)
        return combo

    def setEditorData(self, editor: QComboBox, index) -> None:
        pos = editor.findText(str(index.data(Qt.EditRole)))
        if pos >= 0:
            editor.setCurrentIndex(pos)

    def setModelData(self, editor: QComboBox, model, index) -> None:
        model.setData(index, editor.currentText(), Qt.EditRole)
