"""Review dialog for image inference autofill (spec 06, Part 2).

Shows the detected scene — the photo with numbered accent badges — beside one
**identity card per detection**: the matcher's guess, its confidence, set/number
and qty. The user confirms identities, edits or clears wrong ones, unchecks false
detections, then the entry window turns the accepted rows into card rows.
Low-confidence or unidentified boxes are flagged for review.

Styled from the Ember Duo tokens with the Recognition window's vocabulary — a
framed scene canvas, ``surface2`` row cards with a number badge and a confidence
pill, and hover cross-highlighting between a row and its box — so it reads as
part of the app rather than a bare Qt dialog.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPen,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QCompleter,
    QDialog,
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
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.ui import icons, theme
from tcginventory.ui.completer_popup import show_fitted

_CARD_ID_ROLE = Qt.UserRole + 1
_PASTE_PREFIX = "tcgi-paste-"  # spooled clipboard image (see ui/clipboard_images)


def _tint(color: str, alpha: float) -> str:
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


def _source_label(image_path: str) -> str:
    """What to call the scene in the caption — a pasted bitmap has no real name."""
    name = Path(str(image_path)).name
    return "Pasted image" if name.startswith(_PASTE_PREFIX) else name


def _micro(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("afMicro")
    return label


def _cap_bar(key: str, value: str, mono: bool = True) -> QWidget:
    """A panel caption strip: uppercase key left, muted value right (mono when the
    value is data — a file name — plain when it's a hint)."""
    bar = QFrame()
    bar.setObjectName("afCap")
    h = QHBoxLayout(bar)
    h.setContentsMargins(13, 9, 13, 9)
    h.setSpacing(8)
    k = QLabel(key)
    k.setObjectName("afCapKey")
    h.addWidget(k)
    h.addStretch(1)
    v = QLabel(value)
    v.setObjectName("afCapVal" if mono else "afCapHint")
    h.addWidget(v)
    return bar


@dataclass
class AcceptedCard:
    card_id: str | None  # kept only when the user accepts the guess unedited
    name: str
    set_name: str | None
    card_number: str | None
    quantity: int
    review: bool  # low-confidence / unidentified / edited → double-check
    box: tuple[float, float, float, float]


class _SceneView(QGraphicsView):
    """The scene canvas — keeps the whole photo fitted however the dialog resizes."""

    def __init__(self, scene: QGraphicsScene) -> None:
        super().__init__(scene)
        self.setObjectName("afCanvas")
        self.setFrameShape(QFrame.NoFrame)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setAlignment(Qt.AlignCenter)

    def _fit(self) -> None:
        scene = self.scene()
        if scene is not None and not scene.sceneRect().isEmpty():
            self.fitInView(scene.sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().resizeEvent(event)
        self._fit()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().showEvent(event)
        self._fit()


class _HoverCard(QFrame):
    """A row card that reports hover so its box can light up in the scene."""

    def __init__(self, number: int, on_hover) -> None:
        super().__init__()
        self._number = number
        self._on_hover = on_hover  # (number, bool) -> None

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._on_hover(self._number, True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._on_hover(self._number, False)
        super().leaveEvent(event)


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
        self.include.setObjectName("afInclude")
        self.include.setChecked(True)
        self.include.setCursor(Qt.PointingHandCursor)
        self.include.setToolTip("Add this card to the entry")
        self.name = QLineEdit(self.guessed_name)
        self.name.setObjectName("afName")
        self.name.setPlaceholderText(
            "Type the card name…" if not self.guessed_name else ""
        )
        self.set = QLineEdit((label.set_name if label else None) or "")
        self.set.setObjectName("afMeta")
        self.set.setPlaceholderText("Set")
        self.number = QLineEdit((label.card_number if label else None) or "")
        self.number.setObjectName("afMeta")
        self.number.setPlaceholderText("No.")
        self.qty = QSpinBox()
        self.qty.setObjectName("afQty")
        self.qty.setMinimum(1)
        self.qty.setMaximum(9999)
        # Typed, arrow-less spins are the app-wide convention (see transaction /
        # auction card rows), and the arrows don't survive a styled sub-control.
        self.qty.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.qty.setAlignment(Qt.AlignCenter)
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
    def __init__(self, image_path: str, detections, conn=None, parent=None) -> None:
        super().__init__(parent)
        t = theme.active()
        self.setObjectName("afDialog")
        self.setWindowTitle("Autofill cards from photo")
        self.setStyleSheet(_qss(t))
        self.resize(1000, 640)
        self.setMinimumSize(820, 520)
        self._rows: list[_Row] = [_Row(d, conn) for d in detections]
        self._box_items: dict[int, QGraphicsRectItem] = {}
        self._cards: dict[int, QFrame] = {}
        self._hl: int | None = None

        confident = sum(1 for r in self._rows if r.det.confident)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addLayout(self._build_header(t, confident))

        split = QSplitter(Qt.Horizontal)
        split.setObjectName("afSplit")
        split.setHandleWidth(12)
        split.addWidget(self._build_stage(image_path, detections))
        split.addWidget(self._build_panel())
        split.setSizes([460, 500])
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        layout.addLayout(self._build_footer(t))
        self._sync()

    # -- construction -------------------------------------------------------

    def _build_header(self, t: theme.Tokens, confident: int) -> QHBoxLayout:
        tile = QLabel()
        tile.setObjectName("afIcon")
        tile.setFixedSize(34, 34)
        tile.setAlignment(Qt.AlignCenter)
        tile.setPixmap(icons.pixmap("scan-line", t.accent, 18))

        title = QLabel("Autofill cards from photo")
        title.setObjectName("afTitle")
        found = len(self._rows)
        sub = QLabel(
            f"{found} card{'' if found == 1 else 's'} detected · "
            f"{confident} confidently identified — confirm or correct each identity, "
            "and uncheck any false detection."
        )
        sub.setObjectName("afSub")
        sub.setWordWrap(True)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(title)
        text.addWidget(sub)

        self._count = QLabel()
        self._count.setObjectName("afCount")
        self._count.setAlignment(Qt.AlignCenter)

        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(tile, 0, Qt.AlignTop)
        head.addLayout(text, 1)
        head.addWidget(self._count, 0, Qt.AlignTop)
        return head

    def _build_stage(self, image_path: str, detections) -> QWidget:
        t = theme.active()
        scene = QGraphicsScene(self)
        pixmap = QPixmap(str(image_path))
        if pixmap.isNull():
            note = scene.addSimpleText("Preview unavailable")
            note.setBrush(QBrush(QColor(t.ink3)))
            scene.setSceneRect(note.boundingRect().adjusted(-20, -20, 20, 20))
        else:
            scene.addPixmap(pixmap)
            scene.setSceneRect(QRectF(pixmap.rect()))
            pw, ph = max(1, pixmap.width()), max(1, pixmap.height())
            for det in detections:
                x, y, w, h = det.box
                self._box_items[det.number] = self._add_box(
                    scene, QRectF(x * pw, y * ph, w * pw, h * ph), det.number, t
                )

        frame = QFrame()
        frame.setObjectName("afStage")
        v = QVBoxLayout(frame)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(_cap_bar("DETECTED SCENE", _source_label(image_path)))
        v.addWidget(_SceneView(scene), 1)
        return frame

    def _add_box(
        self, scene: QGraphicsScene, rect: QRectF, number: int, t: theme.Tokens
    ) -> QGraphicsRectItem:
        """One dashed accent box plus a fixed-size numbered badge at its corner."""
        item = QGraphicsRectItem(rect)
        item.setPen(self._box_pen(False, t))
        scene.addItem(item)
        badge = QGraphicsEllipseItem(-11, -11, 22, 22, item)
        badge.setBrush(QBrush(QColor(t.accent)))
        badge.setPen(QPen(Qt.NoPen))
        badge.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        badge.setPos(rect.topLeft())
        num = QGraphicsSimpleTextItem(str(number), badge)
        num.setBrush(QBrush(QColor(t.accent_on)))
        font = num.font()
        font.setBold(True)
        num.setFont(font)
        box = num.boundingRect()
        num.setPos(-box.width() / 2, -box.height() / 2)
        return item

    @staticmethod
    def _box_pen(highlighted: bool, t: theme.Tokens) -> QPen:
        pen = QPen(QColor(t.accent_ink if highlighted else t.accent))
        pen.setCosmetic(True)  # constant on-screen width, whatever the zoom
        pen.setWidthF(3.5 if highlighted else 2.0)
        if not highlighted:
            pen.setStyle(Qt.DashLine)
        return pen

    def _build_panel(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("afHolder")
        col = QVBoxLayout(holder)
        col.setContentsMargins(11, 11, 11, 11)
        col.setSpacing(9)
        for row in self._rows:
            card = self._build_row_card(row)
            self._cards[row.det.number] = card
            col.addWidget(card)
        if not self._rows:
            empty = QLabel("No cards detected in this image.")
            empty.setObjectName("afSub")
            empty.setAlignment(Qt.AlignCenter)
            col.addWidget(empty)
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setObjectName("afScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(holder)

        frame = QFrame()
        frame.setObjectName("afPanel")
        v = QVBoxLayout(frame)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(
            _cap_bar("IDENTITIES", "hover a row to locate its card", mono=False)
        )
        v.addWidget(scroll, 1)
        return frame

    def _build_row_card(self, row: _Row) -> QFrame:
        card = _HoverCard(row.det.number, self._on_hover)
        card.setObjectName("afRow")
        v = QVBoxLayout(card)
        v.setContentsMargins(11, 9, 11, 9)
        v.setSpacing(8)

        badge = QLabel(str(row.det.number))
        badge.setObjectName("afBadge")
        badge.setFixedSize(22, 22)
        badge.setAlignment(Qt.AlignCenter)
        top = QHBoxLayout()
        top.setSpacing(9)
        top.addWidget(row.include)
        top.addWidget(badge)
        top.addWidget(row.name, 1)
        top.addWidget(self._conf_pill(row.det))
        v.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(7)
        bottom.addWidget(_micro("SET"))
        bottom.addWidget(row.set, 3)
        bottom.addWidget(_micro("NO."))
        bottom.addWidget(row.number, 1)
        bottom.addSpacing(4)
        bottom.addWidget(_micro("QTY"))
        row.qty.setFixedWidth(62)
        bottom.addWidget(row.qty)
        v.addLayout(bottom)

        row.include.toggled.connect(self._sync)
        row.include.toggled.connect(lambda on, c=card: self._set_off(c, not on))
        row.name.textChanged.connect(self._sync)
        return card

    @staticmethod
    def _set_off(card: QFrame, off: bool) -> None:
        """Dim a card that won't be added — the filled/empty checkbox alone is a
        thin signal for 'is this one going in?'. Children are repolished too, or
        Qt keeps their old style despite the ancestor-property selectors."""
        card.setProperty("off", off)
        for widget in [card, *card.findChildren(QWidget)]:
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    @staticmethod
    def _conf_pill(det) -> QLabel:
        """The matcher's confidence, as a status pill."""
        if det.label is None:
            pill = QLabel("no match")
            pill.setObjectName("afPillNone")
            pill.setToolTip("No match in the labeled corpus yet — type the card name")
        else:
            pct = int(round(max(0.0, min(1.0, det.score)) * 100))
            if det.confident:
                pill = QLabel(f"{pct}%")
                pill.setObjectName("afPillOk")
                pill.setToolTip("Confident match")
            else:
                pill = QLabel(f"⚠ {pct}%")
                pill.setObjectName("afPillWarn")
                pill.setToolTip(
                    "Low confidence — double-check; it'll be flagged for review"
                )
        pill.setAlignment(Qt.AlignCenter)
        pill.setFixedHeight(22)
        return pill

    def _build_footer(self, t: theme.Tokens) -> QHBoxLayout:
        hint = QLabel(
            "Corrections re-pick a real card, and every confirmed card trains the "
            "matcher."
        )
        hint.setObjectName("afHint")
        cancel = QPushButton("Cancel")
        cancel.setObjectName("afGhost")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        self._add = QPushButton("Add checked cards")
        self._add.setObjectName("afPrimary")
        self._add.setCursor(Qt.PointingHandCursor)
        self._add.setIcon(icons.icon("plus", t.accent_on, 15))
        self._add.setDefault(True)
        self._add.clicked.connect(self.accept)

        foot = QHBoxLayout()
        foot.setSpacing(10)
        foot.addWidget(hint)
        foot.addStretch(1)
        foot.addWidget(cancel)
        foot.addWidget(self._add)
        return foot

    # -- interaction --------------------------------------------------------

    def _sync(self, *_args) -> None:
        """Keep the header count and the primary button honest about what's in."""
        n = len(self.result_cards())
        self._count.setText(f"{n} to add")
        self._add.setEnabled(n > 0)

    def _on_hover(self, number: int, on: bool) -> None:
        self._highlight(number if on else None)

    def _highlight(self, number: int | None) -> None:
        if number == self._hl:
            return
        self._apply_highlight(self._hl, False)
        self._hl = number
        self._apply_highlight(number, True)

    def _apply_highlight(self, number: int | None, on: bool) -> None:
        if number is None:
            return
        t = theme.active()
        item = self._box_items.get(number)
        if item is not None:
            item.setPen(self._box_pen(on, t))
            item.setZValue(1 if on else 0)
        card = self._cards.get(number)
        if card is not None:
            card.setProperty("hl", on)
            card.style().unpolish(card)
            card.style().polish(card)

    def result_cards(self) -> list[AcceptedCard]:
        return [card for card in (r.accepted() for r in self._rows) if card]


def _qss(t: theme.Tokens) -> str:
    return f"""
    #afDialog {{ background: {t.bg}; }}
    #afTitle {{ font-size: 17px; font-weight: 800; color: {t.ink}; }}
    #afSub {{ color: {t.ink3}; font-size: 12px; }}
    #afIcon {{ background: {t.accent_soft}; border-radius: 10px; }}
    #afCount {{ background: {t.accent_soft}; color: {t.accent_ink};
        border-radius: 11px; padding: 4px 12px; font-size: 11px; font-weight: 700; }}

    #afSplit::handle {{ background: transparent; }}
    #afStage, #afPanel {{ background: {t.surface}; border: 1px solid {t.line};
        border-radius: 12px; }}
    #afCap {{ background: transparent; border: none;
        border-bottom: 1px solid {t.line}; }}
    #afCapKey {{ color: {t.ink3}; font-size: 10px; font-weight: 700;
        letter-spacing: 1px; }}
    #afCapVal {{ color: {t.ink3}; font-family: "{theme.mono_family()}";
        font-size: 11px; }}
    #afCapHint {{ color: {t.ink3}; font-size: 11px; }}
    #afCanvas {{ background: {t.bg}; border: none;
        border-bottom-left-radius: 12px; border-bottom-right-radius: 12px; }}
    #afScroll, #afHolder {{ background: transparent; border: none; }}

    #afRow {{ background: {t.surface2}; border: 1px solid {t.line};
        border-radius: 10px; }}
    #afRow[off="true"] {{ background: transparent; border-style: dashed; }}
    #afRow[off="true"] QLineEdit {{ color: {t.ink3}; }}
    #afRow[off="true"] #afBadge {{ background: {t.line2}; color: {t.ink3}; }}
    /* Hover wins over the dimmed state, so locating a skipped box still lights up. */
    #afRow[hl="true"] {{ border-color: {t.accent}; border-style: solid;
        background: {t.accent_soft}; }}
    #afBadge {{ background: {t.accent}; color: {t.accent_on}; border-radius: 11px;
        font-weight: 800; font-size: 11px; }}
    #afMicro {{ color: {t.ink3}; font-size: 9px; font-weight: 700;
        letter-spacing: .4px; }}
    QLineEdit#afName {{ background: {t.surface}; border: 1px solid {t.line2};
        border-radius: 7px; padding: 6px 9px; color: {t.ink}; font-size: 12px;
        font-weight: 600; }}
    QLineEdit#afName:focus {{ border-color: {t.accent}; }}
    QLineEdit#afMeta {{ background: {t.surface}; border: 1px solid {t.line};
        border-radius: 7px; padding: 4px 8px; color: {t.ink2}; font-size: 11px; }}
    QLineEdit#afMeta:focus {{ border-color: {t.accent}; }}
    QSpinBox#afQty {{ background: {t.surface}; border: 1px solid {t.line};
        border-radius: 7px; padding: 3px 5px; color: {t.ink}; font-size: 11px; }}
    QCheckBox#afInclude::indicator {{ width: 15px; height: 15px; border-radius: 4px;
        border: 1.5px solid {t.line2}; background: {t.surface}; }}
    QCheckBox#afInclude::indicator:hover {{ border-color: {t.accent}; }}
    /* No image override — Fusion draws its checkmark over the accent fill, so a
       checked row is unmistakable (an empty accent square is not). */
    QCheckBox#afInclude::indicator:checked {{ background: {t.accent};
        border-color: {t.accent}; }}

    #afPillOk, #afPillWarn, #afPillNone {{ border-radius: 11px; padding: 2px 10px;
        font-size: 10px; font-weight: 700; }}
    #afPillOk {{ background: {_tint(t.pos, 0.16)}; color: {t.pos}; }}
    #afPillWarn {{ background: {_tint(t.warn, 0.16)}; color: {t.warn}; }}
    #afPillNone {{ background: transparent; color: {t.ink3};
        border: 1px solid {t.line2}; }}

    #afHint {{ color: {t.ink3}; font-size: 11px; }}
    QPushButton#afGhost {{ background: transparent; border: 1px solid {t.line2};
        border-radius: 10px; color: {t.ink2}; padding: 8px 16px; font-size: 12px;
        font-weight: 700; }}
    QPushButton#afGhost:hover {{ border-color: {t.accent}; color: {t.accent_ink}; }}
    QPushButton#afPrimary {{ background: {t.accent}; color: {t.accent_on};
        border: none; border-radius: 10px; padding: 8px 18px; font-size: 12px;
        font-weight: 800; }}
    QPushButton#afPrimary:hover {{ background: {t.accent_ink}; }}
    QPushButton#afPrimary:disabled {{ background: {t.surface2}; color: {t.ink3}; }}
    """
