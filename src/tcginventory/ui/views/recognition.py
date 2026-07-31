"""Recognition window — the human-in-the-loop training surface (spec 06, M-V1b).

Load image scenes of cards on a blank background, draw numbered boxes around each
card, label them via the catalog, and save the result as localized **regions**
(box geometry + identity + side) in ``vision.db``. In *Transaction* mode an
adjustable divider splits the scene into **I Give** / **They Give** (a box's side
follows the zone its centre sits in); *Listing* mode is one lot. Multiple images
per session share one card set.

Model-agnostic: it collects the training data the detector/identifier will learn
from. Auto box-proposals (classic-CV / YOLO) toggle in here later.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

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
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFileDialog,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.data.vision import CardLabel, VisionRepository, open_vision
from tcginventory.services import vision as vision_svc

_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
_MODES = ["Transaction (I Give / They Give)", "Listing (one lot)"]
_BOX_PEN = QColor("#d79921")
_PROPOSAL_PEN = QColor("#83a598")  # detector proposals (distinct from drawn boxes)
# Cosine below this gets a ⚠ in the suggestion list (advisory; frozen DINOv2 on
# photo-vs-catalog-render is a weak baseline until the M-V2 fine-tune).
_LOW_CONFIDENCE = 0.6


@dataclass
class _Box:
    """A drawn box on an image: geometry (pixel coords) + its card label."""

    number: int
    rect: QRectF
    card: CardLabel = field(default_factory=CardLabel)


@dataclass
class _Session:
    """One image in the annotation session: its pixmap, boxes and split line."""

    path: str
    pixmap: QPixmap
    boxes: list[_Box] = field(default_factory=list)
    divider_x: float | None = None  # transaction-mode split (pixels); None = mid


class _Scene(QGraphicsScene):
    """Graphics scene that draws rubber-band boxes when in draw mode."""

    def __init__(self, on_box) -> None:
        super().__init__()
        self._on_box = on_box  # (QRectF) -> None, called with a finished box
        self._draw = False
        self._origin = None
        self._rubber: QGraphicsRectItem | None = None

    def set_draw(self, on: bool) -> None:
        self._draw = on

    def mousePressEvent(self, event) -> None:
        if self._draw and event.button() == Qt.LeftButton:
            self._origin = event.scenePos()
            self._rubber = self.addRect(
                QRectF(self._origin, self._origin), QPen(_BOX_PEN, 0)
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._rubber is not None:
            self._rubber.setRect(QRectF(self._origin, event.scenePos()).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._rubber is not None:
            rect = self._rubber.rect().normalized()
            self.removeItem(self._rubber)
            self._rubber = None
            if rect.width() > 5 and rect.height() > 5:
                self._on_box(rect)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _NameField(QLineEdit):
    """Catalog-autocompleting name field that records the picked card's identity."""

    _CARD_ID_ROLE = Qt.UserRole + 1

    def __init__(self, conn, box: _Box) -> None:
        super().__init__()
        self._conn = conn
        self._box = box
        self.setText(box.card.name or "")
        self._matches: dict[str, object] = {}
        self._model = QStandardItemModel(self)
        self._completer = QCompleter(self._model, self)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.setCompleter(self._completer)
        self.textEdited.connect(self._on_edited)
        self._completer.activated[QModelIndex].connect(self._pick)
        self.editingFinished.connect(self._on_free_text)

    def _on_edited(self, text: str) -> None:
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
            item.setData(card.card_id, self._CARD_ID_ROLE)
            self._model.appendRow(item)
        if matches:
            self._completer.complete()

    def _pick(self, index: QModelIndex) -> None:
        card = self._matches.get(index.data(self._CARD_ID_ROLE))
        if card is None:
            return
        number = CardCatalogRepository(self._conn).display_number(card)
        self._box.card = CardLabel(
            card_id=card.card_id,
            name=card.name,
            set_name=card.set_name,
            card_number=number or card.number,
            language=getattr(card, "language", None),
        )
        self.setText(card.name)

    def _on_free_text(self) -> None:
        text = self.text().strip()
        # Only overwrite when the text no longer matches the picked card's name.
        if text and text != (self._box.card.name or ""):
            self._box.card = CardLabel(name=text)

    def apply_label(self, card: CardLabel) -> None:
        """Adopt a card identity picked elsewhere (e.g. a matcher suggestion)."""
        self._box.card = card
        self.setText(card.name or "")


