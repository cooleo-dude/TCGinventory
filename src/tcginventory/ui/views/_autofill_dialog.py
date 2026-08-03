"""Review dialog for image inference autofill (spec 06, Part 2).

Shows a detected scene — the image with numbered boxes — beside a table of the
matcher's best guesses. The user confirms identities, edits or clears wrong ones,
unchecks false detections, then the entry window turns the accepted rows into
card rows. Low-confidence or unidentified boxes are flagged for review.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QModelIndex, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPen,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QCompleter,
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

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.ui import theme
from tcginventory.ui.completer_popup import show_fitted

_BOX_PEN = QColor(theme.DARK.accent)
_CARD_ID_ROLE = Qt.UserRole + 1


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
    """One detection's editable row. When a catalog connection is supplied, the
    Name field autocompletes so a **correction** re-picks a real card_id (the
    highest-value label for the feedback loop)."""

    def __init__(self, det, conn=None) -> None:
        self.det = det
        label = det.label
        self.guessed_name = (label.name if label else None) or ""
        self.guessed_id = label.card_id if label else None
        self.picked_id: str | None = None
        self.picked_name: str | None = None
        self._conn = conn
        self.include = QCheckBox()
        self.include.setChecked(True)
        self.name = QLineEdit(self.guessed_name)
        self.set = QLineEdit((label.set_name if label else None) or "")
        self.number = QLineEdit((label.card_number if label else None) or "")
        self.qty = QSpinBox()
        self.qty.setMinimum(1)
        self.qty.setMaximum(9999)
        if conn is not None:
            self._install_completer()

    def _install_completer(self) -> None:
        self._matches: dict[str, object] = {}
        self._model = QStandardItemModel(self.name)
        self._completer = QCompleter(self._model, self.name)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.name.setCompleter(self._completer)
        self.name.textEdited.connect(self._on_name_edited)
        self._completer.activated[QModelIndex].connect(self._pick_match)

    def _on_name_edited(self, text: str) -> None:
        if len(text.strip()) < 2:
            return
        repo = CardCatalogRepository(self._conn)
        matches = repo.search(text, limit=20)
        self._matches = {c.card_id: c for c in matches}
        self._model.clear()
        for card in matches:
            item = QStandardItem(
                f"{card.name}  ·  {card.set_name or '?'}  "
                f"#{repo.display_number(card) or '?'}"
            )
            item.setData(card.card_id, _CARD_ID_ROLE)
            self._model.appendRow(item)
        if matches:
            show_fitted(self._completer, self.name)

    def _pick_match(self, index: QModelIndex) -> None:
        card = self._matches.get(index.data(_CARD_ID_ROLE))
        if card is None:
            return
        self.picked_id = card.card_id
        self.picked_name = card.name
        self.name.setText(card.name)
        self.set.setText(card.set_name or "")
        number = CardCatalogRepository(self._conn).display_number(card)
        self.number.setText(number or card.full_number or card.number or "")

    def resolved_id(self, name: str) -> str | None:
        """The card_id backing the current name: a re-pick, or the original guess
        if it was accepted unedited; None for free-typed text."""
        if self.picked_id and name == self.picked_name:
            return self.picked_id
        if self.guessed_id and name == self.guessed_name:
            return self.guessed_id
        return None

    def accepted(self) -> AcceptedCard | None:
        if not self.include.isChecked():
            return None
        name = self.name.text().strip()
        if not name:
            return None
        card_id = self.resolved_id(name)
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

    def __init__(self, image_path: str, detections, conn=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Autofill cards from photo")
        self.resize(900, 560)
        self._rows: list[_Row] = [_Row(d, conn) for d in detections]

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
