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

import os
import sqlite3
import tempfile
from dataclasses import dataclass, field

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QKeySequence,
    QPen,
    QPixmap,
    QShortcut,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCompleter,
    QFileDialog,
    QFrame,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.data.vision import CardLabel, VisionRepository, open_vision
from tcginventory.domain.enums import Condition
from tcginventory.services import vision as vision_svc
from tcginventory.ui import icons, theme

_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
_MODES = ["Transaction (I Give / They Give)", "Listing (one lot)"]
_BOX_PEN = QColor(theme.DARK.accent)  # confirmed box — the theme accent
_BOX_FILL = QColor(theme.DARK.accent)
_BOX_FILL.setAlpha(40)
_PROPOSAL_PEN = QColor(theme.DARK.info)  # classic-CV proposals (info blue)
_YOLO_PEN = QColor(theme.DARK.accent2)  # trained-detector proposals (brick red)
# Cosine below this gets a ⚠ in the suggestion list (advisory; frozen DINOv2 on
# photo-vs-catalog-render is a weak baseline until the M-V2 fine-tune).
_LOW_CONFIDENCE = 0.6


@dataclass
class _Box:
    """A drawn box on an image: geometry (pixel coords) + its card label."""

    number: int
    rect: QRectF
    card: CardLabel = field(default_factory=CardLabel)
    condition: str | None = None  # set in card-backs mode (Condition value)


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

    def __init__(self, conn, box: _Box, meta_label: QLabel | None = None) -> None:
        super().__init__()
        self._conn = conn
        self._box = box
        self._meta_label = meta_label
        self.setPlaceholderText("Card name…")
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
        self._refresh_meta()

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
        self._refresh_meta()

    def _on_free_text(self) -> None:
        text = self.text().strip()
        # Only overwrite when the text no longer matches the picked card's name.
        if text and text != (self._box.card.name or ""):
            self._box.card = CardLabel(name=text)
            self._refresh_meta()

    def apply_label(self, card: CardLabel) -> None:
        """Adopt a card identity picked elsewhere (e.g. a matcher suggestion)."""
        self._box.card = card
        self.setText(card.name or "")
        self._refresh_meta()

    def _refresh_meta(self) -> None:
        if self._meta_label is None:
            return
        card = self._box.card
        parts: list[str] = []
        if card.set_name:
            parts.append(card.set_name)
        if card.card_number:
            parts.append(f"#{card.card_number}")
        self._meta_label.setText("  ·  ".join(parts) if parts else "— not labeled —")


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


class _ConditionBox(QComboBox):
    """Per-box condition picker (card-backs mode); writes to the box."""

    def __init__(self, box: _Box) -> None:
        super().__init__()
        self._box = box
        self.addItem("—", None)
        for cond in Condition:
            if cond is Condition.SEALED:
                continue  # not a wear grade
            self.addItem(cond.value, cond.value)
        if box.condition:
            pos = self.findData(box.condition)
            if pos >= 0:
                self.setCurrentIndex(pos)
        self.currentIndexChanged.connect(self._on_changed)

    def _on_changed(self, *_args) -> None:
        self._box.condition = self.currentData()

    def set_condition(self, value: str | None) -> None:
        pos = self.findData(value)
        if pos >= 0:
            self.setCurrentIndex(pos)