class _SuggestBox(QComboBox):
    """Per-row dropdown of the matcher's ranked guesses; picking one labels the box.

    Starts as a placeholder; :meth:`set_suggestions` fills it after the recognizer
    runs. Selecting an entry copies that identity into the box and the name field.
    """

    def __init__(self, box: _Box, name_field: _NameField) -> None:
        super().__init__()
        self._box = box
        self._name_field = name_field
        self._labels: list[CardLabel | None] = [None]
        self.addItem("— suggest —")
        self.setEnabled(False)
        self.activated.connect(self._on_activated)

    def set_suggestions(self, items: list[tuple[CardLabel, float]]) -> None:
        self.blockSignals(True)
        self.clear()
        self._labels = [None]
        if not items:
            self.addItem("no match in corpus")
            self.setEnabled(False)
        else:
            self.addItem("— pick a match —")
            for card, score in items:
                pct = int(round(max(0.0, min(1.0, score)) * 100))
                mark = "⚠ " if score < _LOW_CONFIDENCE else ""
                label = card.name or card.card_id or "?"
                if card.set_name:
                    label += f" · {card.set_name}"
                self.addItem(f"{mark}{label}  ({pct}%)")
                self._labels.append(card)
            self.setEnabled(True)
        self.setCurrentIndex(0)
        self.blockSignals(False)

    def _on_activated(self, index: int) -> None:
        if 0 <= index < len(self._labels) and self._labels[index] is not None:
            self._name_field.apply_label(self._labels[index])


