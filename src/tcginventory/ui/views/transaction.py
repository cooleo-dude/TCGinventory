"""Unified Transaction screen — the write UI (F-TX-*, F-PU/SA/TR-*).

A **"You vs Them" face-off**: everything you put on the table (cards you give +
cash you pay) on the left, everything the counterparty puts up (their cards +
cash they pay) on the right. The running totals (You give / They give / Net cash
/ Est. margin) sit top-left, and the inferred type (purchase / sale / trade)
shows as a colour-coded badge up top.

Cards are entered as stacked **card rows** — a ＋ adds one, a per-row ✕ removes
it. A give row searches your on-hand stock; a receive row carries the full card
field set with the essentials inline (name, set, #, condition, market, qty,
intent) and the rest behind a per-row expander; its Name field autocompletes
from the local card catalog and auto-fills set/number. Type is inferred live and
Submit (bottom-right) is blocked until valid.

Also supports **edit mode**: pre-loaded from an existing transaction, Submit
performs an atomic void + re-post via :meth:`TransactionService.edit`.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import (
    QDate,
    QModelIndex,
    QRectF,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from tcginventory import config
from tcginventory.data.repositories import (
    CardCatalogRepository,
    ItemRepository,
    LotRepository,
    TransactionLineRepository,
    TransactionRepository,
)
from tcginventory.domain.enums import (
    Category,
    Condition,
    Direction,
    Edition,
    GradingCompany,
    HoloPattern,
    Intent,
    Language,
    TransactionStatus,
)
from tcginventory.domain.models import Item
from tcginventory.services import cardimages, tcgplayer
from tcginventory.services.ebay import sold_search_url
from tcginventory.services.exceptions import ServiceError
from tcginventory.services.inference import write_back as inference_write_back
from tcginventory.services.inventory import InventoryService
from tcginventory.services.reporting import ReportingService
from tcginventory.services.review import without_review_note
from tcginventory.services.transactions import (
    IncomingCard,
    OutgoingCard,
    TransactionRequest,
    TransactionService,
)
from tcginventory.ui import icons, theme
from tcginventory.ui.card_preview import (
    CardImagePane,
    _FetchWorker,
    install_hover_preview,
)
from tcginventory.ui.formatting import money, percent
from tcginventory.ui.views._autofill_dialog import AutofillDialog

_TOOLBAR_H = 34  # Them's autofill toolbar row; matched by a You-side spacer
_DOCK_W = 178  # preview dock width; the header fields align to the board past it
_CONTENT_GAP = 14  # spacing between a dock and the board
_BOARD_GAP = 16  # spacing between the You and Them panels


def _mono_font(size: float, bold: bool = False) -> QFont:
    f = QFont(theme.mono_family())
    f.setPointSizeF(size)
    f.setBold(bold)
    return f


def _icon_button(
    glyph: str, tip: str, danger: bool = False, icon: str | None = None
) -> QPushButton:
    """A borderless square button. Pass ``icon`` for a Lucide SVG (crisp + always
    visible); ``glyph`` is a text fallback."""
    t = theme.active()
    b = QPushButton(glyph)
    b.setCursor(Qt.PointingHandCursor)
    b.setToolTip(tip)
    b.setFixedSize(26, 26)
    hover = t.neg if danger else t.ink
    if icon:
        b.setIcon(icons.icon(icon, t.ink2, 16))  # visible neutral; hovers to accent
    b.setStyleSheet(
        f"QPushButton{{border:none;background:transparent;color:{t.ink2};"
        f"font-size:14px;border-radius:7px;}}"
        f"QPushButton:hover{{background:{t.surface};color:{hover};}}"
    )
    return b


def _link_button(text: str, tip: str) -> QPushButton:
    """A compact outlined quick-link chip (e.g. TCG / eBay) that hovers to accent."""
    t = theme.active()
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    b.setToolTip(tip)
    b.setFixedHeight(24)
    b.setStyleSheet(
        f"QPushButton{{border:1px solid {t.line2};border-radius:6px;color:{t.ink2};"
        f"padding:1px 8px;font-size:11px;font-weight:600;}}"
        f"QPushButton:hover{{border-color:{t.accent};color:{t.accent_ink};}}"
    )
    return b


def _review_chip() -> QPushButton:
    """A checkable 'flag for review' chip that sits inline with the quick-links.
    Amber (armed) when checked, neutral outline when off — it *is* the card's
    review flag, so ``isChecked``/``setChecked`` drive posting as before."""
    t = theme.active()
    b = QPushButton("Review")
    b.setCheckable(True)
    b.setCursor(Qt.PointingHandCursor)
    b.setToolTip("Flag this card for review (e.g. the price needs a second look)")
    b.setFixedHeight(24)
    b.setIcon(icons.icon("triangle-alert", t.ink2, 13))
    warn = QColor(t.warn)
    tint = f"rgba({warn.red()},{warn.green()},{warn.blue()},0.16)"
    b.setStyleSheet(
        f"QPushButton{{border:1px solid {t.line2};border-radius:6px;color:{t.ink2};"
        f"padding:1px 8px 1px 6px;font-size:11px;font-weight:600;}}"
        f"QPushButton:hover{{border-color:{t.warn};color:{t.warn};}}"
        f"QPushButton:checked{{background:{tint};border-color:{t.warn};"
        f"color:{t.warn};}}"
    )
    # Recolour the glyph to match the armed/idle text colour.
    b.toggled.connect(
        lambda on: b.setIcon(icons.icon("triangle-alert", t.warn if on else t.ink2, 13))
    )
    return b


def _inline_field(label: str, widget: QWidget) -> QHBoxLayout:
    """A label beside its input (used above each side)."""
    t = theme.active()
    row = QHBoxLayout()
    row.setSpacing(8)
    lab = QLabel(label.upper())
    lab.setStyleSheet(
        f"color:{t.ink3};font-size:10px;font-weight:700;letter-spacing:.4px;"
    )
    row.addWidget(lab)
    row.addWidget(widget, 1)
    return row


def _cash_block(label: str, spin: QDoubleSpinBox, rail: str) -> QWidget:
    """An emphasised cash field (label left, big mono amount right)."""
    t = theme.active()
    box = QFrame()
    box.setObjectName("cash")
    box.setStyleSheet(
        f"QFrame#cash{{background:{t.zebra};border:1px solid {t.line};"
        f"border-left:3px solid {rail};border-radius:11px;}}"
    )
    h = QHBoxLayout(box)
    h.setContentsMargins(13, 8, 11, 8)
    cl = QLabel(label)
    cl.setStyleSheet(f"color:{t.ink2};font-size:11.5px;font-weight:600;")
    h.addWidget(cl)
    h.addStretch(1)
    spin.setFont(_mono_font(17, bold=True))
    spin.setStyleSheet("border:none;background:transparent;")
    spin.setAlignment(Qt.AlignRight)
    spin.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
    h.addWidget(spin)
    return box


def _add_button(text: str, slot) -> QPushButton:
    """A full-width dashed 'add a card' button."""
    t = theme.active()
    b = QPushButton("  " + text)
    b.setCursor(Qt.PointingHandCursor)
    b.setIcon(icons.icon("plus", t.ink3, 15))
    b.setStyleSheet(
        f"QPushButton{{border:1px dashed {t.line2};border-radius:9px;"
        f"color:{t.ink3};padding:9px;font-size:12px;font-weight:600;}}"
        f"QPushButton:hover{{border-color:{t.accent};color:{t.accent_ink};}}"
    )
    b.clicked.connect(slot)
    return b


def money_spin() -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setPrefix("$")
    spin.setDecimals(2)
    spin.setMaximum(10_000_000.0)
    spin.setGroupSeparatorShown(True)
    return spin


def _cents(spin: QDoubleSpinBox) -> int:
    return round(spin.value() * 100)


def _combo(values, *, capitalize: bool = False) -> QComboBox:
    combo = QComboBox()
    for v in values:
        combo.addItem(v.value.capitalize() if capitalize else v.value, v.value)
    return combo


def _select(combo: QComboBox, value: str) -> None:
    pos = combo.findData(value)
    if pos >= 0:
        combo.setCurrentIndex(pos)


def _recognized_name(card) -> str:
    """A short label for a recognized card in warnings."""
    parts = [card.name or "?"]
    if card.card_number:
        parts.append(f"#{card.card_number}")
    return " ".join(parts)


def _match_sellable(sellable, card) -> int | None:
    """Best-effort match of a recognized card to an owned sellable lot's item id,
    by name (+ collector number, then set). Returns None when nothing matches."""

    def norm(text) -> str:
        return (text or "").strip().casefold()

    def numkey(text) -> str:
        return norm(text).split("/", 1)[0].lstrip("0")

    name = norm(card.name)
    if not name:
        return None
    num = numkey(card.card_number)
    set_name = norm(card.set_name)
    # 1) name + number (+ set when both sides have one); 2) name + number; 3) name.
    for require_set in (True, False):
        for item, _avail in sellable:
            if norm(item.name) != name:
                continue
            if num and numkey(item.card_number) != num:
                continue
            if require_set and set_name and norm(item.set_name) != set_name:
                continue
            return item.id
    for item, _avail in sellable:
        if norm(item.name) == name:
            return item.id
    return None


class _PriceLookupWorker(QThread):
    """Fetch a picked card's current tcgcsv market price off the UI thread."""

    done = Signal(object)  # (price_cents, tcgplayer_url) or None

    def __init__(
        self,
        set_name: str,
        number: str,
        name: str,
        category: int = 3,
        set_code: str | None = None,
    ) -> None:
        super().__init__()
        self._set = set_name
        self._number = number
        self._name = name
        self._category = category
        self._set_code = set_code

    def run(self) -> None:
        from tcginventory.services.pricing import TcgcsvProvider

        try:
            result = TcgcsvProvider().price_for_card(
                self._set,
                self._number,
                self._name,
                category=self._category,
                set_code=self._set_code,
            )
        except Exception:  # network/parse failure → just skip the auto-price
            result = None
        self.done.emit(result)


