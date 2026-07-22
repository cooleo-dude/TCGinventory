"""A small button that collects card photos to attach to an entry row.

Holds a list of image file paths in memory while editing; the owning screen
ingests them into the vision training store when the entry is committed. Clicking
opens a manager dialog (thumbnails + add/remove).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
_PATH_ROLE = Qt.UserRole + 1


class ImageCaptureButton(QPushButton):
    """Collects image file paths to attach to a card; shows the count."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._paths: list[str] = []
        self.setFixedWidth(52)
        self.setToolTip("Attach photos of this card (for image-recognition training)")
        self.clicked.connect(self._open_dialog)
        self._sync()

    def paths(self) -> list[str]:
        return list(self._paths)

    def set_paths(self, paths) -> None:
        self._paths = [str(p) for p in paths]
        self._sync()

    def _sync(self) -> None:
        n = len(self._paths)
        self.setText(f"📷 {n}" if n else "📷")

    def _open_dialog(self) -> None:
        dialog = _ImageListDialog(self._paths, self)
        if dialog.exec() == QDialog.Accepted:
            self._paths = dialog.paths()
            self._sync()


class _ImageListDialog(QDialog):
    """Thumbnail list of attached photos with add / remove."""

    def __init__(self, paths, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Card photos")
        self._list = QListWidget()
        self._list.setViewMode(QListWidget.IconMode)
        self._list.setIconSize(QSize(96, 134))
        self._list.setResizeMode(QListWidget.Adjust)
        self._list.setSelectionMode(QListWidget.ExtendedSelection)
        for path in paths:
            self._add_item(path)

        add = QPushButton("Add images…")
        add.clicked.connect(self._add)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove)
        top = QHBoxLayout()
        top.addWidget(add)
        top.addWidget(remove)
        top.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self._list, 1)
        layout.addWidget(buttons)
        self.resize(380, 340)

    def _add_item(self, path: str) -> None:
        pixmap = QPixmap(path)
        icon = QIcon(pixmap) if not pixmap.isNull() else QIcon()
        item = QListWidgetItem(icon, Path(path).name)
        item.setData(_PATH_ROLE, path)
        self._list.addItem(item)

    def _add(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choose card photos", "", _IMAGE_FILTER
        )
        existing = set(self.paths())
        for f in files:
            if f not in existing:
                self._add_item(f)

    def _remove(self) -> None:
        for item in self._list.selectedItems():
            self._list.takeItem(self._list.row(item))

    def paths(self) -> list[str]:
        return [
            self._list.item(i).data(_PATH_ROLE) for i in range(self._list.count())
        ]