class RecognitionView(QWidget):
    def __init__(
        self,
        conn: sqlite3.Connection,
        vision_repo=None,
        vision_dir=None,
        recognizer=None,
    ) -> None:
        super().__init__()
        self._conn = conn
        self._vision_repo = vision_repo
        self._vision_dir = vision_dir
        self._recognizer = recognizer  # lazily built on first Suggest if None
        self._rec_repo = None  # the vision repo backing the recognizer
        self._sessions: list[_Session] = []
        self._current: int = -1
        self._items: dict[int, QGraphicsRectItem] = {}  # number -> item (current)
        self._name_fields: dict[int, _NameField] = {}
        self._suggest_boxes: dict[int, _SuggestBox] = {}
        self._proposals: list[QGraphicsRectItem] = []  # classic-CV overlays
        self._divider = None  # QGraphicsLineItem for the current image

        # -- toolbar
        self._mode = QComboBox()
        self._mode.addItems(_MODES)
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        add_btn = QPushButton("Add images…")
        add_btn.clicked.connect(self._add_images)
        self._draw_btn = QPushButton("Draw boxes")
        self._draw_btn.setCheckable(True)
        self._draw_btn.toggled.connect(lambda on: self._scene.set_draw(on))
        del_btn = QPushButton("Delete box")
        del_btn.clicked.connect(self._delete_selected)
        self._suggest_btn = QPushButton("Suggest labels")
        self._suggest_btn.setToolTip(
            "Rank catalog matches for each box against the cards you've already "
            "labeled (identity matcher)"
        )
        self._suggest_btn.clicked.connect(self._run_suggestions)
        # Auto-proposal toggles. Classic CV works today (no training); YOLO waits
        # on a trained detector (M-V1.5 Phase B).
        self._cv_toggle = QCheckBox("Classic-CV boxes")
        self._cv_toggle.setToolTip(
            "Auto-detect card boxes on a blank background (no training). Accept the "
            "ones you want into numbered boxes."
        )
        self._cv_toggle.toggled.connect(self._on_cv_toggle)
        self._accept_btn = QPushButton("Accept proposals")
        self._accept_btn.setToolTip(
            "Turn the detector's proposed boxes (selected, or all) into numbered "
            "boxes you can label"
        )
        self._accept_btn.clicked.connect(self._accept_proposals)
        self._accept_btn.setEnabled(False)
        self._yolo_toggle = QCheckBox("YOLO boxes")
        self._yolo_toggle.setEnabled(False)
        self._yolo_toggle.setToolTip(
            "Trained card detector — available once a model is trained on your "
            "labeled boxes"
        )
        save_btn = QPushButton("Save annotations")
        save_btn.clicked.connect(self._save)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Mode:"))
        toolbar.addWidget(self._mode)
        toolbar.addWidget(add_btn)
        toolbar.addWidget(self._draw_btn)
        toolbar.addWidget(del_btn)
        toolbar.addWidget(self._suggest_btn)
        toolbar.addWidget(self._cv_toggle)
        toolbar.addWidget(self._accept_btn)
        toolbar.addWidget(self._yolo_toggle)
        toolbar.addStretch(1)
        toolbar.addWidget(save_btn)

        # -- canvas + image strip
        self._scene = _Scene(self._add_box)
        self._view = QGraphicsView(self._scene)
        self._strip = QListWidget()
        self._strip.setMaximumWidth(150)
        self._strip.currentRowChanged.connect(self._switch_image)

        # -- segments panel
        self._panel = QTableWidget(0, 4)
        self._panel.setHorizontalHeaderLabels(["#", "Card", "Suggestion", "Side"])
        self._panel.verticalHeader().setVisible(False)
        self._panel.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._panel.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._panel.setColumnWidth(0, 30)
        self._panel.setColumnWidth(2, 190)
        self._panel.setColumnWidth(3, 70)

        self._empty = QLabel("Add images, then draw a box around each card.")
        self._empty.setAlignment(Qt.AlignCenter)

        canvas_split = QSplitter(Qt.Horizontal)
        canvas_split.addWidget(self._strip)
        centre = QWidget()
        centre_layout = QVBoxLayout(centre)
        centre_layout.setContentsMargins(0, 0, 0, 0)
        centre_layout.addWidget(self._view, 1)
        centre_layout.addWidget(self._empty)
        canvas_split.addWidget(centre)
        canvas_split.addWidget(self._panel)
        canvas_split.setStretchFactor(1, 1)
        canvas_split.setSizes([150, 700, 300])

        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(canvas_split, 1)
        self._empty.setVisible(True)
        self._view.setVisible(False)

    # -- mode ---------------------------------------------------------------

    def _is_transaction(self) -> bool:
        return self._mode.currentIndex() == 0

    def _on_mode_changed(self, *_args) -> None:
        if self._current >= 0:
            self._load_session(self._current)  # add/remove the divider
        self._refresh_panel()

    # -- images -------------------------------------------------------------

    def _add_images(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add card scene images", "", _IMAGE_FILTER
        )
        self.add_image_paths(files)

    def add_image_paths(self, paths) -> None:
        """Load images into the session (also the test hook)."""
        for path in paths:
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                continue
            self._sessions.append(_Session(path=str(path), pixmap=pixmap))
            self._strip.addItem(str(path).rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
        if self._sessions and self._current < 0:
            self._strip.setCurrentRow(0)

    def _switch_image(self, row: int) -> None:
        if 0 <= self._current < len(self._sessions):
            self._sync_scene_to_model()
        if 0 <= row < len(self._sessions):
            self._load_session(row)

    def _load_session(self, index: int) -> None:
        self._current = index
        session = self._sessions[index]
        self._empty.setVisible(False)
        self._view.setVisible(True)
        self._scene.clear()
        self._items.clear()
        self._proposals.clear()  # scene.clear() already dropped the items
        self._divider = None
        self._scene.addPixmap(session.pixmap)
        self._scene.setSceneRect(QRectF(session.pixmap.rect()))
        for box in session.boxes:
            self._add_box_item(box)
        if self._is_transaction():
            self._add_divider(session)
        self._view.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
        self._refresh_panel()
        if self._cv_toggle.isChecked():
            self._show_cv_proposals()

    def _add_divider(self, session: _Session) -> None:
        w = session.pixmap.width()
        h = session.pixmap.height()
        if session.divider_x is None:
            session.divider_x = w / 2
        line = self._scene.addLine(
            session.divider_x, 0, session.divider_x, h, QPen(QColor("#458588"), 0)
        )
        line.setFlag(line.GraphicsItemFlag.ItemIsMovable, True)
        self._divider = line

    # -- boxes --------------------------------------------------------------

    def _next_number(self) -> int:
        session = self._sessions[self._current]
        return (max((b.number for b in session.boxes), default=0)) + 1

    def _add_box(self, rect: QRectF) -> _Box:
        """Add a box (from a drawn rubber-band) to the current image."""
        box = _Box(number=self._next_number(), rect=rect)
        self._sessions[self._current].boxes.append(box)
        self._add_box_item(box)
        self._refresh_panel()
        return box

    def _add_box_item(self, box: _Box) -> None:
        item = QGraphicsRectItem(box.rect)
        item.setPen(QPen(_BOX_PEN, 0))
        item.setBrush(QBrush(QColor(215, 153, 33, 40)))
        item.setFlag(item.GraphicsItemFlag.ItemIsMovable, True)
        item.setFlag(item.GraphicsItemFlag.ItemIsSelectable, True)
        item.setData(0, box.number)
        badge = QGraphicsSimpleTextItem(str(box.number), item)
        badge.setBrush(QBrush(QColor("#ffffff")))
        badge.setPos(box.rect.topLeft())
        self._items[box.number] = item

    def _delete_selected(self) -> None:
        session = self._sessions[self._current] if self._current >= 0 else None
        if session is None:
            return
        for item in list(self._scene.selectedItems()):
            number = item.data(0)
            if number is None:
                continue
            session.boxes = [b for b in session.boxes if b.number != number]
            self._items.pop(number, None)
            self._scene.removeItem(item)
        self._refresh_panel()

    # -- classic-CV box proposals -------------------------------------------

    def _on_cv_toggle(self, on: bool) -> None:
        if on:
            self._show_cv_proposals()
        else:
            self._clear_proposals()

    def _show_cv_proposals(self) -> None:
        """Overlay the classic-CV detector's boxes for the current image."""
        from tcginventory.services.detector import propose_card_boxes

        self._clear_proposals()
        if not (0 <= self._current < len(self._sessions)):
            return
        session = self._sessions[self._current]
        pw = max(1, session.pixmap.width())
        ph = max(1, session.pixmap.height())
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            norm_boxes = propose_card_boxes(session.path)
        finally:
            QApplication.restoreOverrideCursor()
        pen = QPen(_PROPOSAL_PEN, 0, Qt.DashLine)
        for nx, ny, nw, nh in norm_boxes:
            rect = QRectF(nx * pw, ny * ph, nw * pw, nh * ph)
            item = self._scene.addRect(rect, pen, QBrush(QColor(131, 165, 152, 30)))
            item.setFlag(item.GraphicsItemFlag.ItemIsSelectable, True)
            self._proposals.append(item)
        self._accept_btn.setEnabled(bool(self._proposals))

    def _clear_proposals(self) -> None:
        for item in self._proposals:
            self._scene.removeItem(item)
        self._proposals.clear()
        self._accept_btn.setEnabled(False)

    def _accept_proposals(self) -> None:
        """Promote proposals (selected, or all if none selected) into real boxes."""
        if not self._proposals:
            return
        selected = [it for it in self._proposals if it.isSelected()]
        targets = selected or list(self._proposals)
        for item in targets:
            self._add_box(item.rect())
            self._scene.removeItem(item)
            self._proposals.remove(item)
        self._accept_btn.setEnabled(bool(self._proposals))

    def _sync_scene_to_model(self) -> None:
        """Pull moved box geometry + the divider back into the session model."""
        if not (0 <= self._current < len(self._sessions)):
            return
        session = self._sessions[self._current]
        for box in session.boxes:
            item = self._items.get(box.number)
            if item is not None:
                box.rect = item.sceneBoundingRect()
        if self._divider is not None:
            session.divider_x = self._divider.line().x1() + self._divider.x()

    # -- segments panel -----------------------------------------------------

    def _refresh_panel(self) -> None:
        self._panel.setRowCount(0)
        self._name_fields.clear()
        self._suggest_boxes.clear()
        if not (0 <= self._current < len(self._sessions)):
            return
        session = self._sessions[self._current]
        for box in session.boxes:
            r = self._panel.rowCount()
            self._panel.insertRow(r)
            num = QTableWidgetItem(str(box.number))
            num.setTextAlignment(Qt.AlignCenter)
            self._panel.setItem(r, 0, num)
            name_field = _NameField(self._conn, box)
            suggest_box = _SuggestBox(box, name_field)
            self._name_fields[box.number] = name_field
            self._suggest_boxes[box.number] = suggest_box
            self._panel.setCellWidget(r, 1, name_field)
            self._panel.setCellWidget(r, 2, suggest_box)
            side = self._side_for(box, session) or "—"
            self._panel.setItem(r, 3, QTableWidgetItem(side))

    def _side_for(self, box: _Box, session: _Session) -> str | None:
        if not self._is_transaction():
            return None
        divider = session.divider_x
        if divider is None:
            divider = session.pixmap.width() / 2
        return "give" if box.rect.center().x() < divider else "receive"

    # -- identity suggestions (matcher) -------------------------------------

    def _ensure_recognizer(self):
        """Build the identity matcher on first use (embedder + catalog references)."""
        if self._recognizer is None:
            from tcginventory.services.recognizer import (
                Recognizer,
                catalog_reference_resolver,
            )

            self._rec_repo = self._vision_repo or VisionRepository(open_vision())
            self._recognizer = Recognizer(
                self._rec_repo,
                reference_image=catalog_reference_resolver(self._conn),
            )
        elif self._rec_repo is None:
            self._rec_repo = self._vision_repo
        return self._recognizer

    def _candidate_pool(self) -> list[str]:
        """Cards to rank against: the saved-region corpus plus anything already
        labeled in the current (unsaved) session."""
        ids: set[str] = set()
        if self._rec_repo is not None:
            ids.update(self._rec_repo.labeled_card_ids())
        for session in self._sessions:
            for box in session.boxes:
                if box.card and box.card.card_id:
                    ids.add(box.card.card_id)
        return sorted(ids)

    def _crop_for(self, full, box: _Box, session: _Session):
        """Crop ``box`` out of the full-resolution image via its normalised rect."""
        pw = max(1, session.pixmap.width())
        ph = max(1, session.pixmap.height())
        rect = box.rect
        w_img, h_img = full.size
        left = int(rect.x() / pw * w_img)
        top = int(rect.y() / ph * h_img)
        right = int((rect.x() + rect.width()) / pw * w_img)
        bottom = int((rect.y() + rect.height()) / ph * h_img)
        left = max(0, min(left, w_img - 1))
        top = max(0, min(top, h_img - 1))
        right = max(left + 1, min(right, w_img))
        bottom = max(top + 1, min(bottom, h_img))
        return full.crop((left, top, right, bottom))

    def _decorate(self, suggestions) -> list[tuple[CardLabel, float]]:
        """Turn ranked (card_id, score) pairs into (CardLabel, score) for display."""
        catalog = CardCatalogRepository(self._conn)
        out: list[tuple[CardLabel, float]] = []
        for s in suggestions:
            card = catalog.get(s.card_id)
            if card is None:
                label = CardLabel(card_id=s.card_id)
            else:
                label = CardLabel(
                    card_id=card.card_id,
                    name=card.name,
                    set_name=card.set_name,
                    card_number=catalog.display_number(card) or card.number,
                    language=getattr(card, "language", None),
                )
            out.append((label, s.score))
        return out

    def _run_suggestions(self) -> None:
        from PIL import Image

        if not (0 <= self._current < len(self._sessions)):
            return
        self._sync_scene_to_model()
        session = self._sessions[self._current]
        if not session.boxes:
            QMessageBox.information(
                self, "No boxes", "Draw a box around a card first."
            )
            return
        try:
            recognizer = self._ensure_recognizer()
        except Exception as exc:  # embedder/model failed to load
            QMessageBox.warning(self, "Suggestions unavailable", str(exc))
            return
        pool = self._candidate_pool()
        if not pool:
            QMessageBox.information(
                self,
                "No labeled cards yet",
                "Suggestions rank against cards you've already labeled. Label a "
                "few cards and save — the matcher improves as your corpus grows.",
            )
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with Image.open(session.path) as raw:
                full = raw.convert("RGB")
                for box in session.boxes:
                    combo = self._suggest_boxes.get(box.number)
                    if combo is None:
                        continue
                    crop = self._crop_for(full, box, session)
                    ranked = recognizer.suggest(crop, pool, top=5)
                    combo.set_suggestions(self._decorate(ranked))
        finally:
            QApplication.restoreOverrideCursor()

    # -- save ---------------------------------------------------------------

    def _save(self) -> None:
        self._sync_scene_to_model()
        sessions = [s for s in self._sessions if s.boxes]
        if not sessions:
            QMessageBox.information(
                self, "Nothing to save", "Draw at least one box around a card."
            )
            return
        repo = self._vision_repo or VisionRepository(open_vision())
        source = "transaction" if self._is_transaction() else "listing"
        capture_id = repo.add_capture(source=source)
        for session in sessions:
            dest, sha = vision_svc.store_file(session.path, self._vision_dir)
            pixmap = session.pixmap
            image_id = repo.add_image(
                capture_id,
                sha,
                str(dest),
                width=pixmap.width(),
                height=pixmap.height(),
            )
            pw = max(1, pixmap.width())
            ph = max(1, pixmap.height())
            for box in session.boxes:
                rect = box.rect
                norm = (
                    rect.x() / pw,
                    rect.y() / ph,
                    rect.width() / pw,
                    rect.height() / ph,
                )
                repo.add_region(
                    image_id, box.number, norm, self._side_for(box, session), box.card
                )
        self._clear()
        QMessageBox.information(
            self, "Saved", f"Saved {len(sessions)} image(s) of annotations."
        )

    def _clear(self) -> None:
        self._sessions.clear()
        self._current = -1
        self._items.clear()
        self._name_fields.clear()
        self._suggest_boxes.clear()
        self._proposals.clear()
        self._divider = None
        self._strip.clear()
        self._scene.clear()
        self._panel.setRowCount(0)
        self._accept_btn.setEnabled(False)
        self._view.setVisible(False)
        self._empty.setVisible(True)

    def refresh(self) -> None:  # nav hook; nothing to reload
        pass
