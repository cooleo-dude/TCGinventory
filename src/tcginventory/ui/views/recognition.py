"""Recognition window — the human-in-the-loop training surface (spec 06, M-V1b).

Load image scenes of cards on a blank background, draw numbered boxes around each
card, label them via the catalog, and save the result as localized **regions**
(box geometry + identity + side) in ``vision.db``. In *Transaction* mode an
adjustable divider splits the scene into **I Give** / **They Give** (a box's side
follows the zone its centre sits in); *Listing* mode is one lot. Multiple images
per session share one card set.

Model-agnostic: it collects the training data the detector/identifier will learn
from. Trained-detector box proposals toggle in here.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field, replace

from PySide6.QtCore import QModelIndex, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
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
    QFrame,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
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
from tcginventory.ui.card_preview import CardImagePane
from tcginventory.ui.clipboard_images import clipboard_image_paths
from tcginventory.ui.completer_popup import show_fitted

_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
_MODES = ["Transaction (I Give / They Give)", "Listing (one lot)"]
_BOX_PEN = QColor(theme.DARK.accent)  # confirmed box — the theme accent
_BOX_FILL = QColor(theme.DARK.accent)
_BOX_FILL.setAlpha(40)
_YOLO_PEN = QColor(theme.DARK.accent2)  # trained-detector proposals (brick red)
# Cosine below this gets a ⚠ in the suggestion list (advisory; frozen DINOv2 on
# photo-vs-catalog-render is a weak baseline until the M-V2 fine-tune).
_LOW_CONFIDENCE = 0.6


@dataclass
class RecognizedCard:
    """A labeled card handed off to the Transaction / Auction windows on export.

    Consumed duck-typed by those windows' ``import_recognized`` methods, so this
    stays a plain data carrier (no cross-view import coupling)."""

    card_id: str | None
    name: str
    set_name: str | None
    card_number: str | None
    language: str | None
    quantity: int
    side: str | None  # "give" | "receive" (transaction) | None (listing)
    condition: str | None
    is_graded: bool
    grade: str | None
    grading_company: str | None


@dataclass
class _Box:
    """A drawn box on an image: geometry (pixel coords) + its card label."""

    number: int
    rect: QRectF
    card: CardLabel = field(default_factory=CardLabel)
    condition: str | None = Condition.NM.value  # default Near Mint (Condition value)
    visible: bool = True  # box overlay shown on the canvas
    # Which catalog this card is ranked against. Per box, because one photo can
    # hold English and Japanese cards side by side. None means "all languages".
    language: str | None = "EN"


@dataclass
class _Session:
    """One image in the annotation session: its pixmap, boxes and split line."""

    path: str
    pixmap: QPixmap
    boxes: list[_Box] = field(default_factory=list)
    # Transaction split: position (px) along the divider axis; None = mid. The
    # axis follows the image's orientation — a landscape scene splits left/right
    # (a vertical line), a portrait scene splits top/bottom (a horizontal one).
    divider_pos: float | None = None
    orientation: str = "landscape"  # "landscape" | "portrait" (from aspect)
    # Front/back pairing (Include card backs): side is "front"/"back"/None, pair_id
    # links a front to its back, and awaiting marks a back slot with no image yet.
    side: str | None = None
    pair_id: int | None = None
    awaiting: bool = False


_STAGE_EMPTY = (
    "Upload or paste a photo of your cards.\n\n"
    "Then draw a box around each one — the boxes become the labeled regions your "
    "detector learns from."
)
_BACKS_EMPTY = (
    "Add the card-backs photo.\n\n"
    "Upload or paste a photo of these cards' backs — the front's boxes are "
    "already placed, so you'll just align each one."
)
_NEW_IMAGE_EMPTY = (
    "Add an image to this slot.\n\n"
    "Upload or paste a photo of your cards, then draw a box around each one."
)


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


class _ResizeHandle(QGraphicsRectItem):
    """A small constant-size grip at a box corner; dragging it resizes the box."""

    _CURSORS = {
        "tl": Qt.SizeFDiagCursor,
        "br": Qt.SizeFDiagCursor,
        "tr": Qt.SizeBDiagCursor,
        "bl": Qt.SizeBDiagCursor,
    }

    def __init__(self, box_item: _BoxItem, corner: str, color: str) -> None:
        super().__init__(-5, -5, 10, 10, box_item)
        self._box = box_item
        self._corner = corner
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setBrush(QBrush(QColor("#ffffff")))
        pen = QPen(QColor(color))
        pen.setWidthF(1.5)
        self.setPen(pen)
        self.setZValue(3)
        self.setCursor(self._CURSORS[corner])
        self.setVisible(False)

    def mousePressEvent(self, event) -> None:
        # Grab the drag so it resizes here instead of moving the parent box.
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        self._box.resize_from_handle(self._corner, event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        event.accept()


class _BoxItem(QGraphicsRectItem):
    """A drawn card box: reports hover for cross-highlight, carries a numbered
    badge, and grows resize grips at its corners while selected."""

    _MIN = 8.0  # smallest box side (scene px)

    def __init__(self, rect: QRectF, number: int, on_hover) -> None:
        super().__init__(rect)
        self._number = number
        self._on_hover = on_hover  # (number, bool) -> None
        self._badge: QGraphicsEllipseItem | None = None
        self._handles: dict[str, _ResizeHandle] = {}
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)

    def build_decorations(self, accent: str, accent_on: str) -> None:
        """Attach the numbered badge and the (hidden) corner resize grips."""
        badge = QGraphicsEllipseItem(-11, -11, 22, 22, self)
        badge.setBrush(QBrush(QColor(accent)))
        badge.setPen(QPen(Qt.NoPen))
        badge.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        num = QGraphicsSimpleTextItem(str(self._number), badge)
        num.setBrush(QBrush(QColor(accent_on)))
        font = num.font()
        font.setBold(True)
        num.setFont(font)
        br = num.boundingRect()
        num.setPos(-br.width() / 2, -br.height() / 2)
        self._badge = badge
        for corner in ("tl", "tr", "bl", "br"):
            self._handles[corner] = _ResizeHandle(self, corner, accent)
        self._position_decorations()

    def _position_decorations(self) -> None:
        r = self.rect()
        if self._badge is not None:
            self._badge.setPos(r.topLeft())
        corners = {
            "tl": r.topLeft(),
            "tr": r.topRight(),
            "bl": r.bottomLeft(),
            "br": r.bottomRight(),
        }
        for corner, handle in self._handles.items():
            handle.setPos(corners[corner])

    def resize_from_handle(self, corner: str, scene_pos: QPointF) -> None:
        """Move ``corner`` toward the cursor, keeping the box at least _MIN wide."""
        local = self.mapFromScene(scene_pos)
        r = QRectF(self.rect())
        lx, ly = local.x(), local.y()
        if "l" in corner:
            lx = min(lx, r.right() - self._MIN)
        if "r" in corner:
            lx = max(lx, r.left() + self._MIN)
        if "t" in corner:
            ly = min(ly, r.bottom() - self._MIN)
        if "b" in corner:
            ly = max(ly, r.top() + self._MIN)
        point = QPointF(lx, ly)
        if corner == "tl":
            r.setTopLeft(point)
        elif corner == "tr":
            r.setTopRight(point)
        elif corner == "bl":
            r.setBottomLeft(point)
        else:
            r.setBottomRight(point)
        self.setRect(r)
        self._position_decorations()

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedChange:
            for handle in self._handles.values():
                handle.setVisible(bool(value))
        return super().itemChange(change, value)

    def hoverEnterEvent(self, event) -> None:
        self._on_hover(self._number, True)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._on_hover(self._number, False)
        super().hoverLeaveEvent(event)


class _DividerLine(QGraphicsLineItem):
    """The transaction split, draggable but locked to one axis (horizontal drag
    for a vertical landscape split, vertical drag for a horizontal portrait one)."""

    def __init__(self, x1, y1, x2, y2, axis: str) -> None:
        super().__init__(x1, y1, x2, y2)
        self._axis = axis  # "x" = vertical line slides horizontally; "y" = the reverse
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange:
            if self._axis == "x":
                return QPointF(value.x(), 0.0)  # lock vertical drift
            return QPointF(0.0, value.y())  # lock horizontal drift
        return super().itemChange(change, value)


class _ElidedLabel(QLabel):
    """A single-line label that elides instead of widening its row.

    A segment's "set · #number" line is as long as the set name, and a plain
    QLabel reports that whole string as its minimum width. That minimum
    propagated out through the row, the holder, the scroll area and the splitter
    until the Recognition view itself could not be made narrower — the panel ran
    off the screen. Reporting an ignored width instead anchors the panel: the
    layout decides how much room this line gets, and the full text stays
    available as the tooltip.
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__("", parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self.setText(text)  # so text set at construction elides too

    def setText(self, text: str) -> None:  # noqa: N802 (Qt override)
        self._full = text or ""
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(
            self._full, Qt.ElideRight, max(0, self.width())
        )
        if shown != super().text():  # guard against a relayout loop
            super().setText(shown)
        # A tooltip only where it adds something: when the text is cut short.
        self.setToolTip(self._full if shown != self._full else "")


class _SegRow(QFrame):
    """A segment card row that reports hover enter/leave for cross-highlighting."""

    def __init__(self, number: int, on_hover) -> None:
        super().__init__()
        self._number = number
        self._on_hover = on_hover  # (number, bool) -> None

    def enterEvent(self, event) -> None:
        self._on_hover(self._number, True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._on_hover(self._number, False)
        super().leaveEvent(event)


class _NameField(QLineEdit):
    """Catalog-autocompleting name field that records the picked card's identity."""

    _CARD_ID_ROLE = Qt.UserRole + 1

    def __init__(
        self,
        conn,
        box: _Box,
        meta_label: QLabel | None = None,
        on_change=None,
        on_preview=None,
    ) -> None:
        super().__init__()
        self._conn = conn
        self._box = box
        self._meta_label = meta_label
        self._on_change = on_change  # (box) -> None, after the card identity changes
        self._on_preview = on_preview  # (card_id) -> None, to preview art live
        self.setPlaceholderText("Card name…")
        self.setText(box.card.name or "")
        self._matches: dict[str, object] = {}
        self._picked = False  # last interaction was a completer pick (not free text)
        self._model = QStandardItemModel(self)
        self._completer = QCompleter(self._model, self)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.setCompleter(self._completer)
        self.textEdited.connect(self._on_edited)
        self._completer.activated[QModelIndex].connect(self._pick)
        # Preview each candidate's art as you arrow through the dropdown…
        self._completer.highlighted[QModelIndex].connect(self._on_highlighted)
        # …and as you *hover* one with the mouse. QCompleter only emits
        # `highlighted` when the popup's selection changes (keyboard nav), so
        # mouse tracking + `entered` is what makes hovering preview as well —
        # the same wiring the shared card-entry rows use (_card_entry.py).
        popup = self._completer.popup()
        popup.setMouseTracking(True)
        popup.entered.connect(self._on_highlighted)
        self.editingFinished.connect(self._on_free_text)
        self._refresh_meta()

    def _on_highlighted(self, index: QModelIndex) -> None:
        if self._on_preview is None:
            return
        card_id = index.data(self._CARD_ID_ROLE)
        if not card_id:
            # The signal's index lives in the completion (proxy) model; map it
            # back to our source model to read the custom card-id role.
            try:
                src = self._completer.completionModel().mapToSource(index)
                card_id = self._model.data(src, self._CARD_ID_ROLE)
            except (RuntimeError, AttributeError):
                card_id = None
        if card_id:
            self._on_preview(card_id)

    def _on_edited(self, text: str) -> None:
        self._picked = False  # the user is typing — subsequent text is free text
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
            show_fitted(self._completer, self)
            # Preview the top hit immediately; hovering the dropdown refines it.
            if self._on_preview is not None:
                self._on_preview(matches[0].card_id)

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
        self._picked = True
        self._refresh_meta()
        self._notify()

    def _on_free_text(self) -> None:
        # editingFinished right after a completer pick: the field may hold the
        # display string ("Name · Set #Number"). That's not free text — keep the
        # structured card (id/set/number) and restore its clean name.
        if self._picked:
            self._picked = False
            self.setText(self._box.card.name or "")
            return
        text = self.text().strip()
        if text and text != (self._box.card.name or ""):
            self._box.card = CardLabel(name=text)
            self._refresh_meta()
            self._notify()

    def apply_label(self, card: CardLabel) -> None:
        """Adopt a card identity picked elsewhere (e.g. a matcher suggestion)."""
        self._box.card = card
        self.setText(card.name or "")
        self._refresh_meta()
        self._notify()

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change(self._box)

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
        # A combo sizes itself to its longest item by default, and these items
        # carry "name · set (94%)" — enough to push the whole panel off screen.
        # Bound it on both sides: never wider than the cap, never narrower than a
        # usable control (an ignored size policy let the layout collapse it to
        # nothing). The popup is widened separately, so the full ranked labels
        # stay readable where they actually matter.
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(12)
        self.setMinimumWidth(120)
        self.setMaximumWidth(190)
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
        self._widen_popup()

    def _widen_popup(self) -> None:
        """Let the drop-down be as wide as its longest guess, even though the
        closed combo stays narrow — the ranked labels are the whole point."""
        metrics = self.fontMetrics()
        widest = max(
            (metrics.horizontalAdvance(self.itemText(i)) for i in range(self.count())),
            default=0,
        )
        self.view().setMinimumWidth(widest + 36)  # text + scrollbar and padding

    def _on_activated(self, index: int) -> None:
        if 0 <= index < len(self._labels) and self._labels[index] is not None:
            self._name_field.apply_label(self._labels[index])


class _LangBox(QComboBox):
    """Per-box catalog language, and the single largest lever on suggestion
    accuracy.

    The same artwork ships in English and in Japanese, so a card's nearest
    neighbour by picture is routinely its counterpart from the other region —
    measured on the corpus, 42 of 46 wrong suggestions were exactly that. Reading
    the language off the photograph was tried and does not work (a probe on the
    embeddings scores 99.8% on catalog renders and 51.7% on real photos), so the
    scope is stated rather than guessed.

    It sits per box, not per scan, because one photo routinely holds both.
    """

    CHOICES = (("EN", "EN"), ("JP", "JP"), ("All", None))

    def __init__(self, box: _Box, on_change=None) -> None:
        super().__init__()
        self._box = box
        self._on_change = on_change  # (box, language) -> None
        for label, value in self.CHOICES:
            self.addItem(label, value)
        pos = self.findData(box.language)
        if pos >= 0:
            self.setCurrentIndex(pos)
        self.currentIndexChanged.connect(self._on_changed)

    def _on_changed(self, *_args) -> None:
        self._box.language = self.currentData()
        if self._on_change is not None:
            self._on_change(self._box, self._box.language)

    def set_language(self, value: str | None) -> None:
        """Show ``value`` without announcing it — used when mirroring the other
        face, so the two don't bounce updates at each other."""
        pos = self.findData(value)
        if pos >= 0 and pos != self.currentIndex():
            blocked = self.blockSignals(True)
            self.setCurrentIndex(pos)
            self.blockSignals(blocked)


class _ConditionBox(QComboBox):
    """Per-box condition picker; writes the wear grade to the box so the saved
    corpus region records each card's condition.

    A card is one object with two faces, so an edit here is announced through
    ``on_change`` and mirrored onto the paired box on the other side.
    """

    def __init__(self, box: _Box, on_change=None) -> None:
        super().__init__()
        self._box = box
        self._on_change = on_change  # (box, value) -> None
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
        if self._on_change is not None:
            self._on_change(self._box, self._box.condition)

    def set_condition(self, value: str | None) -> None:
        """Show ``value`` without announcing it — used when mirroring an edit
        made on the other face, so the two don't bounce updates at each other."""
        pos = self.findData(value)
        if pos >= 0:
            self.blockSignals(True)
            try:
                self.setCurrentIndex(pos)
            finally:
                self.blockSignals(False)
            self._box.condition = value


class _PairRail(QWidget):
    """Left gutter that links a front/back pair: a full-height rail on the front
    that runs into an elbow connector on the (indented) back, both in the accent
    colour — so a pair reads as a nested group even amid many images."""

    def __init__(self, kind: str, color: str, parent=None) -> None:
        super().__init__(parent)
        self._kind = kind  # "front" | "back"
        self._color = color
        self.setFixedWidth(6 if kind == "front" else 20)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(self._color))
        pen.setWidth(3)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        x, h, w = 3, self.height(), self.width()
        if self._kind == "front":
            p.drawLine(x, 4, x, h)  # runs to the bottom, toward the back below
        else:  # back: down from the front, then elbow into the (indented) thumb
            mid = int(h / 2)
            p.drawLine(x, 0, x, mid)
            p.drawLine(x, mid, w - 2, mid)


class _CardPreviewPane(CardImagePane):
    """Card preview that fills its column width and takes a card-proportioned
    height (63:88), so the whole card shows rather than a fixed box clipping its
    lower half. It shrinks to fit on short windows without ever clipping (the
    base class always scales the art to fit)."""

    _ASPECT = 88 / 63  # card height / width

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return round(width * self._ASPECT)

    def sizeHint(self) -> QSize:
        w = max(self.width(), self.minimumWidth())
        return QSize(w, self.heightForWidth(w))


class RecognitionView(QWidget):
    # Hand the current session's labeled cards to another window; payload is a
    # list[RecognizedCard]. The main window routes + switches views.
    export_to_transaction = Signal(object)
    export_to_auction = Signal(object)

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
        self._pair_seq: int = 0  # increments per front/back pair (Include card backs)
        self._items: dict[int, QGraphicsRectItem] = {}  # number -> item (current)
        self._name_fields: dict[int, _NameField] = {}
        self._suggest_boxes: dict[int, _SuggestBox] = {}
        # detector overlays, per proposer source ("cv" | "yolo")
        self._proposals: dict[str, list[QGraphicsRectItem]] = {"yolo": []}
        self._yolo_detector = None  # OnnxCardDetector, lazily built
        self._condition_boxes: dict[int, _ConditionBox] = {}
        self._lang_boxes: dict[int, _LangBox] = {}
        # New boxes inherit the last language chosen, so a single-language
        # scan is one change rather than one per card.
        self._last_language: str | None = "EN"
        # Candidate ids per language scope, built once per scan.
        self._pool_cache: dict[object, list[str]] = {}
        self._assessor = None  # condition assessor, lazily built
        self._divider = None  # QGraphicsLineItem for the current image
        self._rotation = 0  # view rotation in degrees (landscape / portrait)
        self._strip_counts: dict[int, QLabel] = {}  # row -> count chip label
        self._strip_thumbs: list[QFrame] = []
        self._seg_rows: dict[int, _SegRow] = {}  # number -> segment row (current)
        self._vis_checks: dict[int, QCheckBox] = {}  # number -> visibility checkbox
        self._updating_checks = False  # guard master <-> per-row feedback
        self._hl: int | None = None  # cross-highlighted box/segment number

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

        self._empty_box.setVisible(True)
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
        # Elided for the same reason as the segment meta line: a fixed sentence
        # of prose was the single biggest floor under the window's width.
        sub = _ElidedLabel(
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

        self._export_btn = QPushButton()
        self._export_btn.setObjectName("tool")
        self._export_btn.setIcon(icons.icon("arrow-left-right", t.ink2, 15))
        self._export_btn.setToolTip(
            "Send the labeled cards to the Transaction or Auction window "
            "(by session mode)"
        )
        self._export_btn.clicked.connect(self._export)
        row.addWidget(self._export_btn)

        save_btn = QPushButton("Save annotations")
        save_btn.setObjectName("primary")
        save_btn.setIcon(icons.icon("save", t.accent_on, 15))
        save_btn.clicked.connect(self._save)
        row.addWidget(save_btn)
        self._sync_export_label()
        return head

    def _sync_mode_toggle(self, index: int) -> None:
        for i, b in enumerate(self._mode_btns):
            b.setChecked(i == index)
        self._sync_export_label()

    def _sync_export_label(self) -> None:
        btn = getattr(self, "_export_btn", None)
        if btn is not None:
            btn.setText(
                "Export to Transaction"
                if self._is_transaction()
                else "Export to Auction"
            )

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

        # -- Boxes cluster (images are added from the IMAGES panel's Start button)
        self._draw_btn = self._tool_button("Draw", "scan-line")
        self._draw_btn.setCheckable(True)
        self._draw_btn.toggled.connect(self._set_draw)
        del_btn = self._tool_button("Delete", "trash-2")
        del_btn.clicked.connect(self._delete_selected)
        row.addLayout(self._cluster("Boxes", [self._draw_btn, del_btn]))
        row.addWidget(_vrule())

        # -- Assist cluster (matcher + auto box proposals)
        self._suggest_btn = self._tool_button("Suggest labels", "sparkles")
        self._suggest_btn.setToolTip(
            "Rank catalog matches for each box against the cards you've already "
            "labeled (identity matcher)"
        )
        self._suggest_btn.clicked.connect(self._run_suggestions)
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
                [self._suggest_btn, self._yolo_toggle, self._accept_btn],
            )
        )
        row.addWidget(_vrule())

        # -- Card-backs cluster
        self._include_backs_btn = self._tool_button("Include card backs")
        self._include_backs_btn.setToolTip(
            "Set the current (front) photo aside and add a photo of these cards' "
            "backs. The labeled boxes copy over so you just align them; each "
            "card's condition can then be graded from its back."
        )
        self._include_backs_btn.clicked.connect(self._include_card_backs)
        self._assess_btn = self._tool_button("Assess condition")
        self._assess_btn.setToolTip(
            "Estimate each box's condition from edge/corner whitening on the back"
        )
        self._assess_btn.clicked.connect(self._assess_conditions)
        self._assess_btn.setEnabled(False)
        row.addLayout(
            self._cluster("Card backs", [self._include_backs_btn, self._assess_btn])
        )
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
        split.setSizes([196, 810, 450])  # segments pane ~1.25x wider (was 360)
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

        self._strip = QListWidget()
        self._strip.setObjectName("strip")
        self._strip.setFrameShape(QFrame.NoFrame)
        self._strip.setSpacing(6)
        self._strip.setSelectionMode(QAbstractItemView.SingleSelection)
        self._strip.currentRowChanged.connect(self._switch_image)
        v.addWidget(self._strip, 1)

        # The only way to add an image: a blank slot opens under the last one,
        # filled from its own Upload / Paste buttons.
        start = QPushButton("  Start New Image")
        start.setObjectName("dropTile")
        start.setIcon(icons.icon("image", theme.active().ink3, 16))
        start.setToolTip("Open a blank slot, then upload or paste an image into it")
        start.clicked.connect(self._add_new_image_slot)
        v.addWidget(start)
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
        self._zone_give = QLabel("●  I GIVE")
        self._zone_give.setObjectName("zoneGive")
        self._zone_recv = QLabel("THEY GIVE  ●")
        self._zone_recv.setObjectName("zoneRecv")
        self._zone_recv.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        zb.addWidget(self._zone_give)
        zb.addStretch(1)
        zb.addWidget(self._zone_recv)
        v.addWidget(self._zone_bar)

        # canvas
        self._scene = _Scene(self._add_box)
        self._view = QGraphicsView(self._scene)
        self._view.setObjectName("stageCanvas")
        self._view.setFrameShape(QFrame.NoFrame)
        v.addWidget(self._view, 1)

        self._empty = QLabel(_STAGE_EMPTY)
        self._empty.setObjectName("emptyBox")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty_upload = QPushButton("  Upload image")
        self._empty_upload.setObjectName("dropTile")
        self._empty_upload.setIcon(icons.icon("image", theme.active().ink3, 18))
        self._empty_upload.setCursor(Qt.PointingHandCursor)
        self._empty_upload.setMaximumWidth(220)
        self._empty_upload.clicked.connect(self._add_images)
        self._empty_paste = QPushButton("  Paste from clipboard")
        self._empty_paste.setObjectName("pasteBtn")
        self._empty_paste.setIcon(
            icons.icon("clipboard-paste", theme.active().ink3, 15)
        )
        self._empty_paste.setCursor(Qt.PointingHandCursor)
        self._empty_paste.setToolTip("Paste a copied image or image file  (Ctrl+V)")
        self._empty_paste.clicked.connect(self._paste_image)
        self._empty_box = QWidget()
        eb = QVBoxLayout(self._empty_box)
        eb.setAlignment(Qt.AlignCenter)
        eb.setSpacing(10)
        eb.addWidget(self._empty)
        eb.addWidget(self._empty_upload, 0, Qt.AlignHCenter)
        eb.addWidget(self._empty_paste, 0, Qt.AlignHCenter)
        v.addWidget(self._empty_box, 1)

        # floating stage tools (parented to the canvas, top-left overlay)
        self._stage_tools = QFrame(self._view)
        self._stage_tools.setObjectName("stageTools")
        st = QHBoxLayout(self._stage_tools)
        st.setContentsMargins(5, 5, 5, 5)
        st.setSpacing(5)
        self._stage_draw = self._stage_tool("scan-line", "Draw box", checkable=True)
        self._stage_draw.toggled.connect(self._set_draw)
        self._stage_sel = self._stage_tool(
            "mouse-pointer-2", "Select / move / resize", checkable=True
        )
        self._stage_sel.setChecked(True)  # default mode
        self._stage_sel.clicked.connect(lambda: self._set_draw(False))
        stage_del = self._stage_tool("trash-2", "Delete box")
        stage_del.clicked.connect(self._delete_selected)
        stage_rot = self._stage_tool("rotate-cw", "Rotate view (landscape / portrait)")
        stage_rot.clicked.connect(self._rotate_view)
        stage_fit = self._stage_tool("maximize", "Fit image")
        stage_fit.clicked.connect(self._fit_view)
        for b in (self._stage_draw, self._stage_sel, stage_del, stage_rot, stage_fit):
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
        self._show_all = QCheckBox("Show boxes")
        self._show_all.setObjectName("showAll")
        self._show_all.setChecked(True)
        self._show_all.setCursor(Qt.PointingHandCursor)
        self._show_all.setToolTip("Check / uncheck to show or hide every box")
        self._show_all.toggled.connect(self._on_show_all_toggled)
        hh.addWidget(self._show_all)
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

        # Card-art preview (bottom-right): fills as you label / hover segments.
        preview_wrap = QFrame()
        preview_wrap.setObjectName("previewWrap")
        pv = QVBoxLayout(preview_wrap)
        pv.setContentsMargins(13, 10, 13, 12)
        pv.setSpacing(7)
        preview_cap = QLabel("CARD PREVIEW")
        preview_cap.setObjectName("clusterCap")
        pv.addWidget(preview_cap)
        self._preview = _CardPreviewPane()
        # Fill the panel width at the card's 63:88 aspect (height tracks width),
        # instead of a fixed box that overran the panel and clipped the card's
        # lower half. It shrinks to fit on short windows without ever clipping.
        pv.addWidget(self._preview, 0)
        v.addWidget(preview_wrap, 0)
        return frame

    # -- mode ---------------------------------------------------------------

    def _is_transaction(self) -> bool:
        return self._mode.currentIndex() == 0

    def _on_mode_changed(self, *_args) -> None:
        if self._current >= 0:
            self._load_session(self._current)  # add/remove the divider
        self._refresh_panel()

    # -- export to Transaction / Auction ------------------------------------

    def _export(self) -> None:
        """Hand the session's labeled cards to the window the mode implies."""
        self._sync_scene_to_model()
        cards = self._collect_export_cards()
        if not cards:
            QMessageBox.information(
                self,
                "Nothing to export",
                "Label at least one card (draw a box and pick its name) first.",
            )
            return
        if self._is_transaction():
            self.export_to_transaction.emit(cards)
        else:
            self.export_to_auction.emit(cards)

    def _collect_export_cards(self) -> list[RecognizedCard]:
        """Labeled boxes across the session → RecognizedCards, aggregating exact
        duplicates (same identity + side + condition + grading) into a quantity."""
        agg: dict[tuple, RecognizedCard] = {}
        order: list[tuple] = []
        for session in self._sessions:
            if session.side == "back":
                continue  # a back is the same physical card as its front — don't
                # double-count it (front + back are one card)
            for box in session.boxes:
                label = box.card
                if not label or not (label.card_id or (label.name or "").strip()):
                    continue  # skip unlabeled boxes
                side = self._side_for(box, session)  # None in listing mode
                condition = label.condition or box.condition
                key = (
                    label.card_id or (label.name or "").casefold(),
                    side,
                    condition,
                    label.is_graded,
                    label.grade,
                    label.grading_company,
                )
                if key in agg:
                    agg[key].quantity += 1
                    continue
                agg[key] = RecognizedCard(
                    card_id=label.card_id,
                    name=label.name or "",
                    set_name=label.set_name,
                    card_number=label.card_number,
                    language=label.language,
                    quantity=1,
                    side=side,
                    condition=condition,
                    is_graded=label.is_graded,
                    grade=label.grade,
                    grading_company=label.grading_company,
                )
                order.append(key)
        return [agg[k] for k in order]

    # -- images -------------------------------------------------------------

    def _add_images(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add card scene images", "", _IMAGE_FILTER
        )
        self.add_image_paths(files)

    def _add_new_image_slot(self) -> None:
        """Append a blank image slot and switch to it; the user fills it by
        uploading or pasting into the empty stage."""
        if 0 <= self._current < len(self._sessions):
            self._sync_scene_to_model()
        self._sessions.append(_Session(path="", pixmap=QPixmap(), awaiting=True))
        target = len(self._sessions) - 1
        self._current = -1
        self._rebuild_strip()
        self._strip.setCurrentRow(target)

    def _paste_image(self) -> None:
        """Add scene image(s) from the clipboard — either copied image files or a
        raw bitmap (screenshot / copied image), which is spooled to a temp PNG so
        the rest of the pipeline (crop / save / suggest) can read it from disk."""
        paths = clipboard_image_paths()
        if paths:
            self.add_image_paths(paths)
            return

        QMessageBox.information(
            self,
            "Paste image",
            "The clipboard doesn't contain an image. Copy a card photo (or an "
            "image file) and try again.",
        )

    def add_image_paths(self, paths) -> None:
        """Load images into the session (also the test hook). If the current slot
        is an awaiting card-backs slot, the first image fills it; the rest are
        added as new front images."""
        paths = list(paths)
        target = self._current
        if paths and 0 <= self._current < len(self._sessions):
            cur = self._sessions[self._current]
            if cur.awaiting:
                pix = QPixmap(str(paths[0]))
                if not pix.isNull():
                    cur.path = str(paths[0])
                    cur.pixmap = pix
                    cur.awaiting = False
                    cur.orientation = (
                        "portrait" if pix.height() > pix.width() else "landscape"
                    )
                    paths = paths[1:]
        for path in paths:
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                continue
            orientation = (
                "portrait" if pixmap.height() > pixmap.width() else "landscape"
            )
            self._sessions.append(
                _Session(path=str(path), pixmap=pixmap, orientation=orientation)
            )
        if target < 0 and self._sessions:
            target = 0
        # Rebuild from scratch (thumbnails + the just-filled back slot), then
        # reselect — mirrors the safe reindex path used on removal.
        self._current = -1
        self._rebuild_strip()
        if 0 <= target < len(self._sessions):
            self._strip.setCurrentRow(target)

    def _add_strip_item(self, session: _Session) -> None:
        row = self._strip.count()
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 118))
        thumb, count = self._make_thumb(session)
        self._strip.addItem(item)
        self._strip.setItemWidget(item, thumb)
        self._strip_thumbs.append(thumb)
        self._strip_counts[row] = count

    def _make_thumb(self, session: _Session) -> tuple[QFrame, QLabel]:
        t = theme.active()
        base = session.path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if session.awaiting or not base:
            base = "backs photo" if session.side == "back" else "new image"
        # Front/back pairs read as a group: prefix the name with the side.
        name = f"{session.side.title()} · {base}" if session.side else base
        frame = QFrame()
        frame.setObjectName("thumb")
        frame.setProperty("sel", False)
        outer = QHBoxLayout(frame)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        if session.side in ("front", "back"):  # nest the pair with a linking rail
            outer.addWidget(_PairRail(session.side, t.accent))
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        pic = QLabel()
        pic.setObjectName("thumbPic")
        pic.setAlignment(Qt.AlignCenter)
        pic.setFixedHeight(84)
        if session.pixmap.isNull():
            pic.setText("＋")
            pic.setStyleSheet(f"color:{t.ink3}; font-size:22px;")
        else:
            pic.setPixmap(
                session.pixmap.scaled(
                    QSize(150, 82), Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
            )
        v.addWidget(pic)
        meta = QFrame()
        meta.setObjectName("thumbMeta")
        mh = QHBoxLayout(meta)
        mh.setContentsMargins(8, 5, 6, 6)
        mh.setSpacing(6)
        fn = QLabel(name)
        fn.setObjectName("thumbFn")
        fn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        count = QLabel("0")
        count.setObjectName("thumbCnt")
        remove = QPushButton()
        remove.setObjectName("thumbDel")
        remove.setIcon(icons.icon("trash-2", t.ink3, 13))
        remove.setFixedSize(20, 20)
        remove.setCursor(Qt.PointingHandCursor)
        remove.setToolTip("Remove this image from the session")
        remove.clicked.connect(lambda _=False, s=session: self._remove_image(s))
        mh.addWidget(fn, 1)
        mh.addWidget(count)
        mh.addWidget(remove)
        v.addWidget(meta)
        outer.addWidget(body, 1)
        return frame, count

    def _remove_image(self, session: _Session) -> None:
        """Drop one image (and its boxes) from the active session."""
        if session not in self._sessions:
            return
        # Persist edits to whatever image is currently shown before reindexing.
        if 0 <= self._current < len(self._sessions):
            self._sync_scene_to_model()
        cur_session = (
            self._sessions[self._current]
            if 0 <= self._current < len(self._sessions)
            else None
        )
        idx = self._sessions.index(session)
        del self._sessions[idx]
        if not self._sessions:
            self._clear()
            return
        if cur_session is session or cur_session is None:
            target = min(idx, len(self._sessions) - 1)
        else:
            target = self._sessions.index(cur_session)
        self._current = -1  # force a clean load (skip stale scene sync)
        self._rebuild_strip()
        self._strip.setCurrentRow(target)

    def _rebuild_strip(self) -> None:
        """Rebuild the filmstrip from ``self._sessions`` after a removal."""
        self._strip.blockSignals(True)
        self._strip.clear()
        self._strip_thumbs.clear()
        self._strip_counts.clear()
        for session in self._sessions:
            self._add_strip_item(session)
        self._strip.blockSignals(False)
        for i, session in enumerate(self._sessions):
            label = self._strip_counts.get(i)
            if label is not None:
                label.setText(str(len(session.boxes)))
        self._film_cap.setText(f"IMAGES · {len(self._sessions)}")

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
        # Each image opens in its own natural orientation (auto-detected); the
        # manual rotate control still works from there.
        self._rotation = 0
        if session.awaiting:  # a slot waiting for its photo (backs or a new image)
            self._empty.setText(
                _BACKS_EMPTY if session.side == "back" else _NEW_IMAGE_EMPTY
            )
            self._empty_box.setVisible(True)
            self._view.setVisible(False)
            self._zone_bar.setVisible(False)
            self._scene.clear()
            self._items.clear()
            self._assess_btn.setEnabled(False)
            self._refresh_panel()
            return
        self._empty.setText(_STAGE_EMPTY)
        self._empty_box.setVisible(False)
        self._view.setVisible(True)
        self._assess_btn.setEnabled(session.side == "back")
        self._zone_bar.setVisible(self._is_transaction())
        self._sync_zone_labels(session)
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
        self._scene.set_draw(self._draw_btn.isChecked())  # keep draw mode persistent
        self._fit_view()
        self._refresh_panel()
        if self._yolo_toggle.isChecked():
            self._show_proposals("yolo")

    def _sync_zone_labels(self, session: _Session) -> None:
        """Label the give/receive zones for the split's orientation."""
        if session.orientation == "portrait":
            self._zone_give.setText("●  I GIVE · top")
            self._zone_recv.setText("bottom · THEY GIVE  ●")
        else:
            self._zone_give.setText("●  I GIVE")
            self._zone_recv.setText("THEY GIVE  ●")

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
        pen = QPen(QColor(t.info), 0, Qt.DashLine)
        if session.orientation == "portrait":
            if session.divider_pos is None:
                session.divider_pos = h / 2
            line = _DividerLine(0, session.divider_pos, w, session.divider_pos, "y")
        else:
            if session.divider_pos is None:
                session.divider_pos = w / 2
            line = _DividerLine(session.divider_pos, 0, session.divider_pos, h, "x")
        line.setPen(pen)
        line.setZValue(2)
        self._scene.addItem(line)
        self._divider = line

    # -- boxes --------------------------------------------------------------

    def _next_number(self) -> int:
        session = self._sessions[self._current]
        return (max((b.number for b in session.boxes), default=0)) + 1

    def _add_box(self, rect: QRectF) -> _Box:
        """Add a box (from a drawn rubber-band) to the current image."""
        box = _Box(number=self._next_number(), rect=rect, language=self._last_language)
        self._sessions[self._current].boxes.append(box)
        self._add_box_item(box)
        self._refresh_panel()
        return box

    def _add_box_item(self, box: _Box) -> None:
        t = theme.active()
        item = _BoxItem(box.rect, box.number, self._on_box_hover)
        pen = QPen(QColor(_BOX_PEN))
        pen.setCosmetic(True)  # constant 2px outline regardless of zoom
        pen.setWidthF(2.0)
        item.setPen(pen)
        item.setBrush(QBrush(_BOX_FILL))
        item.setData(0, box.number)
        item.setVisible(box.visible)
        self._scene.addItem(item)  # the box (and its badge/grips) must join the scene
        item.build_decorations(t.accent, t.accent_on)
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
        """Toggle draw mode, keeping the toolbar + stage draw/select buttons in
        sync. Draw and Select/move are mutually exclusive modes."""
        self._scene.set_draw(on)
        for b in (self._draw_btn, getattr(self, "_stage_draw", None)):
            if b is None:
                continue
            b.blockSignals(True)
            b.setChecked(on)
            b.blockSignals(False)
        sel = getattr(self, "_stage_sel", None)
        if sel is not None:
            sel.blockSignals(True)
            sel.setChecked(not on)
            sel.blockSignals(False)

    # -- cross-highlight (box <-> segment) + card preview -------------------

    def _box_by_number(self, number: int) -> _Box | None:
        if not (0 <= self._current < len(self._sessions)):
            return None
        for box in self._sessions[self._current].boxes:
            if box.number == number:
                return box
        return None

    def _on_box_hover(self, number: int, on: bool) -> None:
        self._set_highlight(number if on else None)
        if on:
            box = self._box_by_number(number)
            if box is not None:
                self._preview_card(box.card.card_id)

    def _on_seg_hover(self, number: int, on: bool) -> None:
        self._set_highlight(number if on else None)
        if on:
            box = self._box_by_number(number)
            if box is not None:
                self._preview_card(box.card.card_id)

    def _set_highlight(self, number: int | None) -> None:
        if number == self._hl:
            return
        self._apply_highlight(self._hl, False)
        self._hl = number
        self._apply_highlight(number, True)

    def _apply_highlight(self, number: int | None, on: bool) -> None:
        if number is None:
            return
        t = theme.active()
        item = self._items.get(number)
        if item is not None:
            pen = QPen(QColor(t.accent_ink if on else _BOX_PEN))
            pen.setCosmetic(True)
            pen.setWidthF(3.5 if on else 2.0)
            item.setPen(pen)
            item.setZValue(1 if on else 0)
        row = self._seg_rows.get(number)
        if row is not None:
            row.setProperty("hl", on)
            row.style().unpolish(row)
            row.style().polish(row)

    def _on_card_changed(self, box: _Box) -> None:
        """A segment's card identity changed — refresh the art preview."""
        self._preview_card(box.card.card_id)

    def _preview_card(self, card_id: str | None) -> None:
        # Never touch the network from the preview during tests (it spawns a
        # fetch thread) — mirrors the prewarm guard.
        if not card_id or "PYTEST_CURRENT_TEST" in os.environ:
            return
        card = CardCatalogRepository(self._conn).get(card_id)
        if card is not None:
            self._preview.show_card(card_id, getattr(card, "image_small", None))

    # -- box visibility (per-box + mass check/uncheck) ----------------------

    def _set_box_visible(self, box: _Box, on: bool) -> None:
        box.visible = on
        item = self._items.get(box.number)
        if item is not None:
            item.setVisible(on)
        self._sync_show_all()

    def _on_show_all_toggled(self, on: bool) -> None:
        if self._updating_checks or not (0 <= self._current < len(self._sessions)):
            return
        self._updating_checks = True
        for box in self._sessions[self._current].boxes:
            box.visible = on
            item = self._items.get(box.number)
            if item is not None:
                item.setVisible(on)
            check = self._vis_checks.get(box.number)
            if check is not None:
                check.setChecked(on)
        self._updating_checks = False

    def _sync_show_all(self) -> None:
        """Reflect the per-box states in the master checkbox (checked = all on)."""
        if self._updating_checks or not (0 <= self._current < len(self._sessions)):
            return
        boxes = self._sessions[self._current].boxes
        all_on = all(b.visible for b in boxes) if boxes else True
        self._updating_checks = True
        self._show_all.setChecked(all_on)
        self._updating_checks = False

    # -- detector box proposals (trained YOLO) ------------------------------

    def _on_yolo_toggle(self, on: bool) -> None:
        self._toggle_proposals("yolo", on)

    def _toggle_proposals(self, source: str, on: bool) -> None:
        if on:
            self._show_proposals(source)
        else:
            self._clear_proposals(source)

    def _detect(self, source: str, path: str):
        """Run the trained detector; returns normalised boxes, or [] on any
        failure (including no model installed)."""
        from tcginventory.services import detector as det

        try:
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
        colour = _YOLO_PEN
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
                # Map the pure rect (not sceneBoundingRect, which is inflated by
                # the pen width and the ignore-transform badge child).
                box.rect = item.mapRectToScene(item.rect())
        if self._divider is not None:
            line = self._divider
            if session.orientation == "portrait":
                session.divider_pos = line.line().y1() + line.y()
            else:
                session.divider_pos = line.line().x1() + line.x()

    # -- segments panel -----------------------------------------------------

    def _refresh_panel(self) -> None:
        self._clear_seg_list()
        self._name_fields.clear()
        self._suggest_boxes.clear()
        self._condition_boxes.clear()
        self._lang_boxes.clear()
        self._seg_rows.clear()
        self._vis_checks.clear()
        self._hl = None
        if not (0 <= self._current < len(self._sessions)):
            self._seg_count.setText("Segments")
            self._seg_list.addStretch(1)
            return
        session = self._sessions[self._current]
        for box in session.boxes:
            self._seg_list.addWidget(self._make_seg_row(box, session))
        self._seg_list.addStretch(1)
        n = len(session.boxes)
        self._seg_count.setText(f"Segments · {n} card{'' if n == 1 else 's'}")
        self._sync_show_all()
        self._update_strip_count()

    def _clear_seg_list(self) -> None:
        while self._seg_list.count():
            item = self._seg_list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _make_seg_row(self, box: _Box, session: _Session) -> QFrame:
        side = self._side_for(box, session)
        row = _SegRow(box.number, self._on_seg_hover)
        row.setObjectName("segRow")
        self._seg_rows[box.number] = row
        v = QVBoxLayout(row)
        v.setContentsMargins(10, 9, 10, 9)
        v.setSpacing(7)

        # top line: visibility check + badge + name + side chip
        r1 = QHBoxLayout()
        r1.setSpacing(9)
        vis = QCheckBox()
        vis.setObjectName("visCheck")
        vis.setChecked(box.visible)
        vis.setCursor(Qt.PointingHandCursor)
        vis.setToolTip("Show / hide this box")
        vis.toggled.connect(lambda on, b=box: self._set_box_visible(b, on))
        self._vis_checks[box.number] = vis
        badge = QLabel(str(box.number))
        badge.setObjectName("segBadgeRecv" if side == "receive" else "segBadge")
        badge.setFixedSize(22, 22)
        badge.setAlignment(Qt.AlignCenter)

        meta = _ElidedLabel()
        meta.setObjectName("segMeta")
        name = _NameField(
            self._conn,
            box,
            meta_label=meta,
            on_change=self._on_card_changed,
            on_preview=self._preview_card,
        )
        name.setObjectName("segName")
        self._name_fields[box.number] = name

        r1.addWidget(vis)
        r1.addWidget(badge)
        r1.addWidget(name, 1)
        if side is not None:
            chip = QLabel("I Give" if side == "give" else "They Give")
            chip.setObjectName("sideGive" if side == "give" else "sideRecv")
            chip.setAlignment(Qt.AlignCenter)
            r1.addWidget(chip)
        v.addLayout(r1)

        # bottom line: set·number meta + suggestion + condition picker
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        r2.setContentsMargins(31, 0, 0, 0)
        r2.addWidget(meta, 1)
        lang = _LangBox(box, on_change=self._on_language_edited)
        lang.setObjectName("segLang")
        lang.setToolTip(
            "Which catalog to match this card against.\n"
            "English and Japanese releases share artwork, so narrowing this is "
            "the biggest single improvement to the suggestions."
        )
        self._lang_boxes[box.number] = lang
        r2.addWidget(lang)
        suggest = _SuggestBox(box, name)
        suggest.setObjectName("segSuggest")
        self._suggest_boxes[box.number] = suggest
        r2.addWidget(suggest)
        # Condition picker on every card so the corpus records its grade — not
        # just on card backs. "Assess condition" still auto-fills it on backs.
        cond = _ConditionBox(box, on_change=self._on_condition_edited)
        cond.setObjectName("segCond")
        cond.setToolTip("Condition / wear grade for this card (saved to the corpus)")
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
        centre = box.rect.center()
        if session.orientation == "portrait":
            divider = session.divider_pos
            if divider is None:
                divider = session.pixmap.height() / 2
            return "give" if centre.y() < divider else "receive"
        divider = session.divider_pos
        if divider is None:
            divider = session.pixmap.width() / 2
        return "give" if centre.x() < divider else "receive"

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

    def _labeled_ids(self) -> set[str]:
        """Cards already taught to the matcher: the saved-region corpus plus
        anything labeled in the current (unsaved) session."""
        ids: set[str] = set()
        if self._rec_repo is not None:
            ids.update(self._rec_repo.labeled_card_ids())
        for session in self._sessions:
            for box in session.boxes:
                if box.card and box.card.card_id:
                    ids.add(box.card.card_id)
        return ids

    def _candidate_pool(self, language: str | None, model_version: str) -> list[str]:
        """Cards to rank a box against: the indexed catalog, scoped to a language.

        Suggestions used to run against the labeled corpus alone, which meant the
        matcher could only ever propose a card already identified once before.
        With the catalog indexed it ranks against the real thing; the labeled
        cards stay in regardless of scope, since those are known-relevant and
        carry crop exemplars photographed under the same conditions as the query.
        """
        from tcginventory.services import catalog_index as ci

        labeled = self._labeled_ids()
        key = (language, model_version, len(labeled))
        cached = self._pool_cache.get(key)
        if cached is not None:
            return cached
        # `_rec_repo` is only bound once a recognizer has been built, so fall back
        # to the repo the view was handed — the pool is readable either way.
        repo = self._rec_repo or self._vision_repo
        pool: list[str]
        if repo is None:
            pool = sorted(labeled)
        else:
            pool = ci.candidate_pool(
                self._conn,
                repo,
                model_version,
                languages=[language] if language else None,
                extra=labeled,
            )
        self._pool_cache[key] = pool
        return pool

    def _on_language_edited(self, box: _Box, value: str | None) -> None:
        """Remember the choice for new boxes, and mirror it onto the other face.

        One physical card has one language, so setting it from either side means
        the same thing — the same rule the condition picker follows.
        """
        self._last_language = value
        if not (0 <= self._current < len(self._sessions)):
            return
        other = self._paired_session(self._sessions[self._current])
        if other is None:
            return
        for candidate in other.boxes:
            if candidate.number == box.number:
                if candidate.language != value:
                    candidate.language = value
                    if self._sessions[self._current] is other:
                        combo = self._lang_boxes.get(candidate.number)
                        if combo is not None:
                            combo.set_language(value)
                break

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
        # Each box carries its own language scope, so the pool is resolved per
        # box rather than once for the scan — one photo can hold both catalogs.
        self._pool_cache.clear()
        version = recognizer.model_version
        if not any(
            self._candidate_pool(box.language, version) for box in session.boxes
        ):
            QMessageBox.information(
                self,
                "Nothing to match against",
                "Suggestions rank against the indexed catalog. Index it from "
                "Operations → Catalog index, or label a few cards and save.",
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
                    pool = self._candidate_pool(box.language, version)
                    if not pool:
                        continue
                    crop = self._crop_for(full, box, session)
                    ranked = recognizer.suggest(crop, pool, top=5)
                    combo.set_suggestions(self._decorate(ranked))
        finally:
            QApplication.restoreOverrideCursor()

    # -- condition assessment (card backs, M-V3) ----------------------------

    def _include_card_backs(self) -> None:
        """Set the current (front) photo aside and open a slot for a backs photo,
        copying the labeled boxes onto it so they only need aligning."""
        if not (0 <= self._current < len(self._sessions)):
            return
        self._sync_scene_to_model()
        front = self._sessions[self._current]
        if front.awaiting:
            QMessageBox.information(self, "Card backs", "Add this backs photo first.")
            return
        if front.side is not None:
            QMessageBox.information(
                self, "Card backs", "This photo is already part of a front/back pair."
            )
            return
        if not front.boxes:
            QMessageBox.information(
                self,
                "Card backs",
                "Label the cards on the front first — the boxes copy onto the back.",
            )
            return
        self._pair_seq += 1
        front.side = "front"
        front.pair_id = self._pair_seq
        back = _Session(
            path="",
            pixmap=QPixmap(),
            orientation=front.orientation,
            side="back",
            pair_id=self._pair_seq,
            awaiting=True,
        )
        for b in front.boxes:  # copy labeled boxes onto the back to align
            back.boxes.append(
                _Box(
                    number=b.number,
                    rect=QRectF(b.rect),
                    card=replace(b.card),
                    condition=b.condition,  # same physical card → same condition
                    language=b.language,  # ...and the same catalog language
                )
            )
        insert_at = self._current + 1
        self._sessions.insert(insert_at, back)
        self._current = -1
        self._rebuild_strip()
        self._strip.setCurrentRow(insert_at)

    def _paired_session(self, session: _Session) -> _Session | None:
        """The other face of ``session``'s card set, if one has been added."""
        if session.pair_id is None:
            return None
        for other in self._sessions:
            if other is not session and other.pair_id == session.pair_id:
                return other
        return None

    def _on_condition_edited(self, box: _Box, value: str | None) -> None:
        """Mirror a condition onto the same card's other face.

        One physical card has one condition, so grading it from either side means
        the same thing. Whichever face was edited last wins, and the paired box is
        updated in place — including its picker when that face is on screen.
        """
        if not (0 <= self._current < len(self._sessions)):
            return
        other = self._paired_session(self._sessions[self._current])
        if other is None:
            return
        for candidate in other.boxes:
            if candidate.number == box.number:
                if candidate.condition != value:
                    candidate.condition = value
                    self._sync_condition_widget(other, candidate, value)
                break

    def _sync_condition_widget(
        self, session: _Session, box: _Box, value: str | None
    ) -> None:
        """Refresh the paired picker when its face happens to be displayed."""
        if 0 <= self._current < len(self._sessions):
            if self._sessions[self._current] is session:
                combo = self._condition_boxes.get(box.number)
                if combo is not None:
                    combo.set_condition(value)

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
                    self._on_condition_edited(box, box.condition)
                    combo = self._condition_boxes.get(box.number)
                    if combo is not None:
                        combo.set_condition(box.condition)
        finally:
            QApplication.restoreOverrideCursor()

    # -- save ---------------------------------------------------------------

    def _save(self) -> None:
        self._sync_scene_to_model()
        sessions = [s for s in self._sessions if s.boxes and not s.awaiting]
        if not sessions:
            QMessageBox.information(
                self, "Nothing to save", "Draw at least one box around a card."
            )
            return
        repo = self._vision_repo or VisionRepository(open_vision())
        source = "transaction" if self._is_transaction() else "listing"
        capture_id = repo.add_capture(source=source)
        entries = []  # (region_id, card_id, image_path, box) for labeled regions
        for session in sessions:
            dest, sha = vision_svc.store_file(session.path, self._vision_dir)
            pixmap = session.pixmap
            image_id = repo.add_image(
                capture_id,
                sha,
                str(dest),
                side=session.side,  # 'front' | 'back' | None
                pair_group=session.pair_id,  # links a front to its back
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
                # Backs carry identity + condition, not a give/receive zone.
                region_side = (
                    None if session.side == "back" else self._side_for(box, session)
                )
                region_id = repo.add_region(
                    image_id, box.number, norm, region_side, box.card
                )
                if box.card and box.card.card_id:
                    entries.append((region_id, box.card.card_id, str(dest), norm))
        indexed = self._index_exemplars(repo, entries)
        self._clear()
        msg = f"Saved {len(sessions)} image(s) of annotations."
        if indexed:
            msg += f"\nIndexed {indexed} card crop(s) — the matcher just got smarter."
        self._saved_notice(msg)

    def _saved_notice(self, message: str) -> None:
        """Themed 'annotations saved' confirmation. A bare QMessageBox ignores
        the app stylesheet and renders in the OS default look, so build a small
        themed dialog (green success rail + accent Done) like the rest of the UI."""
        from PySide6.QtWidgets import QDialog

        t = theme.active()
        dialog = QDialog(self)
        dialog.setWindowTitle("Annotations saved")

        band = QFrame()
        band.setStyleSheet(
            f"QFrame{{background:{t.surface2}; border:1px solid {t.line};"
            f" border-left:3px solid {t.pos}; border-radius:10px;}}"
        )
        bv = QVBoxLayout(band)
        bv.setContentsMargins(14, 11, 14, 11)
        bv.setSpacing(4)
        title = QLabel("Annotations saved")
        title.setStyleSheet(
            f"color:{t.ink}; font-size:15px; font-weight:700;"
            " border:none; background:transparent;"
        )
        body = QLabel(message)
        body.setWordWrap(True)
        body.setStyleSheet(
            f"color:{t.ink2}; font-size:12.5px; border:none; background:transparent;"
        )
        bv.addWidget(title)
        bv.addWidget(body)

        ok = QPushButton("Done")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(dialog.accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(ok)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(13)
        layout.addWidget(band)
        layout.addLayout(buttons)
        dialog.setMinimumWidth(360)
        dialog.exec()

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
        self._pair_seq = 0
        self._empty.setText(_STAGE_EMPTY)
        self._empty_box.setVisible(True)
        self._view.setVisible(False)
        self._items.clear()
        self._name_fields.clear()
        self._suggest_boxes.clear()
        self._condition_boxes.clear()
        self._lang_boxes.clear()
        self._seg_rows.clear()
        self._hl = None
        for items in self._proposals.values():
            items.clear()
        self._divider = None
        self._strip.blockSignals(True)
        self._strip.clear()
        self._strip.blockSignals(False)
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
        self._empty_box.setVisible(True)

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
    QPushButton#thumbDel {{ background: transparent; border: none;
        border-radius: 5px; }}
    QPushButton#thumbDel:hover {{ background: {t.neg_soft}; }}

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
    QCheckBox#showAll {{ color: {t.ink3}; font-size: 11px; spacing: 5px; }}
    QCheckBox#showAll:hover {{ color: {t.ink}; }}
    QCheckBox::indicator {{ width: 15px; height: 15px; border-radius: 4px;
        border: 1.5px solid {t.line2}; background: {t.surface2}; }}
    QCheckBox::indicator:hover {{ border-color: {t.accent}; }}
    QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent};
        image: url(none); }}
    #segScroll, #segHolder {{ background: transparent; border: none; }}
    #segRow {{ background: {t.surface2}; border: 1px solid {t.line};
        border-radius: 10px; }}
    #segRow[hl="true"] {{ border-color: {t.accent}; background: {t.accent_soft}; }}
    #previewWrap {{ background: {t.surface}; border: none;
        border-top: 1px solid {t.line}; }}
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
    QComboBox#segSuggest, QComboBox#segCond, QComboBox#segLang {{
        background: {t.surface};
        border: 1px solid {t.line2}; border-radius: 7px; padding: 4px 7px;
        font-size: 11px; }}
    """