class _FocusLineEdit(QLineEdit):
    """A line edit that announces focus-in, so an empty picker can pop its list."""

    focused = Signal()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.focused.emit()


class _GiveRow:
    """Inline row for a card you give: search your on-hand cards + quantity.

    Starts **empty** — the user types (or focuses to browse) and picks a match.
    Hovering a match in the popup, or the chosen card in the field, previews its
    image in the shared card display.
    """

    _ITEM_ID_ROLE = Qt.UserRole + 1

    def __init__(
        self, conn, sellable: list[tuple[Item, int]], on_change, sink=None
    ) -> None:
        self._conn = conn
        self._sellable = sellable  # [(Item, available)]
        self._by_id = {item.id: (item, avail) for item, avail in sellable}
        self._sink = sink
        self._on_change = on_change
        self._chosen_id: int | None = None

        self.name = _FocusLineEdit()
        self.name.setPlaceholderText("Type to search your cards to give…")
        self._model = QStandardItemModel(self.name)
        self._completer = QCompleter(self._model, self.name)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.name.setCompleter(self._completer)
        self.name.textEdited.connect(self._on_edited)
        self.name.focused.connect(self._show_all)
        self._completer.activated[QModelIndex].connect(self._pick)
        self.name.textChanged.connect(self._on_text_changed)
        install_hover_preview(self.name, self._hover_card, sink)
        popup = self._completer.popup()
        popup.setMouseTracking(True)
        popup.entered.connect(self._on_popup_hover)
        install_hover_preview(popup, lambda: None, sink)

        self.qty = QSpinBox()
        self.qty.setMinimum(1)
        self.qty.setMaximum(1)  # widened once a card is chosen
        self.qty.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.qty.valueChanged.connect(lambda *_: on_change())

    def widgets(self) -> list[QWidget]:
        return [self.name, self.qty]

    @staticmethod
    def _label(item: Item) -> str:
        """Card name (+set), then its Notes — with the auto "Manual price…"
        review marker stripped out. No notes → just the name."""
        name = item.name + (f" ({item.set_name})" if item.set_name else "")
        note = without_review_note(item.notes)
        return f"{name} — {note}" if note else name

    def _fill(self, needle: str) -> None:
        self._model.clear()
        needle = needle.strip().lower()
        for item, _available in self._sellable:
            label = self._label(item)
            if needle and needle not in label.lower():
                continue
            entry = QStandardItem(label)
            entry.setData(item.id, self._ITEM_ID_ROLE)
            self._model.appendRow(entry)

    def _on_edited(self, text: str) -> None:
        self._fill(text)
        if self._model.rowCount():
            self._completer.complete()

    def _show_all(self) -> None:
        # Focus an empty field → list everything you can give.
        if self.name.text().strip():
            return
        self._fill("")
        if self._model.rowCount():
            self._completer.complete()

    def _on_text_changed(self, *_) -> None:
        # Editing away from the chosen card's label drops the selection.
        if self._chosen_id is not None:
            item, _available = self._by_id[self._chosen_id]
            if self.name.text() != self._label(item):
                self._chosen_id = None
                self.qty.setMaximum(1)
        self._on_change()

    def _on_popup_hover(self, index: QModelIndex) -> None:
        self._preview(index.data(self._ITEM_ID_ROLE))

    def _pick(self, index: QModelIndex) -> None:
        self._set_chosen(index.data(self._ITEM_ID_ROLE))

    def _set_chosen(self, item_id) -> None:
        pair = self._by_id.get(item_id)
        if pair is None:
            return
        item, available = pair
        self._chosen_id = item_id
        self.name.setText(self._label(item))
        self.qty.setMaximum(max(1, available))
        self._preview(item_id)
        self._on_change()

    def _image_for(self, item: Item) -> tuple[str, str] | None:
        return CardCatalogRepository(self._conn).image_for(
            item.name, item.set_name, item.card_number
        )

    def _preview(self, item_id) -> None:
        if self._sink is None or item_id is None:
            return
        pair = self._by_id.get(item_id)
        if pair is not None:
            res = self._image_for(pair[0])
            self._sink.show_card(*(res if res else (None, None)))

    def _hover_card(self) -> tuple[str, str] | None:
        if self._chosen_id is None:
            return None
        return self._image_for(self._by_id[self._chosen_id][0])

    def set_item(self, item_id: int, quantity: int) -> None:
        self._set_chosen(item_id)
        self.qty.setValue(quantity)

    def to_card(self):
        if self._chosen_id is None:
            return None
        return OutgoingCard(item_id=self._chosen_id, quantity=self.qty.value())

    def widget(self, on_delete) -> QWidget:
        """A styled give card-row: search field + quantity + remove."""
        t = theme.active()
        card = QFrame()
        card.setObjectName("giverow")
        card.setStyleSheet(
            f"QFrame#giverow{{background:{t.surface2};border:1px solid {t.line};"
            f"border-radius:10px;}}"
        )
        h = QHBoxLayout(card)
        h.setContentsMargins(11, 7, 9, 7)
        h.setSpacing(9)
        h.addWidget(self.name, 1)
        qlbl = QLabel("Qty")
        qlbl.setStyleSheet(f"color:{t.ink3};font-size:11px;")
        h.addWidget(qlbl)
        self.qty.setFixedWidth(56)
        h.addWidget(self.qty)
        remove = _icon_button("", "Remove this card", danger=True, icon="trash-2")
        remove.clicked.connect(lambda: on_delete())
        h.addWidget(remove)
        return card


class _MatchDelegate(QStyledItemDelegate):
    """Render autofill popup rows with the full "name · set #number" label.

    QCompleter shows *and* inserts its completion role, so the completion text
    is kept as the clean card name (what lands in the Name field) while this
    delegate paints the richer label from a separate role in the popup only.
    """

    def __init__(self, label_role: int, parent=None) -> None:
        super().__init__(parent)
        self._label_role = label_role

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        label = index.data(self._label_role)
        if label:
            option.text = label


