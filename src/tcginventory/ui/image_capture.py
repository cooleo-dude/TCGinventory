"""A button that collects card photos to attach to a transaction/auction.

A photo is a **scene of one or more cards**; on commit the owning screen ingests
each photo into the vision store, grouped as one **capture** documenting every
card in the entry. Two photos can optionally be **linked as front/back** of the
same cards (not always applicable). Holds the chosen photos in memory while
editing; clicking opens a manager dialog (thumbnails, add/remove, link).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
_PATH_ROLE = Qt.UserRole + 1
_SIDE_ROLE = Qt.UserRole + 2
_PAIR_ROLE = Qt.UserRole + 3


@dataclass
class PhotoEntry:
    """A supplied photo, optionally tagged as a card face and linked to its pair."""

    path: str
    side: str | None = None  # 'front' | 'back' | None
    pair: int | None = None  # shared by a linked front+back within the entry


class ImageCaptureButton(QPushButton):
    """Collects photos to attach to an entry; shows the count."""

    def __init__(self, label: str = "📷 Photos", parent=None) -> None:
        super().__init__(parent)
        self._photos: list[PhotoEntry] = []
        self._label = label
        self.setToolTip(
            "Attach photos of the cards in this entry (for image-recognition "
            "training). A photo may contain several cards; two photos can be "
            "linked as front/back."
        )
        self.clicked.connect(self._open_dialog)
        self._sync()

    def photos(self) -> list[PhotoEntry]:
        return list(self._photos)

    def set_photos(self, entries) -> None:
        self._photos = [
            e if isinstance(e, PhotoEntry) else PhotoEntry(str(e)) for e in entries
        ]
        self._sync()

    def set_paths(self, paths) -> None:
        self.set_photos([PhotoEntry(str(p)) for p in paths])

    def _sync(self) -> None:
        n = len(self._photos)
        self.setText(f"{self._label} ({n})" if n else self._label)

    def _open_dialog(self) -> None:
        dialog = _ImageListDialog(self._photos, self)
        if dialog.exec() == QDialog.Accepted:
            self._photos = dialog.photos()
            self._sync()


class _ImageListDialog(QDialog):
    """Thumbnail list of a capture's photos: add / remove / link front-back."""

    def __init__(self, photos, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Card photos")
        self._list = QListWidget()
        self._list.setViewMode(QListWidget.IconMode)
        self._list.setIconSize(QSize(96, 134))
        self._list.setResizeMode(QListWidget.Adjust)
        self._list.setSelectionMode(QListWidget.ExtendedSelection)
        self._list.setWordWrap(True)
        for entry in photos:
            self._add_item(entry)

        add = QPushButton("Add images…")
        add.clicked.connect(self._add)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove)
        link = QPushButton("Link front/back")
        link.clicked.connect(self._link)
        unlink = QPushButton("Unlink")
        unlink.clicked.connect(self._unlink)
        top = QHBoxLayout()
        for button in (add, remove, link, unlink):
            top.addWidget(button)
        top.addStretch(1)

        hint = QLabel(
            "Select two photos and “Link front/back” to mark them as the two "
            "faces of the same cards (topmost = front)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #928374;")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(hint)
        layout.addWidget(self._list, 1)
        layout.addWidget(buttons)
        self.resize(420, 360)

    @staticmethod
    def _caption(entry: PhotoEntry) -> str:
        name = Path(entry.path).name
        if entry.side and entry.pair is not None:
            return f"{name}\n▸ {entry.side} (pair {entry.pair})"
        return name

    def _entry(self, item: QListWidgetItem) -> PhotoEntry:
        return PhotoEntry(
            item.data(_PATH_ROLE), item.data(_SIDE_ROLE), item.data(_PAIR_ROLE)
        )

    def _refresh(self, item: QListWidgetItem) -> None:
        item.setText(self._caption(self._entry(item)))

    def _add_item(self, entry: PhotoEntry) -> None:
        pixmap = QPixmap(entry.path)
        icon = QIcon(pixmap) if not pixmap.isNull() else QIcon()
        item = QListWidgetItem(icon, "")
        item.setData(_PATH_ROLE, entry.path)
        item.setData(_SIDE_ROLE, entry.side)
        item.setData(_PAIR_ROLE, entry.pair)
        item.setText(self._caption(entry))
        self._list.addItem(item)

    def _add(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choose card photos", "", _IMAGE_FILTER
        )
        existing = {
            self._list.item(i).data(_PATH_ROLE) for i in range(self._list.count())
        }
        for f in files:
            if f not in existing:
                self._add_item(PhotoEntry(f))

    def _remove(self) -> None:
        for item in self._list.selectedItems():
            self._list.takeItem(self._list.row(item))

    def _next_pair(self) -> int:
        used = [
            self._list.item(i).data(_PAIR_ROLE) for i in range(self._list.count())
        ]
        nums = [p for p in used if isinstance(p, int)]
        return (max(nums) + 1) if nums else 1

    def _link(self) -> None:
        items = sorted(self._list.selectedItems(), key=self._list.row)
        if len(items) != 2:
            QMessageBox.information(
                self, "Link front/back", "Select exactly two photos to link."
            )
            return
        pair = self._next_pair()
        for side, item in zip(("front", "back"), items, strict=False):
            item.setData(_SIDE_ROLE, side)
            item.setData(_PAIR_ROLE, pair)
            self._refresh(item)

    def _unlink(self) -> None:
        for item in self._list.selectedItems():
            item.setData(_SIDE_ROLE, None)
            item.setData(_PAIR_ROLE, None)
            self._refresh(item)

    def photos(self) -> list[PhotoEntry]:
        return [self._entry(self._list.item(i)) for i in range(self._list.count())]
