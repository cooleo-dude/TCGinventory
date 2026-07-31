"""Review dialog for image inference autofill (spec 06, Part 2).

Shows a detected scene — the image with numbered boxes — beside a table of the
matcher's best guesses. The user confirms identities, edits or clears wrong ones,
unchecks false detections, then the entry window turns the accepted rows into
card rows. Low-confidence or unidentified boxes are flagged for review.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGraphicsScene,
    QGraphicsView,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

_BOX_PEN = QColor("#d3869b")


@dataclass
class AcceptedCard:
    card_id: str | None  # kept only when the user accepts the guess unedited
    name: str
    set_name: str | None
    card_number: str | None
    quantity: int
    review: bool  # low-confidence / unidentified / edited → double-check
    box: tuple[float, float, float, float]


class _Row:
    __slots__ = (
        "det", "guessed_name", "guessed_id", "include",
        "name", "set", "number", "qty",
    )

    def __init__(self, det) -> None:
        self.det = det
        label = det.label
        self.guessed_name = (label.name if label else None) or ""
        self.guessed_id = label.card_id if label else None
        self.include = QCheckBox()
        self.include.setChecked(True)
        self.name = QLineEdit(self.guessed_name)
        self.set = QLineEdit((label.set_name if label else None) or "")
        self.number = QLineEdit((label.card_number if label else None) or "")
        self.qty = QSpinBox()
        self.qty.setMinimum(1)
        self.qty.setMaximum(9999)

    def accepted(self) -> AcceptedCard | None:
        if not self.include.isChecked():
            return None
        name = self.name.text().strip()
        if not name:
            return None
        edited = name != self.guessed_name
        card_id = self.guessed_id if (self.guessed_id and not edited) else None
        return AcceptedCard(
            card_id=card_id,
            name=name,
            set_name=self.set.text().strip() or None,
            card_number=self.number.text().strip() or None,
            quantity=self.qty.value(),
            review=(not self.det.confident) or card_id is None,
            box=self.det.box,
        )


class AutofillDialog(QDialog):
    _HEADERS = ["Add", "#", "Name", "Set", "No.", "Match", "Qty"]

    def __init__(self, image_path: str, detections, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Autofill cards from photo")
        self.resize(900, 560)
        self._rows: list[_Row] = [_Row(d) for d in detections]

        scene = QGraphicsScene(self)
        pixmap = QPixmap(str(image_path))
        if not pixmap.isNull():
            scene.addPixmap(pixmap)
            scene.setSceneRect(QRectF(pixmap.rect()))
            pw, ph = max(1, pixmap.width()), max(1, pixmap.height())
            for det in detections:
                x, y, w, h = det.box
                rect = QRectF(x * pw, y * ph, w * pw, h * ph)
                scene.addRect(rect, QPen(_BOX_PEN, 0, Qt.DashLine))
                badge = scene.addSimpleText(str(det.number))
                badge.setBrush(QBrush(QColor("#ffffff")))
                badge.setPos(rect.topLeft())
        view = QGraphicsView(scene)
        if not pixmap.isNull():
            view.fitInView(scene.sceneRect(), Qt.KeepAspectRatio)

        self._table = QTableWidget(len(self._rows), len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        for r, row in enumerate(self._rows):
            self._table.setCellWidget(r, 0, row.include)
            num = QLabel(str(row.det.number))
            num.setAlignment(Qt.AlignCenter)
            self._table.setCellWidget(r, 1, num)
            self._table.setCellWidget(r, 2, row.name)
            self._table.setCellWidget(r, 3, row.set)
            self._table.setCellWidget(r, 4, row.number)
            if row.det.label is None:
                conf = QLabel("— none")
                conf.setToolTip("No match in the labeled corpus yet — enter it")
            else:
                pct = int(round(max(0.0, min(1.0, row.det.score)) * 100))
                conf = QLabel(f"{pct}%{'' if row.det.confident else '  ⚠'}")
                if not row.det.confident:
                    conf.setToolTip("Low confidence — please double-check")
            conf.setAlignment(Qt.AlignCenter)
            self._table.setCellWidget(r, 5, conf)
            self._table.setCellWidget(r, 6, row.qty)

        confident = sum(1 for r in self._rows if r.det.confident)
        note = QLabel(
            f"{len(self._rows)} card(s) detected, {confident} confidently identified. "
            "Confirm or edit each identity and uncheck any false detections."
        )
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Add checked cards")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(view)
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.addWidget(self._table, 1)
        split.addWidget(panel)
        split.setSizes([420, 480])

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(split, 1)
        layout.addWidget(buttons)

    def result_cards(self) -> list[AcceptedCard]:
        return [card for card in (r.accepted() for r in self._rows) if card]