class _ReceiveRow:
    """Inline row for a card you receive: the full card field set, entered
    directly. Name autocompletes from the catalog and fills set/number."""

    def __init__(self, conn, is_auto, on_change, on_pick=None, sink=None) -> None:
        self._conn = conn
        self._is_auto = is_auto  # () -> bool : is "Auto Detect Card" on?
        self._on_change = on_change
        self._on_pick = on_pick  # (row, CatalogCard) -> None : after a pick
        self._sink = sink  # preview target (Transaction's lower-left pane)
        self._matches: dict[str, object] = {}
        self._card_id: str | None = None  # set when a catalog card is applied
        self._image_url: str | None = None

        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Pikachu Celebrations 005/025")
        # Whole-string catalog search: type name (+set +number), pick a match.
        self._search_model = QStandardItemModel(self.name)
        self._completer = QCompleter(self._search_model, self.name)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.name.setCompleter(self._completer)
        self.name.textEdited.connect(self._on_name_edited)
        self._completer.activated[QModelIndex].connect(self._pick_match)
        self.name.textChanged.connect(lambda *_: on_change())
        # Hover the Name field to preview the card image; hover a match in the
        # autofill popup to preview it before picking. Previews land in the
        # Transaction window's lower-left pane (the sink).
        install_hover_preview(self.name, self._hover_card, self._sink)
        popup = self._completer.popup()
        popup.setMouseTracking(True)
        popup.entered.connect(self._on_match_hover)
        self._match_delegate = _MatchDelegate(self._LABEL_ROLE, popup)
        popup.setItemDelegate(self._match_delegate)
        install_hover_preview(popup, lambda: None, self._sink)  # clear on leave

        self.category = _combo(Category, capitalize=True)
        self.set = QLineEdit()
        self.number = QLineEdit()
        self.language = _combo(Language)
        self.graded = QCheckBox()
        self.graded.toggled.connect(self._toggle_graded)
        self.graded.toggled.connect(lambda *_: on_change())
        self.condition = _combo(Condition)
        # Auto-mark for review when a non-NM condition is chosen.
        self.condition.currentIndexChanged.connect(self._maybe_flag_review)
        self.holo = _combo(HoloPattern)  # default NH
        self.edition = _combo(Edition)  # default N/A
        self.company = _combo(GradingCompany)
        self.grade = QLineEdit()
        self.cert = QLineEdit()
        self.market = money_spin()
        self.market.setButtonSymbols(QAbstractSpinBox.NoButtons)  # no up/down arrows
        self.market.valueChanged.connect(lambda *_: on_change())
        self.qty = QSpinBox()
        self.qty.setMinimum(1)
        self.qty.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.qty.valueChanged.connect(lambda *_: on_change())
        self.intent = _combo(Intent, capitalize=True)
        _select(self.intent, Intent.RESALE.value)
        self.review = _review_chip()
        self.tcgplayer = QLineEdit()
        self.ebay = QLineEdit()
        self.notes = QLineEdit()
        self._toggle_graded(False)

    def widgets(self) -> list[QWidget]:
        return [
            self.name,
            self.category,
            self.set,
            self.number,
            self.language,
            self.graded,
            self.condition,
            self.holo,
            self.edition,
            self.company,
            self.grade,
            self.cert,
            self.market,
            self.qty,
            self.intent,
            self.review,
            self.tcgplayer,
            self.ebay,
            self.notes,
        ]

    def _maybe_flag_review(self) -> None:
        """Sync review to condition (ungraded): a worse-than-NM condition marks
        the card for review; switching back to NM clears it."""
        if not self.graded.isChecked():
            self.review.setChecked(self.condition.currentData() != Condition.NM.value)

    def _toggle_graded(self, graded: bool) -> None:
        # Condition greys out when graded; grade fields enable (Spec 02).
        self.condition.setEnabled(not graded)
        self.company.setEnabled(graded)
        self.grade.setEnabled(graded)
        self.cert.setEnabled(graded)

    _CARD_ID_ROLE = Qt.UserRole + 1
    _LABEL_ROLE = Qt.UserRole + 2

    def _on_name_edited(self, text: str) -> None:
        """Query the catalog as the user types (when Auto Detect is on) and show
        the matches in the popup."""
        if not self._is_auto() or len(text.strip()) < 2:
            return
        repo = CardCatalogRepository(self._conn)
        matches = repo.search(text, limit=25)
        self._matches = {c.card_id: c for c in matches}
        self._search_model.clear()
        for card in matches:
            # Completion text is the clean name (what QCompleter inserts into the
            # Name field); the popup shows the fuller label via _MatchDelegate.
            item = QStandardItem(card.name)
            label = (
                f"{card.name}  ·  {card.set_name or '?'}  "
                f"#{repo.display_number(card) or '?'}"
            )
            item.setData(card.card_id, self._CARD_ID_ROLE)
            item.setData(label, self._LABEL_ROLE)
            self._search_model.appendRow(item)
        if matches:
            self._completer.complete()

    def _on_match_hover(self, index: QModelIndex) -> None:
        """Preview a card image as the pointer moves over autofill matches."""
        card = self._matches.get(index.data(self._CARD_ID_ROLE))
        if card is not None and self._sink is not None:
            self._sink.show_card(card.card_id, getattr(card, "image_small", None))

    def _hover_card(self) -> tuple[str, str] | None:
        if self._card_id and self._image_url:
            return self._card_id, self._image_url
        name = self.name.text().strip()
        if not name:
            return None
        return CardCatalogRepository(self._conn).image_for(
            name, self.set.text().strip(), self.number.text().strip()
        )

    def _pick_match(self, index: QModelIndex) -> None:
        card = self._matches.get(index.data(self._CARD_ID_ROLE))
        if card is not None:
            self.apply_catalog_card(card)
            if self._on_pick is not None:
                self._on_pick(self, card)

    def apply_catalog_card(self, card) -> None:
        """Populate as many fields as possible from a chosen catalog card."""
        self._card_id = card.card_id
        self._image_url = getattr(card, "image_small", None)
        self.name.setText(card.name)
        self.set.setText(card.set_name or "")
        # Catalog-derived denominator: correct for prefixed subsets like Radiant
        # Collection ("RC19/RC32"), where the parent set's total would be wrong.
        number = CardCatalogRepository(self._conn).display_number(card)
        self.number.setText(number or card.full_number or card.number or "")
        _select(self.category, Category.SINGLE.value)  # catalog cards are singles
        _select(self.language, getattr(card, "language", None) or Language.EN.value)
        # Keep the chosen card showing in the pane after the popup closes.
        if self._sink is not None and self._image_url:
            self._sink.show_card(self._card_id, self._image_url)
        self._on_change()

    def set_card(self, card: IncomingCard) -> None:
        item = card.item
        self.name.setText(item.name)
        _select(self.category, item.category.value)
        self.set.setText(item.set_name or "")
        self.number.setText(item.card_number or "")
        _select(self.language, item.language.value)
        self.graded.setChecked(item.is_graded)
        if item.condition is not None:
            _select(self.condition, item.condition.value)
        if item.holo_pattern is not None:
            _select(self.holo, item.holo_pattern.value)
        if item.edition is not None:
            _select(self.edition, item.edition.value)
        if item.grading_company is not None:
            _select(self.company, item.grading_company.value)
        self.grade.setText(item.grade or "")
        self.cert.setText(item.cert_number or "")
        self.market.setValue(card.market_value_cents / 100)
        self.qty.setValue(card.quantity)
        _select(self.intent, card.intent.value)
        self.review.setChecked(getattr(card, "review", False))
        self.tcgplayer.setText(item.tcgplayer_url or "")
        self.ebay.setText(item.ebay_url or "")
        self.notes.setText(item.notes or "")

    def to_card(self):
        name = self.name.text().strip()
        if not name:
            return None
        graded = self.graded.isChecked()
        item = Item(
            name=name,
            category=Category(self.category.currentData()),
            set_name=self.set.text().strip() or None,
            card_number=self.number.text().strip() or None,
            language=Language(self.language.currentData()),
            condition=None if graded else Condition(self.condition.currentData()),
            holo_pattern=HoloPattern(self.holo.currentData()),
            edition=Edition(self.edition.currentData()),
            is_graded=graded,
            grading_company=(
                GradingCompany(self.company.currentData()) if graded else None
            ),
            grade=(self.grade.text().strip() or None) if graded else None,
            cert_number=(self.cert.text().strip() or None) if graded else None,
            tcgplayer_url=self.tcgplayer.text().strip() or None,
            ebay_url=self.ebay.text().strip() or None,
            notes=self.notes.text().strip() or None,
        )
        return IncomingCard(
            item=item,
            quantity=self.qty.value(),
            market_value_cents=round(self.market.value() * 100),
            intent=Intent(self.intent.currentData()),
            review=self.review.isChecked(),
        )

    def _open_tcgplayer(self) -> None:
        card = self.to_card()  # None until the row has a name
        if card is not None and (url := tcgplayer.link_for(card.item)):
            QDesktopServices.openUrl(QUrl(url))

    def _open_ebay(self) -> None:
        card = self.to_card()
        if card is not None:
            url = card.item.ebay_url or sold_search_url(card.item)
            QDesktopServices.openUrl(QUrl(url))

    def widget(self, on_delete) -> QWidget:
        """A card-row: essentials inline, advanced fields behind an expander."""
        t = theme.active()
        card = QFrame()
        card.setObjectName("rc")
        card.setStyleSheet(
            f"QFrame#rc{{background:{t.surface2};border:1px solid {t.line};"
            f"border-radius:11px;}}"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # -- main line: name, set, #, condition, market, qty, intent
        self.name.setPlaceholderText("Card name…")
        self.name.setMinimumWidth(76)  # ~2/3 of 112 — more room for set / #
        self.set.setPlaceholderText("Set")
        self.set.setFixedWidth(132)
        self.number.setPlaceholderText("#")
        self.number.setFixedWidth(88)
        self.condition.setFixedWidth(66)
        self.market.setFixedWidth(86)
        self.qty.setFixedWidth(44)
        self.intent.setFixedWidth(82)
        main = QHBoxLayout()
        main.setContentsMargins(11, 8, 9, 8)
        main.setSpacing(8)
        main.addWidget(self.name, 1)
        main.addWidget(self.set)
        main.addWidget(self.number)
        main.addWidget(self.condition)
        main.addWidget(self.market)
        main.addWidget(self.qty)
        main.addWidget(self.intent)
        # Quick market lookups — built from the card's fields (URL / search).
        self._tcg_link = _link_button("TCG", "Look this card up on TCGplayer")
        self._tcg_link.clicked.connect(self._open_tcgplayer)
        self._ebay_link = _link_button("eBay", "eBay sold listings for this card")
        self._ebay_link.clicked.connect(self._open_ebay)
        main.addWidget(self._tcg_link)
        main.addWidget(self._ebay_link)
        main.addWidget(self.review)
        self._chev = _icon_button("", "Show / hide the extra fields")
        self._chev.setIcon(icons.icon("chevron-down", t.ink3, 16))
        self._chev.clicked.connect(self._toggle_adv)
        main.addWidget(self._chev)
        remove = _icon_button("", "Remove this card", danger=True, icon="trash-2")
        remove.clicked.connect(lambda: on_delete())
        main.addWidget(remove)
        main_w = QWidget()
        main_w.setLayout(main)
        v.addWidget(main_w)

        # -- expander: the rest of the card fields
        self._adv = QFrame()
        self._adv.setObjectName("adv")
        self._adv.setStyleSheet(
            f"QFrame#adv{{background:{t.zebra};border:none;border-top:1px solid "
            f"{t.line};}}"
        )
        g = QGridLayout(self._adv)
        g.setContentsMargins(12, 11, 12, 12)
        g.setHorizontalSpacing(11)
        g.setVerticalSpacing(9)

        def cell(label: str, w: QWidget, r: int, c: int, span: int = 1) -> None:
            box = QVBoxLayout()
            box.setSpacing(3)
            lab = QLabel(label.upper())
            lab.setStyleSheet(
                f"color:{t.ink3};font-size:9px;font-weight:700;letter-spacing:.4px;"
            )
            box.addWidget(lab)
            box.addWidget(w)
            g.addLayout(box, r, c, 1, span)

        cell("Category", self.category, 0, 0)
        cell("Language", self.language, 0, 1)
        cell("Holo", self.holo, 0, 2)
        cell("Edition", self.edition, 0, 3)
        cell("Grading co.", self.company, 1, 0)
        cell("Grade", self.grade, 1, 1)
        cell("Cert #", self.cert, 1, 2)
        self.graded.setText("Graded slab")
        g.addWidget(self.graded, 1, 3, Qt.AlignBottom)
        cell("TCGplayer URL", self.tcgplayer, 2, 0, 2)
        cell("eBay URL", self.ebay, 2, 2, 2)
        cell("Notes", self.notes, 3, 0, 4)
        self._adv.setVisible(False)
        v.addWidget(self._adv)
        return card

    def _toggle_adv(self) -> None:
        show = not self._adv.isVisible()
        self._adv.setVisible(show)
        name = "chevron-up" if show else "chevron-down"
        self._chev.setIcon(icons.icon(name, theme.active().ink3, 16))


class _CardCascade(QWidget):
    """A downward-cascading fan of the cards on one side of the trade.

    Each card's art is drawn at a fixed width, offset ``_STEP`` px below the
    previous one and overlaid on top, so the whole side reads as one stacked
    pile. Images are served from the on-disk cache and fetched lazily.
    """

    _CARD_W = 150
    _STEP = 34  # vertical offset between successive cards
    _RATIO = 88 / 63  # card height / width

    def __init__(self) -> None:
        super().__init__()
        self._cards: list[str] = []  # ordered card_ids
        self._urls: dict[str, str] = {}
        self._pix: dict[str, QPixmap] = {}
        self._workers: list[_FetchWorker] = []
        self.setMinimumHeight(0)

    def set_cards(self, cards: list[tuple[str, str | None]]) -> None:
        seen: set[str] = set()
        ordered: list[str] = []
        for card_id, url in cards:
            if not card_id or card_id in seen:
                continue
            seen.add(card_id)
            ordered.append(card_id)
            if url:
                self._urls[card_id] = url
        self._cards = ordered
        for card_id in ordered:
            if card_id not in self._pix:
                self._load(card_id)
        self._resize_to_fit()
        self.update()

    def clear(self) -> None:
        self._cards = []
        self._resize_to_fit()
        self.update()

    def _card_h(self) -> int:
        return round(self._CARD_W * self._RATIO)

    def _resize_to_fit(self) -> None:
        n = len(self._cards)
        h = self._card_h() + max(0, n - 1) * self._STEP if n else 0
        self.setMinimumHeight(h)

    def _load(self, card_id: str) -> None:
        path = cardimages.cache_path(card_id)
        if path.exists():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                self._pix[card_id] = pixmap
                return
        url = self._urls.get(card_id)
        if not url:
            return
        worker = _FetchWorker(card_id, url)
        worker.done.connect(self._on_fetched)
        worker.finished.connect(lambda w=worker: self._retire(w))
        self._workers.append(worker)
        worker.start()

    def _retire(self, worker: _FetchWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)

    def _on_fetched(self, card_id: str, path: str) -> None:
        if path:
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                self._pix[card_id] = pixmap
                self._resize_to_fit()
                self.update()

    def paintEvent(self, _event) -> None:
        t = theme.active()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        cw, ch = self._CARD_W, self._card_h()
        x = max(0, (self.width() - cw) // 2)
        for i, card_id in enumerate(self._cards):
            y = i * self._STEP
            rect = QRectF(x, y, cw, ch)
            clip = QPainterPath()
            clip.addRoundedRect(rect, 8, 8)
            painter.save()
            painter.setClipPath(clip)
            pixmap = self._pix.get(card_id)
            if pixmap is not None:
                scaled = pixmap.scaled(
                    cw, ch, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
                )
                painter.drawPixmap(int(x), int(y), scaled)
            else:
                painter.fillRect(rect, QColor(t.surface2))
            painter.restore()
            pen = QPen(QColor(t.line2))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.drawPath(clip)
        painter.end()


class TransactionView(QWidget):
    posted = Signal()

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._service = TransactionService(conn)
        self._inventory = InventoryService(conn)
        self._reporting = ReportingService(conn)

        self._give_rows: list[_GiveRow] = []
        self._receive_rows: list[_ReceiveRow] = []
        self._price_workers: list[_PriceLookupWorker] = []
        self._edit_id: int | None = None
        self._inference_pipeline = None  # lazily built (image -> [Detection])
        # Docked preview panes flanking the board: the left one previews the You
        # side, the right one the Them side. Each shows the hovered/selected card
        # (persisting on leave) above a cascading fan of that side's cards.
        self._sink_you = CardImagePane()
        self._sink_them = CardImagePane()
        self._cascade_you = _CardCascade()
        self._cascade_them = _CardCascade()

        self._date = QLineEdit(QDate.currentDate().toString("yyyy-MM-dd"))
        self._date.setFixedWidth(130)
        self._counterparty = QLineEdit()
        self._counterparty.setPlaceholderText("Who you dealt with")
        self._counterparty.textChanged.connect(self._on_counterparty_changed)
        self._reference = QLineEdit()
        self._reference.setPlaceholderText("Optional note / invoice #")
        self._cash_out = money_spin()
        self._cash_out.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._cash_out.valueChanged.connect(self._update_summary)
        self._cash_in = money_spin()
        self._cash_in.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._cash_in.valueChanged.connect(self._update_summary)

        # -- centred type badge + edit banner
        self._badge = QLabel("—")
        self._badge.setToolTip("Type is inferred from which sides carry cards")
        self._badge.setAccessibleName("Inferred transaction type")
        self._edit_banner = QLabel("")
        self._edit_banner.setAlignment(Qt.AlignCenter)
        self._edit_banner.setStyleSheet(
            f"color:{theme.active().accent_ink};font-weight:600;font-size:12px;"
        )

        board = QHBoxLayout()
        board.setContentsMargins(0, 0, 0, 0)  # so the panels align with the header
        board.setSpacing(_BOARD_GAP)
        self._you_side = self._build_you_side()
        self._them_side = self._build_them_side()
        board.addWidget(self._you_side, 1)
        board.addWidget(self._them_side, 1)
        board_w = QWidget()
        board_w.setLayout(board)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # Scroll horizontally only when the window is too narrow for the board
        # beside the preview column, rather than clipping the counterparty panel.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(board_w)

        self._save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self._save_shortcut.activated.connect(self._submit_via_shortcut)

        content = QHBoxLayout()
        content.setSpacing(_CONTENT_GAP)
        content.addWidget(self._build_preview_dock("you"), 0)
        content.addWidget(scroll, 1)
        content.addWidget(self._build_preview_dock("them"), 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 0)
        layout.setSpacing(10)
        layout.addLayout(self._build_top_bar())
        layout.addLayout(self._build_hdr_fields())
        layout.addLayout(content, 1)
        layout.addWidget(self._build_receipt())

        self._update_catalog_hint()
        self._update_summary()

    def _build_preview_dock(self, side: str) -> QWidget:
        """A docked preview column for one side: the hovered/selected card on top
        (persisting on leave), and a cascading fan of that side's cards below."""
        t = theme.active()
        you = side == "you"
        accent = t.pos if you else t.accent
        sink = self._sink_you if you else self._sink_them
        cascade = self._cascade_you if you else self._cascade_them

        col = QWidget()
        col.setFixedWidth(_DOCK_W)
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        header = QLabel("YOU GIVE" if you else "THEY GIVE")
        header.setStyleSheet(
            f"color:{accent};font-size:11px;font-weight:700;letter-spacing:.6px;"
        )
        header.setAlignment(Qt.AlignLeft if you else Qt.AlignRight)
        v.addWidget(header)

        sel_cap = QLabel("SELECTED")
        sel_cap.setStyleSheet(
            f"color:{t.ink3};font-size:9px;font-weight:700;letter-spacing:.7px;"
        )
        v.addWidget(sel_cap)
        sink.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        sink.setFixedHeight(248)
        v.addWidget(sink)

        trade_cap = QLabel("IN THIS TRADE")
        trade_cap.setStyleSheet(
            f"color:{t.ink3};font-size:9px;font-weight:700;letter-spacing:.7px;"
            "margin-top:2px;"
        )
        v.addWidget(trade_cap)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        scroll.setWidget(cascade)
        v.addWidget(scroll, 1)
        return col

    # -- top bar (totals top-left + centred type badge) --------------------

    def _build_top_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.addWidget(self._build_stats(), 0, Qt.AlignTop)
        bar.addStretch(1)
        badge_col = QVBoxLayout()
        badge_col.setSpacing(4)
        badge_col.addWidget(self._build_badge(), 0, Qt.AlignHCenter)
        badge_col.addWidget(self._edit_banner)
        bar.addLayout(badge_col)
        bar.addStretch(1)
        return bar

    def _build_stats(self) -> QWidget:
        t = theme.active()
        box = QFrame()
        box.setObjectName("stats")
        box.setStyleSheet(
            f"QFrame#stats{{background:{t.surface};border:1px solid {t.line};"
            f"border-radius:12px;}}"
        )
        h = QHBoxLayout(box)
        h.setContentsMargins(15, 9, 15, 9)
        h.setSpacing(18)
        self._r_stats: dict[str, QLabel] = {}
        for i, (key, label) in enumerate(
            (
                ("yougive", "You give"),
                ("theygive", "They give"),
                ("net", "Net cash"),
                ("margin", "Est. margin"),
            )
        ):
            if i:
                sep = QFrame()
                sep.setFixedWidth(1)
                sep.setStyleSheet(f"background:{t.line};")
                h.addWidget(sep)
            block = QVBoxLayout()
            block.setSpacing(1)
            k = QLabel(label.upper())
            k.setStyleSheet(
                f"color:{t.ink3};font-size:9px;font-weight:700;letter-spacing:.4px;"
            )
            val = QLabel("—")
            val.setFont(_mono_font(15, bold=True))
            block.addWidget(k)
            block.addWidget(val)
            self._r_stats[key] = val
            h.addLayout(block)
        return box

    def _build_badge(self) -> QWidget:
        t = theme.active()
        self._badge_frame = QFrame()
        self._badge_frame.setObjectName("typebadge")
        bl = QVBoxLayout(self._badge_frame)
        bl.setContentsMargins(18, 7, 18, 7)
        bl.setSpacing(2)
        cap = QLabel("THIS RECORDS A")
        cap.setAlignment(Qt.AlignCenter)
        cap.setStyleSheet(
            f"color:{t.ink3};font-size:9px;font-weight:700;letter-spacing:1px;"
            "background:transparent;border:none;"
        )
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setStyleSheet("background:transparent;border:none;")
        bl.addWidget(cap)
        bl.addWidget(self._badge)
        return self._badge_frame

    def _build_hdr_fields(self) -> QHBoxLayout:
        # Lock the field groups to the panels below them: skip the dock+gap on the
        # left, then keep each group's width synced to its panel (see
        # _sync_header_widths, since the panels don't split evenly).
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addSpacing(_DOCK_W + _CONTENT_GAP)

        self._hdr_you = QWidget()
        you = QHBoxLayout(self._hdr_you)
        you.setContentsMargins(0, 0, 0, 0)
        you.setSpacing(14)
        you.addLayout(_inline_field("Date", self._date))
        you.addLayout(_inline_field("Reference", self._reference), 1)
        row.addWidget(self._hdr_you)

        row.addSpacing(_BOARD_GAP)

        self._hdr_them = QWidget()
        them = QHBoxLayout(self._hdr_them)
        them.setContentsMargins(0, 0, 0, 0)
        them.addLayout(_inline_field("Counterparty", self._counterparty), 1)
        row.addWidget(self._hdr_them)

        row.addStretch(1)  # absorbs the right dock+gap once widths are synced
        return row

    def _sync_header_widths(self) -> None:
        """Keep the Reference / Counterparty groups exactly as wide as the You /
        Them panels below them (the panels don't split 50/50 — Them's rows make it
        wider)."""
        for group, panel in (
            (self._hdr_you, self._you_side),
            (self._hdr_them, self._them_side),
        ):
            w = panel.width()
            if w > 0 and group.width() != w:
                group.setFixedWidth(w)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._sync_header_widths)

    # -- the two sides ------------------------------------------------------

    def _side_header(self, avatar: str, who: str, sub: str, cls: str) -> QWidget:
        t = theme.active()
        color = t.pos if cls == "you" else t.accent_ink
        soft = "rgba(131,209,90,.16)" if cls == "you" else "rgba(245,133,46,.16)"
        head = QFrame()
        h = QHBoxLayout(head)
        h.setContentsMargins(15, 12, 15, 12)
        h.setSpacing(11)
        av = QLabel(avatar)
        av.setFixedSize(32, 32)
        av.setAlignment(Qt.AlignCenter)
        av.setStyleSheet(
            f"background:{soft};color:{color};border-radius:9px;font-weight:800;"
            "font-size:13px;"
        )
        h.addWidget(av)
        who_box = QVBoxLayout()
        who_box.setSpacing(1)
        who_lbl = QLabel(who)
        who_lbl.setStyleSheet(f"color:{t.ink};font-size:15px;font-weight:800;")
        sub_lbl = QLabel(sub)
        sub_lbl.setStyleSheet(f"color:{t.ink3};font-size:11px;")
        who_box.addWidget(who_lbl)
        who_box.addWidget(sub_lbl)
        h.addLayout(who_box)
        h.addStretch(1)
        put = QVBoxLayout()
        put.setSpacing(1)
        pk = QLabel(f"{'You' if cls == 'you' else 'They'} put up".upper())
        pk.setAlignment(Qt.AlignRight)
        pk.setStyleSheet(
            f"color:{t.ink3};font-size:9px;font-weight:700;letter-spacing:.4px;"
        )
        total = QLabel("$0.00")
        total.setFont(_mono_font(17, bold=True))
        total.setAlignment(Qt.AlignRight)
        put.addWidget(pk)
        put.addWidget(total)
        h.addLayout(put)
        if cls == "you":
            self._you_total = total
        else:
            self._them_sub = sub_lbl
            self._them_total = total
            self._margin_chip = self._make_margin_chip()
            h.addWidget(self._margin_chip)
        head.setStyleSheet(f"QFrame{{border-bottom:1px solid {t.line};}}")
        return head

    def _make_margin_chip(self) -> QFrame:
        t = theme.active()
        chip = QFrame()
        cl = QVBoxLayout(chip)
        cl.setContentsMargins(11, 5, 11, 5)
        cl.setSpacing(1)
        mk = QLabel("SALE MARGIN")
        mk.setAlignment(Qt.AlignRight)
        mk.setStyleSheet(
            f"color:{t.ink3};font-size:8.5px;font-weight:700;letter-spacing:.4px;"
            "background:transparent;border:none;"
        )
        self._margin_val = QLabel("—")
        self._margin_val.setFont(_mono_font(13, bold=True))
        self._margin_val.setAlignment(Qt.AlignRight)
        self._margin_val.setStyleSheet("background:transparent;border:none;")
        cl.addWidget(mk)
        cl.addWidget(self._margin_val)
        chip.setVisible(False)
        return chip

    def _build_you_side(self) -> QWidget:
        t = theme.active()
        panel = QFrame()
        panel.setObjectName("sideyou")
        panel.setStyleSheet(
            f"QFrame#sideyou{{background:{t.surface};border:1px solid {t.line};"
            f"border-top:3px solid {t.pos};border-radius:16px;}}"
        )
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._side_header("Y", "You", "what you put on the table", "you"))
        body = QVBoxLayout()
        body.setContentsMargins(15, 14, 15, 15)
        body.setSpacing(9)
        # Blank slot matching the Them side's autofill toolbar, so the two cash
        # blocks line up across the boxes.
        spacer = QWidget()
        spacer.setFixedHeight(_TOOLBAR_H)
        body.addWidget(spacer)
        body.addWidget(_cash_block("Cash you pay", self._cash_out, t.pos))
        self._give_list = QVBoxLayout()
        self._give_list.setSpacing(8)
        self._give_list.setContentsMargins(0, 0, 0, 0)
        body.addLayout(self._give_list)
        # The add button sits directly below the lowest card row.
        body.addWidget(_add_button("Add a card you're giving", self._add_give_row))
        body.addStretch(1)
        v.addLayout(body)
        return panel

    def _build_them_side(self) -> QWidget:
        t = theme.active()
        panel = QFrame()
        panel.setObjectName("sidethem")
        panel.setStyleSheet(
            f"QFrame#sidethem{{background:{t.surface};border:1px solid {t.line};"
            f"border-top:3px solid {t.accent};border-radius:16px;}}"
        )
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._side_header("T", "Them", "the counterparty", "them"))
        body = QVBoxLayout()
        body.setContentsMargins(15, 14, 15, 15)
        body.setSpacing(9)

        # Autofill / auto-detect at the very top of the box.
        self._auto_detect_cb = QCheckBox("Auto-detect")
        self._auto_detect_cb.setToolTip(
            "As you type a card (name, set, and/or number), search the local "
            "card database and pick a match to auto-fill its fields."
        )
        self._auto_detect_cb.setChecked(
            bool(config.get_pref("receive_auto_detect", True))
        )
        self._auto_detect_cb.toggled.connect(self._on_auto_detect_toggled)
        self._catalog_hint = QLabel("")
        self._catalog_hint.setStyleSheet(f"color:{t.warn};font-size:11px;")
        self._autofill_btn = QPushButton("  Autofill from photo")
        self._autofill_btn.setIcon(icons.icon("scan-line", t.ink2, 15))
        self._autofill_btn.setCursor(Qt.PointingHandCursor)
        self._autofill_btn.setStyleSheet(
            f"QPushButton{{border:1px solid {t.line2};border-radius:9px;"
            f"color:{t.ink2};padding:6px 11px;font-size:12px;font-weight:600;}}"
            f"QPushButton:hover{{border-color:{t.accent};color:{t.accent_ink};}}"
        )
        self._autofill_btn.clicked.connect(self._autofill_from_photo)
        tool = QHBoxLayout()
        tool.setContentsMargins(0, 0, 0, 0)
        tool.addWidget(self._autofill_btn)
        tool.addWidget(self._catalog_hint)
        tool.addStretch(1)
        tool.addWidget(self._auto_detect_cb)
        tool_w = QWidget()
        tool_w.setLayout(tool)
        tool_w.setFixedHeight(_TOOLBAR_H)
        body.addWidget(tool_w)

        body.addWidget(_cash_block("Cash they pay you", self._cash_in, t.accent))
        self._recv_list = QVBoxLayout()
        self._recv_list.setSpacing(8)
        self._recv_list.setContentsMargins(0, 0, 0, 0)
        body.addLayout(self._recv_list)
        # The add button sits directly below the lowest card row.
        body.addWidget(_add_button("Add a card they're giving", self._add_receive_row))
        body.addStretch(1)
        v.addLayout(body)
        return panel

    def _build_receipt(self) -> QWidget:
        t = theme.active()
        bar = QFrame()
        bar.setObjectName("receipt")
        bar.setStyleSheet(
            f"QFrame#receipt{{background:{t.surface};border-top:1px solid {t.line2};}}"
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 9, 14, 9)
        h.setSpacing(18)
        h.addStretch(1)
        self._warn = QLabel("")
        self._warn.setStyleSheet(f"color:{t.neg};font-size:12px;font-weight:600;")
        h.addWidget(self._warn)
        clear_btn = QPushButton("Clear")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(
            f"QPushButton{{border:1px solid {t.line2};border-radius:10px;"
            f"color:{t.ink2};padding:9px 16px;font-size:13px;font-weight:700;}}"
        )
        clear_btn.clicked.connect(self.clear)
        self._submit = QPushButton("Submit")
        self._submit.setObjectName("submit")
        self._submit.setCursor(Qt.PointingHandCursor)
        self._submit.setToolTip("Post this transaction (Ctrl+S)")
        self._submit.setStyleSheet(
            f"QPushButton#submit{{background:{t.accent};color:#0e0f13;border:none;"
            f"border-radius:10px;padding:9px 18px;font-size:13px;font-weight:800;}}"
            f"QPushButton#submit:disabled{{background:{t.surface2};color:{t.ink3};}}"
        )
        self._submit.clicked.connect(self.submit)
        h.addWidget(clear_btn)
        h.addWidget(self._submit)
        return bar

    def _on_counterparty_changed(self, text: str) -> None:
        if hasattr(self, "_them_sub"):
            self._them_sub.setText(text.strip() or "the counterparty")

    def _on_auto_detect_toggled(self, on: bool) -> None:
        config.set_pref("receive_auto_detect", on)
        self._update_catalog_hint()

    def _update_catalog_hint(self) -> None:
        empty = CardCatalogRepository(self._conn).count() == 0
        if self._auto_detect_cb.isChecked() and empty:
            self._catalog_hint.setText(
                "Card database is empty — run File ▸ Update card database"
            )
        else:
            self._catalog_hint.setText("")

    # -- add / remove (card-row widgets in each side's list) ---------------

    def _remove_row(self, kind: str, row) -> None:
        rows = self._give_rows if kind == "out" else self._receive_rows
        if row in rows:
            rows.remove(row)
            card = getattr(row, "_card_widget", None)
            if card is not None:
                card.setParent(None)
                card.deleteLater()
        self._update_summary()

    def _add_give_row(self) -> _GiveRow | None:
        sellable = self._inventory.sellable_items()
        if not sellable:
            QMessageBox.information(
                self, "Nothing to give", "No sellable cards on hand."
            )
            return None
        row = _GiveRow(self._conn, sellable, self._update_summary, sink=self._sink_you)
        row._card_widget = row.widget(lambda r=row: self._remove_row("out", r))
        self._give_list.addWidget(row._card_widget)
        self._give_rows.append(row)
        self._update_summary()
        return row

    def _add_receive_row(self) -> _ReceiveRow:
        row = _ReceiveRow(
            self._conn,
            lambda: self._auto_detect_cb.isChecked(),
            self._update_summary,
            on_pick=self._request_price,
            sink=self._sink_them,
        )
        row._card_widget = row.widget(lambda r=row: self._remove_row("in", r))
        self._recv_list.addWidget(row._card_widget)
        self._receive_rows.append(row)
        row.name.setFocus()
        self._update_summary()
        return row

    def _request_price(self, row, card) -> None:
        """After a catalog pick, fetch the card's current tcgcsv market price
        (off-thread) and fill Market — the same source the inventory uses."""
        if not card.set_name or not card.number:
            return
        # Japanese cards price against tcgcsv category 85, English against 3.
        category = 85 if getattr(card, "language", "EN") == "JP" else 3
        worker = _PriceLookupWorker(
            card.set_name,
            card.number,
            card.name,
            category=category,
            set_code=getattr(card, "set_code", None),
        )
        worker.done.connect(
            lambda result, r=row, w=worker: self._on_price_ready(r, w, result)
        )
        self._price_workers.append(worker)
        worker.start()

    def _on_price_ready(self, row, worker, result) -> None:
        if worker in self._price_workers:
            self._price_workers.remove(worker)
        if result is None or row not in self._receive_rows:
            return
        cents, url = result
        row.market.setValue(cents / 100)
        if url and not row.tcgplayer.text().strip():
            row.tcgplayer.setText(url)
        self._update_summary()

    # Test/programmatic API (used by the lifecycle tests).
    def add_outgoing(self, card: OutgoingCard, display_name: str = "") -> None:
        row = self._add_give_row()
        if row is not None:
            row.set_item(card.item_id, card.quantity)
            self._update_summary()

    def add_incoming(self, card: IncomingCard) -> None:
        row = self._add_receive_row()
        row.set_card(card)
        self._update_summary()

    # -- Recognition export -------------------------------------------------

    def has_content(self) -> bool:
        """True if the window holds anything worth confirming before a clear."""
        return bool(
            self._give_rows
            or self._receive_rows
            or self._cash_in.value()
            or self._cash_out.value()
            or self._counterparty.text().strip()
        )

    def import_recognized(self, cards) -> list[str]:
        """Fill a fresh deal from Recognition's labeled cards. Receive-side cards
        become They-Give rows (via the autofill pipeline); give-side cards are
        matched to owned sellable lots. Returns the display names of any give
        cards with no matching inventory lot (skipped)."""
        self.clear()
        sellable = self._inventory.sellable_items()
        unmatched: list[str] = []
        for card in cards:
            if getattr(card, "side", None) == "give":
                item_id = _match_sellable(sellable, card)
                if item_id is not None:
                    self.add_outgoing(
                        OutgoingCard(item_id=item_id, quantity=max(1, card.quantity))
                    )
                else:
                    unmatched.append(_recognized_name(card))
            else:  # receive, or listing/no-side → treat as incoming
                self._add_recognized_receive(card)
        self._update_summary()
        return unmatched

    def _add_recognized_receive(self, card) -> None:
        row = self._add_receive_row()
        catalog = (
            CardCatalogRepository(self._conn).get(card.card_id)
            if getattr(card, "card_id", None)
            else None
        )
        if catalog is not None:
            row.apply_catalog_card(catalog)
            self._request_price(row, catalog)  # inherit the autofill price lookup
        else:
            row.name.setText(card.name)
            if card.set_name:
                row.set.setText(card.set_name)
            if card.card_number:
                row.number.setText(card.card_number)
        if getattr(card, "is_graded", False):
            row.graded.setChecked(True)
            if card.grading_company:
                _select(row.company, card.grading_company)
            if card.grade:
                row.grade.setText(card.grade)
        elif card.condition:
            _select(row.condition, card.condition)
        row.qty.setValue(max(1, card.quantity))

    # -- image inference autofill (spec 06, Part 2) -------------------------

    def _ensure_pipeline(self):
        if self._inference_pipeline is None:
            from tcginventory.services.inference import build_pipeline

            self._inference_pipeline = build_pipeline(self._conn)
        return self._inference_pipeline

    def _autofill_from_photo(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose card scene photo(s)",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if not paths:
            return
        try:
            pipeline = self._ensure_pipeline()
        except Exception as exc:  # model/deps unavailable
            QMessageBox.warning(self, "Autofill unavailable", str(exc))
            return
        added = 0
        for path in paths:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                detections = pipeline(path)
            finally:
                QApplication.restoreOverrideCursor()
            if not detections:
                QMessageBox.information(
                    self, "No cards found", f"No cards detected in {path}."
                )
                continue
            dialog = AutofillDialog(path, detections, self._conn, self)
            if dialog.exec() != QDialog.Accepted:
                continue
            accepted_cards = dialog.result_cards()
            for accepted in accepted_cards:
                self._apply_accepted_receive(accepted)
                added += 1
            inference_write_back(
                self._inference_pipeline, path, "transaction", accepted_cards
            )
        if added:
            self._update_summary()

    def _apply_accepted_receive(self, accepted) -> None:
        row = self._add_receive_row()
        card = (
            CardCatalogRepository(self._conn).get(accepted.card_id)
            if accepted.card_id
            else None
        )
        if card is not None:
            row.apply_catalog_card(card)
            self._request_price(row, card)  # fill Market from tcgcsv, off-thread
        else:
            row.name.setText(accepted.name)
            if accepted.set_name:
                row.set.setText(accepted.set_name)
            if accepted.card_number:
                row.number.setText(accepted.card_number)
        row.qty.setValue(accepted.quantity)
        if accepted.review:
            row.review.setChecked(True)

    # -- request / summary --------------------------------------------------

    def build_request(self) -> TransactionRequest:
        outgoing = [c for c in (r.to_card() for r in self._give_rows) if c]
        incoming = [c for c in (r.to_card() for r in self._receive_rows) if c]
        return TransactionRequest(
            occurred_at=self._date.text().strip(),
            outgoing=outgoing,
            incoming=incoming,
            cash_in_cents=_cents(self._cash_in),
            cash_out_cents=_cents(self._cash_out),
            counterparty=self._counterparty.text().strip() or None,
            reference=self._reference.text().strip() or None,
        )

    _BADGE_COLORS = {"purchase": "info", "sale": "pos", "trade": "accent"}

    def _refresh_cascades(self) -> None:
        """Feed each side's cascade with the cards currently on that side."""
        self._cascade_you.set_cards(
            [c for c in (r._hover_card() for r in self._give_rows) if c]
        )
        self._cascade_them.set_cards(
            [c for c in (r._hover_card() for r in self._receive_rows) if c]
        )

    def _update_summary(self, *_args) -> None:
        t = theme.active()
        self._refresh_cascades()
        # Adding/removing rows can change the panel widths; re-lock the header
        # groups once the layout has settled.
        QTimer.singleShot(0, self._sync_header_widths)
        preview = self._service.preview(self.build_request())
        ttype = preview.inferred_type.value if preview.inferred_type else None
        self._badge.setText((ttype or "—").upper())
        self._badge.setStyleSheet(self._badge_text_style(ttype))
        self._badge_frame.setStyleSheet(self._badge_frame_style(ttype))
        self._submit.setText(f"Submit {ttype}" if ttype else "Submit")

        # each party's contribution + the running receipt totals
        self._you_total.setText(money(preview.consideration_given_cents))
        self._them_total.setText(money(preview.value_received_cents))
        self._r_stats["yougive"].setText(money(preview.consideration_given_cents))
        self._r_stats["theygive"].setText(money(preview.value_received_cents))
        net = preview.net_cash_cents
        net_lbl = self._r_stats["net"]
        net_lbl.setText(money(net))
        net_lbl.setStyleSheet(
            f"color:{t.pos if net > 0 else (t.neg if net < 0 else t.ink)};"
        )

        # sale margin (proceeds − cost basis of cards given up)
        if preview.proceeds_cents:
            pl = preview.realized_pl_cents
            pct = percent(100.0 * pl / preview.proceeds_cents)
            colr = t.pos if pl >= 0 else t.neg
            arrow = "▲ " if pl >= 0 else "▼ "
            self._margin_val.setText(f"{arrow}{money(pl)} · {pct}")
            self._margin_val.setStyleSheet(
                f"color:{colr};background:transparent;border:none;"
            )
            mbg = "rgba(131,209,90,.13)" if pl >= 0 else "rgba(255,106,90,.13)"
            self._margin_chip.setStyleSheet(
                f"QFrame{{background:{mbg};border:1px solid {colr};"
                "border-radius:10px;}"
            )
            self._margin_chip.setVisible(True)
            self._r_stats["margin"].setText(f"{money(pl)} {pct}")
            self._r_stats["margin"].setStyleSheet(f"color:{colr};")
        else:
            self._margin_chip.setVisible(False)
            self._r_stats["margin"].setText("—")
            self._r_stats["margin"].setStyleSheet(f"color:{t.ink3};")

        self._warn.setText("" if preview.feasible else "⚠ oversell — reduce quantity")
        self._submit.setEnabled(preview.inferred_type is not None and preview.feasible)

    def _badge_text_style(self, ttype: str | None) -> str:
        t = theme.active()
        color = getattr(t, self._BADGE_COLORS.get(ttype or "", "ink3"), None) or t.ink3
        return (
            f"color:{color};font-size:19px;font-weight:800;letter-spacing:.5px;"
            "background:transparent;border:none;"
        )

    def _badge_frame_style(self, ttype: str | None) -> str:
        t = theme.active()
        tone = self._BADGE_COLORS.get(ttype or "", None)
        rgb = {"info": "91,147,168", "pos": "131,209,90", "accent": "245,133,46"}
        if tone in rgb:
            c = rgb[tone]
            return (
                f"QFrame#typebadge{{background:rgba({c},.12);"
                f"border:1px solid rgba({c},.4);border-radius:11px;}}"
            )
        return (
            f"QFrame#typebadge{{background:{t.surface};border:1px solid {t.line2};"
            "border-radius:11px;}"
        )

    # -- submit / edit ------------------------------------------------------

    def _submit_via_shortcut(self) -> None:
        if self._submit.isEnabled():
            self.submit()

    def submit(self) -> None:
        request = self.build_request()
        try:
            if self._edit_id is not None:
                old_margin = self._reporting.transaction_margin_cents(self._edit_id)
                result = self._service.edit(self._edit_id, request)
                self._show_success(
                    result, edited_from=self._edit_id, old_margin=old_margin
                )
            else:
                result = self._service.post(request)
                self._show_success(result)
        except (ServiceError, ValueError) as exc:
            QMessageBox.warning(self, "Could not post", str(exc))
            return
        self.clear()
        self.posted.emit()

    _ITEMS_PER_COLUMN = 25

    def _show_success(self, result, edited_from=None, old_margin=None) -> None:
        header = [f"Recorded {result.transaction.type.value.upper()}."]
        if edited_from is not None:
            header.append(f"(edit of transaction {edited_from} — original voided)")

        items = []
        for lot in result.lots:
            item = self._service.items.get(lot.item_id)
            name = item.name if item else f"item {lot.item_id}"
            items.append(f"•  {name}: {money(lot.unit_cost_cents)} × {lot.quantity}")

        footer = []
        if result.realized_pl_cents:
            footer.append(
                (f"Margin on cards given up: {money(result.realized_pl_cents)}", "info")
            )
        if old_margin is not None and old_margin != result.realized_pl_cents:
            footer.append(
                (
                    f"Realized P/L changed: {money(old_margin)} → "
                    f"{money(result.realized_pl_cents)}",
                    "warn",
                )
            )
        self._success_dialog(header, items, footer)

    def _success_dialog(self, header, items, footer) -> None:
        """Themed confirmation dialog. The received-card list is laid out in
        columns of 25, wrapping to a new column so a big transaction never runs
        off-screen."""
        t = theme.active()
        dialog = QDialog(self)
        dialog.setWindowTitle("Transaction recorded")

        # Header in a surface2 band with a green "success" accent rail (no glyph
        # dependency — matches the History detail band motif).
        head = QFrame()
        head.setStyleSheet(
            f"QFrame{{background:{t.surface2}; border:1px solid {t.line};"
            f" border-left:3px solid {t.pos}; border-radius:10px;}}"
        )
        hv = QVBoxLayout(head)
        hv.setContentsMargins(13, 10, 13, 10)
        hv.setSpacing(2)
        title = QLabel(header[0])
        title.setStyleSheet(
            f"color:{t.ink}; font-size:15px; font-weight:700;"
            " border:none; background:transparent;"
        )
        hv.addWidget(title)
        for extra in header[1:]:
            sub = QLabel(extra)
            sub.setWordWrap(True)
            sub.setStyleSheet(
                f"color:{t.ink3}; font-size:12px; border:none; background:transparent;"
            )
            hv.addWidget(sub)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)
        layout.addWidget(head)

        if items:
            cap = QLabel("INFERRED COST OF EACH CARD RECEIVED")
            cap.setStyleSheet(
                f"color:{t.ink3}; font-size:10px; font-weight:700;"
                " letter-spacing:.06em;"
            )
            layout.addWidget(cap)
            columns = QFrame()
            columns.setStyleSheet(
                f"QFrame{{background:{t.surface2}; border:1px solid {t.line};"
                " border-radius:10px;}"
            )
            cols_layout = QHBoxLayout(columns)
            cols_layout.setContentsMargins(13, 11, 13, 11)
            cols_layout.setSpacing(26)
            for start in range(0, len(items), self._ITEMS_PER_COLUMN):
                chunk = items[start : start + self._ITEMS_PER_COLUMN]
                col = QLabel("\n".join(chunk))
                col.setTextInteractionFlags(Qt.TextSelectableByMouse)
                col.setStyleSheet(
                    f"color:{t.ink2}; font-size:12px;"
                    " border:none; background:transparent;"
                )
                cols_layout.addWidget(col, 0, Qt.AlignTop)
            cols_layout.addStretch(1)
            # Scroll if there are so many columns they'd exceed the screen width;
            # the viewport is themed so it doesn't render as a bare white box.
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.viewport().setStyleSheet(f"background:{t.bg};")
            scroll.setWidget(columns)
            layout.addWidget(scroll, 1)

        for line, tone in footer:
            lbl = QLabel(line)
            lbl.setWordWrap(True)
            color = t.warn if tone == "warn" else t.ink2
            weight = "font-weight:600;" if tone == "warn" else ""
            lbl.setStyleSheet(f"color:{color}; font-size:12.5px; {weight}")
            layout.addWidget(lbl)

        ok = QPushButton("Done")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(dialog.accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

        # Never exceed the visible screen; the scroll area handles overflow.
        avail = self.screen().availableGeometry()
        dialog.setMaximumSize(int(avail.width() * 0.9), int(avail.height() * 0.9))
        dialog.setMinimumWidth(380)
        dialog.exec()

    def clear(self) -> None:
        for row in (*self._give_rows, *self._receive_rows):
            card = getattr(row, "_card_widget", None)
            if card is not None:
                card.setParent(None)
                card.deleteLater()
        self._give_rows.clear()
        self._receive_rows.clear()
        self._cash_in.setValue(0.0)
        self._cash_out.setValue(0.0)
        self._counterparty.clear()
        self._reference.clear()
        self._edit_id = None
        self._edit_banner.setText("")
        self._update_summary()

    def refresh(self) -> None:
        self._update_summary()

    def set_deal_context(
        self, *, counterparty: str | None = None, reference: str | None = None
    ) -> None:
        """Prefill the counterparty / reference (e.g. when exporting an auction)."""
        if counterparty is not None:
            self._counterparty.setText(counterparty)
        if reference is not None:
            self._reference.setText(reference)
        self._update_summary()

    # -- edit pre-load ------------------------------------------------------

    def load_transaction(self, transaction_id: int) -> bool:
        """Pre-fill the form from an existing transaction for editing."""
        txns = TransactionRepository(self._conn)
        txn = txns.get(transaction_id)
        if txn is None or txn.status is TransactionStatus.VOIDED:
            QMessageBox.warning(
                self, "Cannot edit", "Transaction is missing or voided."
            )
            return False

        self.clear()
        self._edit_id = transaction_id
        self._edit_banner.setText(f"Editing transaction {transaction_id}")
        self._date.setText(txn.occurred_at[:10])
        self._counterparty.setText(txn.counterparty or "")
        self._reference.setText(txn.reference or "")
        if txn.cash_delta_cents >= 0:
            self._cash_in.setValue(txn.cash_delta_cents / 100)
        else:
            self._cash_out.setValue(-txn.cash_delta_cents / 100)

        items = ItemRepository(self._conn)
        lots = LotRepository(self._conn)
        for line in TransactionLineRepository(self._conn).list_for_transaction(txn.id):
            item = items.get(line.item_id)
            if line.direction is Direction.OUT:
                self.add_outgoing(
                    OutgoingCard(item_id=line.item_id, quantity=line.quantity)
                )
            elif item is not None:
                line_lots = lots.list_for_acquired_line(line.id)
                mv = line_lots[0].acquired_market_value_cents if line_lots else 0
                intent = line_lots[0].intent if line_lots else Intent.RESALE
                self.add_incoming(
                    IncomingCard(
                        item=item,
                        quantity=line.quantity,
                        market_value_cents=mv,
                        intent=intent,
                    )
                )
        return True