class RecognitionView(QWidget):
    def __init__(
        self,
        conn: sqlite3.Connection,
        vision_repo=None,
        vision_dir=None,
        recognizer=None,
        prewarm: bool = False,
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
        # detector overlays, per proposer source ("cv" | "yolo")
        self._proposals: dict[str, list[QGraphicsRectItem]] = {"cv": [], "yolo": []}
        self._yolo_detector = None  # OnnxCardDetector, lazily built
        self._condition_boxes: dict[int, _ConditionBox] = {}
        self._assessor = None  # condition assessor, lazily built
        self._divider = None  # QGraphicsLineItem for the current image
        self._rotation = 0  # view rotation in degrees (landscape / portrait)
        self._strip_counts: dict[int, QLabel] = {}  # row -> count chip label
        self._strip_thumbs: list[QFrame] = []

        t = theme.active()
        self.setStyleSheet(_view_qss(t))

        # Mode is held by a hidden combo (the tested state holder) and mirrored by
        # a segmented pill in the header.
        self._mode = QComboBox()
        self._mode.addItems(_MODES)
        self._mode.setVisible(False)
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        self._mode.currentIndexChanged.connect(self._sync_mode_toggle)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)
        root.addWidget(self._build_header())
        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_body(), 1)

        self._empty.setVisible(True)
        self._view.setVisible(False)
        self._zone_bar.setVisible(False)

        # Ctrl+V pastes a copied image (or image file) as a new scene.
        paste_sc = QShortcut(QKeySequence.Paste, self)
        paste_sc.activated.connect(self._paste_image)

        # Warm the (heavy) identity model on a background thread so the first
        # Suggest / Transaction-autofill doesn't freeze on the model load. Uses a
        # process-wide shared embedder, so both windows benefit. Off by default,
        # and skipped under pytest, so headless tests don't load the model.
        if prewarm and "PYTEST_CURRENT_TEST" not in os.environ:
            import threading

            from tcginventory.services import recognizer as _recognizer

            threading.Thread(
                target=_recognizer.prewarm, name="tcgi-vision-prewarm", daemon=True
            ).start()

    # -- header -------------------------------------------------------------

    def _build_header(self) -> QWidget:
        t = theme.active()
        head = QWidget()
        row = QHBoxLayout(head)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Recognition")
        title.setObjectName("recogTitle")
        sub = QLabel(
            "Draw a box around each card, label it from the catalog, and save the "
            "regions your detector trains on."
        )
        sub.setObjectName("recogSub")
        titles.addWidget(title)
        titles.addWidget(sub)
        row.addLayout(titles)
        row.addStretch(1)

        # segmented mode pill
        self._mode_btns: list[QPushButton] = []
        toggle = QFrame()
        toggle.setObjectName("segToggle")
        th = QHBoxLayout(toggle)
        th.setContentsMargins(3, 3, 3, 3)
        th.setSpacing(3)
        labels = ["Transaction · I Give / They Give", "Listing · one lot"]
        for i, label in enumerate(labels):
            b = QPushButton(label)
            b.setObjectName("segItem")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, idx=i: self._mode.setCurrentIndex(idx))
            th.addWidget(b)
            self._mode_btns.append(b)
        self._mode_btns[0].setChecked(True)
        row.addWidget(toggle)

        save_btn = QPushButton("Save annotations")
        save_btn.setObjectName("primary")
        save_btn.setIcon(icons.icon("save", t.accent_on, 15))
        save_btn.clicked.connect(self._save)
        row.addWidget(save_btn)
        return head

    def _sync_mode_toggle(self, index: int) -> None:
        for i, b in enumerate(self._mode_btns):
            b.setChecked(i == index)

    # -- toolbar ------------------------------------------------------------

    def _tool_button(self, text: str, icon_name: str | None = None) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("tool")
        if icon_name:
            b.setIcon(icons.icon(icon_name, theme.active().ink2, 15))
        return b

    def _build_toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("recogToolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(12)

        # -- Boxes cluster
        add_btn = self._tool_button("Add images", "plus")
        add_btn.clicked.connect(self._add_images)
        self._draw_btn = self._tool_button("Draw", "scan-line")
        self._draw_btn.setCheckable(True)
        self._draw_btn.toggled.connect(self._set_draw)
        del_btn = self._tool_button("Delete", "trash-2")
        del_btn.clicked.connect(self._delete_selected)
        row.addLayout(self._cluster("Boxes", [add_btn, self._draw_btn, del_btn]))
        row.addWidget(_vrule())

        # -- Assist cluster (matcher + auto box proposals)
        self._suggest_btn = self._tool_button("Suggest labels", "sparkles")
        self._suggest_btn.setToolTip(
            "Rank catalog matches for each box against the cards you've already "
            "labeled (identity matcher)"
        )
        self._suggest_btn.clicked.connect(self._run_suggestions)
        self._cv_toggle = self._tool_button("Classic-CV")
        self._cv_toggle.setCheckable(True)
        self._cv_toggle.setToolTip(
            "Auto-detect card boxes on a blank background (no training). Accept the "
            "ones you want into numbered boxes."
        )
        self._cv_toggle.toggled.connect(self._on_cv_toggle)
        self._yolo_toggle = self._tool_button("YOLO")
        self._yolo_toggle.setCheckable(True)
        from tcginventory.services.detector import yolo_model_path

        has_yolo = yolo_model_path() is not None
        self._yolo_toggle.setEnabled(has_yolo)
        self._yolo_toggle.setToolTip(
            "Trained card detector (stronger on clutter/overlap)"
            if has_yolo
            else "Trained card detector — available once you train a model on your "
            "labeled boxes (training/train_detector.py)"
        )
        self._yolo_toggle.toggled.connect(self._on_yolo_toggle)
        self._accept_btn = self._tool_button("Accept proposals")
        self._accept_btn.setToolTip(
            "Turn the detector's proposed boxes (selected, or all) into numbered "
            "boxes you can label"
        )
        self._accept_btn.clicked.connect(self._accept_proposals)
        self._accept_btn.setEnabled(False)
        row.addLayout(
            self._cluster(
                "Assist",
                [
                    self._suggest_btn,
                    self._cv_toggle,
                    self._yolo_toggle,
                    self._accept_btn,
                ],
            )
        )
        row.addWidget(_vrule())

        # -- Card-backs cluster
        self._backs_cb = self._tool_button("Grade backs")
        self._backs_cb.setCheckable(True)
        self._backs_cb.setToolTip(
            "Annotating card backs: saved images are tagged 'back' and each box "
            "can be graded for condition."
        )
        self._backs_cb.toggled.connect(self._on_backs_toggled)
        self._assess_btn = self._tool_button("Assess condition")
        self._assess_btn.setToolTip(
            "Estimate each box's condition from edge/corner whitening on the back"
        )
        self._assess_btn.clicked.connect(self._assess_conditions)
        self._assess_btn.setEnabled(False)
        row.addLayout(self._cluster("Card backs", [self._backs_cb, self._assess_btn]))
        row.addStretch(1)
        return bar

    def _cluster(self, caption: str, widgets: list[QWidget]) -> QHBoxLayout:
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(7)
        cap = QLabel(caption.upper())
        cap.setObjectName("clusterCap")
        lay.addWidget(cap)
        for w in widgets:
            lay.addWidget(w)
        return lay

    # -- body (filmstrip / stage / segments) --------------------------------

    def _build_body(self) -> QWidget:
        split = QSplitter(Qt.Horizontal)
        split.setObjectName("recogSplit")
        split.setChildrenCollapsible(False)
        split.addWidget(self._build_filmstrip())
        split.addWidget(self._build_stage())
        split.addWidget(self._build_segments())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([196, 900, 360])
        return split

    def _build_filmstrip(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("filmFrame")
        frame.setMinimumWidth(178)
        v = QVBoxLayout(frame)
        v.setContentsMargins(11, 11, 11, 11)
        v.setSpacing(10)
        self._film_cap = QLabel("IMAGES")
        self._film_cap.setObjectName("clusterCap")
        v.addWidget(self._film_cap)

        drop = QPushButton("  Add images")
        drop.setObjectName("dropTile")
        drop.setIcon(icons.icon("image", theme.active().ink3, 18))
        drop.clicked.connect(self._add_images)
        v.addWidget(drop)

        paste = QPushButton("Paste from clipboard")
        paste.setObjectName("pasteBtn")
        paste.setIcon(icons.icon("clipboard-paste", theme.active().ink3, 14))
        paste.setToolTip("Paste a copied image or image file  (Ctrl+V)")
        paste.setCursor(Qt.PointingHandCursor)
        paste.clicked.connect(self._paste_image)
        v.addWidget(paste)

        self._strip = QListWidget()
        self._strip.setObjectName("strip")
        self._strip.setFrameShape(QFrame.NoFrame)
        self._strip.setSpacing(6)
        self._strip.setSelectionMode(QAbstractItemView.SingleSelection)
        self._strip.currentRowChanged.connect(self._switch_image)
        v.addWidget(self._strip, 1)
        return frame

    def _build_stage(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("stageFrame")
        v = QVBoxLayout(frame)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # zone headers (transaction mode)
        self._zone_bar = QFrame()
        self._zone_bar.setObjectName("zoneBar")
        zb = QHBoxLayout(self._zone_bar)
        zb.setContentsMargins(16, 9, 16, 9)
        give = QLabel("●  I GIVE")
        give.setObjectName("zoneGive")
        recv = QLabel("THEY GIVE  ●")
        recv.setObjectName("zoneRecv")
        recv.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        zb.addWidget(give)
        zb.addStretch(1)
        zb.addWidget(recv)
        v.addWidget(self._zone_bar)

        # canvas
        self._scene = _Scene(self._add_box)
        self._view = QGraphicsView(self._scene)
        self._view.setObjectName("stageCanvas")
        self._view.setFrameShape(QFrame.NoFrame)
        v.addWidget(self._view, 1)

        self._empty = QLabel(
            "Add images, then draw a box around each card.\n\n"
            "Boxes become labeled regions your detector learns from."
        )
        self._empty.setObjectName("emptyBox")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        v.addWidget(self._empty, 1)

        # floating stage tools (parented to the canvas, top-left overlay)
        self._stage_tools = QFrame(self._view)
        self._stage_tools.setObjectName("stageTools")
        st = QHBoxLayout(self._stage_tools)
        st.setContentsMargins(5, 5, 5, 5)
        st.setSpacing(5)
        self._stage_draw = self._stage_tool("scan-line", "Draw box", checkable=True)
        self._stage_draw.toggled.connect(self._set_draw)
        stage_sel = self._stage_tool("mouse-pointer-2", "Select / move")
        stage_sel.clicked.connect(lambda: self._set_draw(False))
        stage_del = self._stage_tool("trash-2", "Delete box")
        stage_del.clicked.connect(self._delete_selected)
        stage_rot = self._stage_tool("rotate-cw", "Rotate view (landscape / portrait)")
        stage_rot.clicked.connect(self._rotate_view)
        stage_fit = self._stage_tool("maximize", "Fit image")
        stage_fit.clicked.connect(self._fit_view)
        for b in (self._stage_draw, stage_sel, stage_del, stage_rot, stage_fit):
            st.addWidget(b)
        self._stage_tools.move(12, 12)
        self._stage_tools.raise_()
        return frame

    def _stage_tool(self, icon_name, tip, checkable=False) -> QPushButton:
        b = QPushButton()
        b.setObjectName("stageTool")
        b.setIcon(icons.icon(icon_name, theme.active().ink2, 17))
        b.setIconSize(QSize(17, 17))
        b.setFixedSize(30, 30)
        b.setToolTip(tip)
        b.setCheckable(checkable)
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _build_segments(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("segPanel")
        frame.setMinimumWidth(320)
        v = QVBoxLayout(frame)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QFrame()
        header.setObjectName("segHeader")
        hh = QHBoxLayout(header)
        hh.setContentsMargins(13, 11, 10, 11)
        self._seg_count = QLabel("Segments")
        self._seg_count.setObjectName("segTitle")
        hh.addWidget(self._seg_count)
        hh.addStretch(1)
        suggest_all = QPushButton("Suggest all")
        suggest_all.setObjectName("linkBtn")
        suggest_all.setIcon(icons.icon("sparkles", theme.active().accent_ink, 14))
        suggest_all.clicked.connect(self._run_suggestions)
        hh.addWidget(suggest_all)
        v.addWidget(header)

        scroll = QScrollArea()
        scroll.setObjectName("segScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        holder.setObjectName("segHolder")
        self._seg_list = QVBoxLayout(holder)
        self._seg_list.setContentsMargins(9, 9, 9, 9)
        self._seg_list.setSpacing(8)
        self._seg_list.addStretch(1)
        scroll.setWidget(holder)
        v.addWidget(scroll, 1)
        return frame

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

    _PASTE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")

    def _paste_image(self) -> None:
        """Add scene image(s) from the clipboard — either copied image files or a
        raw bitmap (screenshot / copied image), which is spooled to a temp PNG so
        the rest of the pipeline (crop / save / suggest) can read it from disk."""
        clip = QGuiApplication.clipboard()
        mime = clip.mimeData()

        paths: list[str] = []
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(
                    self._PASTE_EXTS
                ):
                    paths.append(url.toLocalFile())
        if paths:
            self.add_image_paths(paths)
            return

        image = clip.image()
        if not image.isNull():
            self.add_image_paths([self._spool_pasted_image(image)])
            return

        QMessageBox.information(
            self,
            "Paste image",
            "The clipboard doesn't contain an image. Copy a card photo (or an "
            "image file) and try again.",
        )

    def _spool_pasted_image(self, image) -> str:
        """Write a pasted QImage to a temp PNG and return its path."""
        fd, path = tempfile.mkstemp(prefix="tcgi-paste-", suffix=".png")
        os.close(fd)
        QPixmap.fromImage(image).save(path, "PNG")
        return path

    def add_image_paths(self, paths) -> None:
        """Load images into the session (also the test hook)."""
        for path in paths:
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                continue
            self._sessions.append(_Session(path=str(path), pixmap=pixmap))
            name = str(path).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            self._add_strip_item(pixmap, name)
        if self._sessions and self._current < 0:
            self._strip.setCurrentRow(0)
        self._film_cap.setText(f"IMAGES · {len(self._sessions)}")

    def _add_strip_item(self, pixmap: QPixmap, name: str) -> None:
        row = self._strip.count()
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 118))
        thumb, count = self._make_thumb(pixmap, name)
        self._strip.addItem(item)
        self._strip.setItemWidget(item, thumb)
        self._strip_thumbs.append(thumb)
        self._strip_counts[row] = count

    def _make_thumb(self, pixmap: QPixmap, name: str) -> tuple[QFrame, QLabel]:
        frame = QFrame()
        frame.setObjectName("thumb")
        frame.setProperty("sel", False)
        v = QVBoxLayout(frame)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        pic = QLabel()
        pic.setObjectName("thumbPic")
        pic.setAlignment(Qt.AlignCenter)
        pic.setFixedHeight(84)
        scaled = pixmap.scaled(
            QSize(150, 82), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        pic.setPixmap(scaled)
        v.addWidget(pic)
        meta = QFrame()
        meta.setObjectName("thumbMeta")
        mh = QHBoxLayout(meta)
        mh.setContentsMargins(8, 5, 7, 6)
        mh.setSpacing(6)
        fn = QLabel(name)
        fn.setObjectName("thumbFn")
        fn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        count = QLabel("0")
        count.setObjectName("thumbCnt")
        mh.addWidget(fn, 1)
        mh.addWidget(count)
        v.addWidget(meta)
        return frame, count

    def _switch_image(self, row: int) -> None:
        if 0 <= self._current < len(self._sessions):
            self._sync_scene_to_model()
        if 0 <= row < len(self._sessions):
            self._load_session(row)
        for i, thumb in enumerate(self._strip_thumbs):
            thumb.setProperty("sel", i == row)
            thumb.style().unpolish(thumb)
            thumb.style().polish(thumb)

    def _load_session(self, index: int) -> None:
        self._current = index
        session = self._sessions[index]
        self._empty.setVisible(False)
        self._view.setVisible(True)
        self._zone_bar.setVisible(self._is_transaction())
        self._scene.clear()
        self._items.clear()
        for items in self._proposals.values():
            items.clear()  # scene.clear() already dropped the items
        self._divider = None
        self._scene.addPixmap(session.pixmap)
        self._scene.setSceneRect(QRectF(session.pixmap.rect()))
        for box in session.boxes:
            self._add_box_item(box)
        if self._is_transaction():
            self._add_divider(session)
        self._fit_view()
        self._refresh_panel()
        for source, toggle in (("cv", self._cv_toggle), ("yolo", self._yolo_toggle)):
            if toggle.isChecked():
                self._show_proposals(source)

    def _fit_view(self) -> None:
        """Fit the scene, honouring the current landscape/portrait rotation."""
        if not (0 <= self._current < len(self._sessions)):
            return
        self._view.resetTransform()
        if self._rotation:
            self._view.rotate(self._rotation)
        self._view.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def _rotate_view(self) -> None:
        """Rotate the canvas 90° clockwise — flips between landscape and portrait."""
        self._rotation = (self._rotation + 90) % 360
        self._fit_view()

    def _add_divider(self, session: _Session) -> None:
        t = theme.active()
        w = session.pixmap.width()
        h = session.pixmap.height()
        if session.divider_x is None:
            session.divider_x = w / 2
        pen = QPen(QColor(t.info), 0, Qt.DashLine)
        line = self._scene.addLine(session.divider_x, 0, session.divider_x, h, pen)
        line.setFlag(line.GraphicsItemFlag.ItemIsMovable, True)
        line.setZValue(2)
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
        t = theme.active()
        item = QGraphicsRectItem(box.rect)
        item.setPen(QPen(_BOX_PEN, 0))
        item.setBrush(QBrush(_BOX_FILL))
        item.setFlag(item.GraphicsItemFlag.ItemIsMovable, True)
        item.setFlag(item.GraphicsItemFlag.ItemIsSelectable, True)
        item.setData(0, box.number)
        # A constant-screen-size numbered badge pinned to the box's top-left.
        badge = QGraphicsEllipseItem(-11, -11, 22, 22, item)
        badge.setBrush(QBrush(QColor(t.accent)))
        badge.setPen(QPen(Qt.NoPen))
        badge.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        badge.setPos(box.rect.topLeft())
        num = QGraphicsSimpleTextItem(str(box.number), badge)
        num.setBrush(QBrush(QColor(t.accent_on)))
        font = num.font()
        font.setBold(True)
        num.setFont(font)
        br = num.boundingRect()
        num.setPos(-br.width() / 2, -br.height() / 2)
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

    def _set_draw(self, on: bool) -> None:
        """Toggle draw mode, keeping the toolbar + stage draw buttons in sync."""
        self._scene.set_draw(on)
        for b in (self._draw_btn, getattr(self, "_stage_draw", None)):
            if b is None:
                continue
            b.blockSignals(True)
            b.setChecked(on)
            b.blockSignals(False)

    # -- detector box proposals (classic-CV + trained YOLO) -----------------

    def _on_cv_toggle(self, on: bool) -> None:
        self._toggle_proposals("cv", on)

    def _on_yolo_toggle(self, on: bool) -> None:
        self._toggle_proposals("yolo", on)

    def _toggle_proposals(self, source: str, on: bool) -> None:
        if on:
            self._show_proposals(source)
        else:
            self._clear_proposals(source)

    def _detect(self, source: str, path: str):
        """Run a proposer; returns normalised boxes, or [] on any failure."""
        from tcginventory.services import detector as det

        try:
            if source == "cv":
                return det.propose_card_boxes(path)
            if self._yolo_detector is None:
                model = det.yolo_model_path()
                if model is None:
                    return []
                self._yolo_detector = det.OnnxCardDetector(model)
            return self._yolo_detector.detect(path)
        except Exception:  # a bad/incompatible model shouldn't break the window
            return []

    def _show_proposals(self, source: str) -> None:
        """Overlay a proposer's boxes for the current image."""
        self._clear_proposals(source)
        if not (0 <= self._current < len(self._sessions)):
            return
        session = self._sessions[self._current]
        pw = max(1, session.pixmap.width())
        ph = max(1, session.pixmap.height())
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            norm_boxes = self._detect(source, session.path)
        finally:
            QApplication.restoreOverrideCursor()
        colour = _PROPOSAL_PEN if source == "cv" else _YOLO_PEN
        pen = QPen(colour, 0, Qt.DashLine)
        brush = QBrush(QColor(colour.red(), colour.green(), colour.blue(), 30))
        for nx, ny, nw, nh in norm_boxes:
            rect = QRectF(nx * pw, ny * ph, nw * pw, nh * ph)
            item = self._scene.addRect(rect, pen, brush)
            item.setFlag(item.GraphicsItemFlag.ItemIsSelectable, True)
            self._proposals[source].append(item)
        self._update_accept_enabled()

    def _all_proposal_items(self) -> list[QGraphicsRectItem]:
        return [item for items in self._proposals.values() for item in items]

    def _clear_proposals(self, source: str | None = None) -> None:
        sources = [source] if source else list(self._proposals)
        for s in sources:
            for item in self._proposals[s]:
                self._scene.removeItem(item)
            self._proposals[s].clear()
        self._update_accept_enabled()

    def _update_accept_enabled(self) -> None:
        self._accept_btn.setEnabled(bool(self._all_proposal_items()))

    def _accept_proposals(self) -> None:
        """Promote proposals (selected, or all if none selected) into real boxes."""
        items = self._all_proposal_items()
        if not items:
            return
        selected = [it for it in items if it.isSelected()]
        targets = selected or items
        for lst in self._proposals.values():
            for item in list(lst):
                if item in targets:
                    self._add_box(item.rect())
                    self._scene.removeItem(item)
                    lst.remove(item)
        self._update_accept_enabled()

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
        self._clear_seg_list()
        self._name_fields.clear()
        self._suggest_boxes.clear()
        self._condition_boxes.clear()
        if not (0 <= self._current < len(self._sessions)):
            self._seg_count.setText("Segments")
            self._seg_list.addStretch(1)
            return
        session = self._sessions[self._current]
        backs = self._backs_cb.isChecked()
        for box in session.boxes:
            self._seg_list.addWidget(self._make_seg_row(box, session, backs))
        self._seg_list.addStretch(1)
        n = len(session.boxes)
        self._seg_count.setText(f"Segments · {n} card{'' if n == 1 else 's'}")
        self._update_strip_count()

    def _clear_seg_list(self) -> None:
        while self._seg_list.count():
            item = self._seg_list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _make_seg_row(self, box: _Box, session: _Session, backs: bool) -> QFrame:
        side = self._side_for(box, session)
        row = QFrame()
        row.setObjectName("segRow")
        v = QVBoxLayout(row)
        v.setContentsMargins(10, 9, 10, 9)
        v.setSpacing(7)

        # top line: badge + name + side chip
        r1 = QHBoxLayout()
        r1.setSpacing(9)
        badge = QLabel(str(box.number))
        badge.setObjectName("segBadgeRecv" if side == "receive" else "segBadge")
        badge.setFixedSize(22, 22)
        badge.setAlignment(Qt.AlignCenter)

        meta = QLabel()
        meta.setObjectName("segMeta")
        name = _NameField(self._conn, box, meta_label=meta)
        name.setObjectName("segName")
        self._name_fields[box.number] = name

        r1.addWidget(badge)
        r1.addWidget(name, 1)
        if side is not None:
            chip = QLabel("I Give" if side == "give" else "They Give")
            chip.setObjectName("sideGive" if side == "give" else "sideRecv")
            chip.setAlignment(Qt.AlignCenter)
            r1.addWidget(chip)
        v.addLayout(r1)

        # bottom line: set·number meta + suggestion (+ condition in backs mode)
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        r2.setContentsMargins(31, 0, 0, 0)
        r2.addWidget(meta, 1)
        suggest = _SuggestBox(box, name)
        suggest.setObjectName("segSuggest")
        self._suggest_boxes[box.number] = suggest
        r2.addWidget(suggest)
        cond = _ConditionBox(box)
        cond.setObjectName("segCond")
        cond.setVisible(backs)
        self._condition_boxes[box.number] = cond
        r2.addWidget(cond)
        v.addLayout(r2)
        return row

    def _update_strip_count(self) -> None:
        if not (0 <= self._current < len(self._sessions)):
            return
        label = self._strip_counts.get(self._current)
        if label is not None:
            label.setText(str(len(self._sessions[self._current].boxes)))

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
            QMessageBox.information(self, "No boxes", "Draw a box around a card first.")
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

    # -- condition assessment (card backs, M-V3) ----------------------------

    def _on_backs_toggled(self, on: bool) -> None:
        self._assess_btn.setEnabled(on)
        self._refresh_panel()

    def _ensure_assessor(self):
        if self._assessor is None:
            from tcginventory.services.condition import build_condition_assessor

            self._assessor = build_condition_assessor()
        return self._assessor

    def _assess_conditions(self) -> None:
        """Estimate each box's condition from its back crop and fill the column."""
        from PIL import Image

        from tcginventory.services import condition as cond

        if not (0 <= self._current < len(self._sessions)):
            return
        self._sync_scene_to_model()
        session = self._sessions[self._current]
        if not session.boxes:
            QMessageBox.information(self, "No boxes", "Draw a box around a card first.")
            return
        assessor = self._ensure_assessor()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with Image.open(session.path) as raw:
                full = raw.convert("RGB")
                for box in session.boxes:
                    crop = self._crop_for(full, box, session)
                    est = cond.assess_condition(crop, assessor)
                    box.condition = est.condition.value
                    combo = self._condition_boxes.get(box.number)
                    if combo is not None:
                        combo.set_condition(box.condition)
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
        side = "back" if self._backs_cb.isChecked() else None
        capture_id = repo.add_capture(source=source)
        entries = []  # (region_id, card_id, image_path, box) for labeled regions
        for session in sessions:
            dest, sha = vision_svc.store_file(session.path, self._vision_dir)
            pixmap = session.pixmap
            image_id = repo.add_image(
                capture_id,
                sha,
                str(dest),
                side=side,
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
                if box.condition:
                    box.card.condition = box.condition
                region_id = repo.add_region(
                    image_id, box.number, norm, self._side_for(box, session), box.card
                )
                if box.card and box.card.card_id:
                    entries.append((region_id, box.card.card_id, str(dest), norm))
        indexed = self._index_exemplars(repo, entries)
        self._clear()
        msg = f"Saved {len(sessions)} image(s) of annotations."
        if indexed:
            msg += f"\nIndexed {indexed} card crop(s) — the matcher just got smarter."
        QMessageBox.information(self, "Saved", msg)

    def _index_exemplars(self, repo, entries) -> int:
        """Embed the just-saved labeled crops into the exemplar store (best-effort
        — a matcher/model failure must never block saving the annotations)."""
        if not entries:
            return 0
        try:
            from tcginventory.services import exemplars

            embedder = self._ensure_recognizer().embedder
        except Exception:
            return 0
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            return exemplars.index_regions(repo, embedder, entries)
        except Exception:
            return 0
        finally:
            QApplication.restoreOverrideCursor()

    def _clear(self) -> None:
        self._sessions.clear()
        self._current = -1
        self._items.clear()
        self._name_fields.clear()
        self._suggest_boxes.clear()
        self._condition_boxes.clear()
        for items in self._proposals.values():
            items.clear()
        self._divider = None
        self._strip.clear()
        self._strip_thumbs.clear()
        self._strip_counts.clear()
        self._scene.clear()
        self._clear_seg_list()
        self._seg_list.addStretch(1)
        self._seg_count.setText("Segments")
        self._film_cap.setText("IMAGES")
        self._accept_btn.setEnabled(False)
        self._view.setVisible(False)
        self._zone_bar.setVisible(False)
        self._empty.setVisible(True)

    def refresh(self) -> None:  # nav hook; nothing to reload
        pass


def _vrule() -> QFrame:
    f = QFrame()
    f.setObjectName("vrule")
    f.setFixedWidth(1)
    f.setFrameShape(QFrame.NoFrame)
    return f


def _view_qss(t: theme.Tokens) -> str:
    return f"""
    #recogTitle {{ font-size: 20px; font-weight: 700; color: {t.ink}; }}
    #recogSub {{ color: {t.ink3}; font-size: 12px; }}

    #segToggle {{ background: {t.surface2}; border: 1px solid {t.line2};
        border-radius: 9px; }}
    #segItem {{ background: transparent; border: none; border-radius: 6px;
        padding: 6px 13px; color: {t.ink2}; font-size: 12px; font-weight: 600; }}
    #segItem:hover {{ color: {t.ink}; }}
    #segItem:checked {{ background: {t.accent_soft}; color: {t.accent_ink}; }}

    #recogToolbar, #filmFrame, #stageFrame, #segPanel {{
        background: {t.surface}; border: 1px solid {t.line}; border-radius: 12px; }}
    #clusterCap {{ color: {t.ink3}; font-size: 10px; font-weight: 700;
        letter-spacing: 1px; }}
    #vrule {{ background: {t.line2}; }}

    QPushButton#tool {{ background: {t.surface2}; color: {t.ink};
        border: 1px solid {t.line2}; border-radius: 8px; padding: 6px 12px;
        font-size: 12px; }}
    QPushButton#tool:hover {{ border-color: {t.accent}; }}
    QPushButton#tool:checked {{ background: {t.accent_soft}; border-color: {t.accent};
        color: {t.accent_ink}; }}
    QPushButton#tool:disabled {{ color: {t.ink3}; border-color: {t.line}; }}

    #dropTile {{ background: transparent; border: 1.5px dashed {t.line2};
        border-radius: 10px; padding: 13px; color: {t.ink3};
        text-align: center; }}
    #dropTile:hover {{ border-color: {t.accent}; color: {t.accent_ink}; }}
    QPushButton#pasteBtn {{ background: transparent; border: none;
        color: {t.ink3}; font-size: 11px; padding: 3px; }}
    QPushButton#pasteBtn:hover {{ color: {t.accent_ink}; }}

    #strip {{ background: transparent; border: none; }}
    #strip::item {{ border: none; }}
    #strip::item:selected {{ background: transparent; }}
    #thumb {{ background: {t.surface2}; border: 1px solid {t.line2};
        border-radius: 9px; }}
    #thumb[sel="true"] {{ border: 1px solid {t.accent}; }}
    #thumbPic {{ background: {t.zebra}; border-top-left-radius: 8px;
        border-top-right-radius: 8px; }}
    #thumbMeta {{ background: transparent; border: none; }}
    #thumbFn {{ color: {t.ink2}; font-family: "{theme.mono_family()}";
        font-size: 10px; }}
    #thumbCnt {{ background: {t.accent_soft}; color: {t.accent_ink};
        font-size: 9px; font-weight: 700; border-radius: 8px; padding: 1px 6px; }}

    #zoneBar {{ background: {t.surface}; border: none;
        border-bottom: 1px solid {t.line};
        border-top-left-radius: 12px; border-top-right-radius: 12px; }}
    #zoneGive {{ color: {t.pos}; font-size: 10px; font-weight: 700;
        letter-spacing: 1px; }}
    #zoneRecv {{ color: {t.accent}; font-size: 10px; font-weight: 700;
        letter-spacing: 1px; }}
    #stageCanvas {{ background: {t.bg}; border: none;
        border-bottom-left-radius: 12px; border-bottom-right-radius: 12px; }}
    #emptyBox {{ color: {t.ink3}; font-size: 13px; }}
    #stageTools {{ background: {t.surface}; border: 1px solid {t.line2};
        border-radius: 9px; }}
    QPushButton#stageTool {{ background: transparent; border: none;
        border-radius: 6px; }}
    QPushButton#stageTool:hover {{ background: {t.surface2}; }}
    QPushButton#stageTool:checked {{ background: {t.accent_soft}; }}

    #segHeader {{ background: transparent; border: none;
        border-bottom: 1px solid {t.line}; }}
    #segTitle {{ font-size: 13px; font-weight: 700; color: {t.ink}; }}
    QPushButton#linkBtn {{ background: transparent; border: none; color: {t.ink3};
        padding: 5px 6px; font-size: 12px; }}
    QPushButton#linkBtn:hover {{ color: {t.accent_ink}; }}
    #segScroll, #segHolder {{ background: transparent; border: none; }}
    #segRow {{ background: {t.surface2}; border: 1px solid {t.line};
        border-radius: 10px; }}
    #segBadge {{ background: {t.accent}; color: {t.accent_on};
        border-radius: 11px; font-weight: 800; font-size: 11px; }}
    #segBadgeRecv {{ background: {t.accent2}; color: {t.accent_on};
        border-radius: 11px; font-weight: 800; font-size: 11px; }}
    QLineEdit#segName {{ background: {t.surface}; border: 1px solid {t.line2};
        border-radius: 7px; padding: 6px 9px; color: {t.ink};
        font-size: 12px; font-weight: 600; }}
    QLineEdit#segName:focus {{ border-color: {t.accent}; }}
    #segMeta {{ color: {t.ink3}; font-family: "{theme.mono_family()}";
        font-size: 11px; }}
    #sideGive {{ background: {t.accent_soft}; color: {t.pos};
        border-radius: 10px; padding: 3px 9px; font-size: 10px; font-weight: 700; }}
    #sideRecv {{ background: {t.accent_soft}; color: {t.accent};
        border-radius: 10px; padding: 3px 9px; font-size: 10px; font-weight: 700; }}
    QComboBox#segSuggest, QComboBox#segCond {{ background: {t.surface};
        border: 1px solid {t.line2}; border-radius: 7px; padding: 4px 7px;
        font-size: 11px; }}
    """
