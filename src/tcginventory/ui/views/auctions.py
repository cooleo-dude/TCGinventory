"""eBay Auctions screen — plan max bids for auctions you're watching.

Two stacked panes:

* **Watchlist (top)** — a row per watched auction: title, a link that opens the
  listing, the total card value, and a **max-bid breakdown** at 70 / 80 / 90 /
  100% of that value (net of shipping + tax). Click a row to load it into the
  editor; each row has a delete button.
* **Editor (bottom)** — the transaction-adjacent entry point. Enter a title +
  eBay URL, add the cards in the auction (catalog autofill fills set/number and
  auto-prices each card from ``pricehistory.db``, editable), and set a flat
  shipping cost and a sales-tax rate (default 6%). A live breakdown shows the
  four max bids as you type. Save upserts into the watchlist.

Watched auctions live in a separate, ephemeral ``auctions.db`` (see
:mod:`tcginventory.data.auctions`) — not the ledger. Nothing prunes them; the
user deletes entries at their discretion.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from PySide6.QtCore import (
    QDateTime,
    QEvent,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from tcginventory import config
from tcginventory.data.auctions import (
    Auction,
    AuctionCard,
    AuctionRepository,
    open_auctions,
)
from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.domain.enums import (
    Category,
    Condition,
    Edition,
    GradingCompany,
    HoloPattern,
    Intent,
    Language,
)
from tcginventory.domain.models import Item
from tcginventory.services import auctions as auction_math
from tcginventory.services import cardimages
from tcginventory.services.ebay import search_url
from tcginventory.services.inference import write_back as inference_write_back
from tcginventory.services.tcgplayer import product_url as tcgplayer_product_url
from tcginventory.services.transactions import IncomingCard
from tcginventory.ui import icons, theme
from tcginventory.ui.card_gallery import CardGallery
from tcginventory.ui.clipboard_images import clipboard_image_paths
from tcginventory.ui.formatting import money, percent
from tcginventory.ui.views._autofill_dialog import AutofillDialog
from tcginventory.ui.views._card_entry import CardEntryMixin, PriceLookupWorker

_RECOMMENDED_PCT = 80  # the "bid to here" rung highlighted on every ladder


def _auction_card_to_incoming(ac: AuctionCard) -> IncomingCard:
    """Translate a saved auction card into a Transaction incoming card.

    A **manually edited** market value is noted, marks the item for review (so
    the poll won't overwrite it), and — via the review flag — overwrites the
    item's current market price on post."""
    manual = ac.market_is_manual
    note = ac.notes
    if manual:
        stamp = (
            f"Market manually set to ${ac.market_value_cents / 100:,.2f} "
            "on auction export."
        )
        note = f"{note} {stamp}".strip() if note else stamp
    item = Item(
        name=ac.name,
        category=Category(ac.category or "single"),
        set_name=ac.set_name,
        set_code=ac.set_code,
        card_number=ac.number,
        language=Language(ac.language or "EN"),
        condition=(
            None
            if ac.is_graded
            else Condition(ac.condition)
            if ac.condition
            else Condition.NM
        ),
        holo_pattern=HoloPattern(ac.holo_pattern) if ac.holo_pattern else None,
        edition=Edition(ac.edition) if ac.edition else None,
        is_graded=ac.is_graded,
        grading_company=(
            GradingCompany(ac.grading_company) if ac.grading_company else None
        ),
        grade=ac.grade,
        cert_number=ac.cert_number,
        tcgplayer_url=ac.tcgplayer_url,
        ebay_url=ac.ebay_url,
        notes=note,
    )
    return IncomingCard(
        item=item,
        quantity=ac.quantity,
        market_value_cents=ac.market_value_cents,
        intent=Intent(ac.intent or "resale"),
        review=ac.review or manual,
    )


def _combo(enum_cls) -> QComboBox:
    """A drop-down of an enum's values (value stored as item data)."""
    box = QComboBox()
    for e in enum_cls:
        box.addItem(e.value, e.value)
    return box


def _select(combo: QComboBox, value: str | None) -> None:
    idx = combo.findData(value) if value is not None else -1
    if idx >= 0:
        combo.setCurrentIndex(idx)


def _value_of(field) -> str | None:
    """The ``.value`` of an enum field, or the string it already is.

    Recognition and the ledger hand over enums; a stored auction card holds the
    value strings. Both reach the same combos.
    """
    return getattr(field, "value", field)


# Card-row columns. Identity first (Name … #), then the three fields that decide
# *which printing* this is (Cond / Holo / Printing), then money, then the links.
_CARD_HEADERS = [
    "Name",
    "Set",
    "#",
    "Cond",
    "Holo",
    "Printing",
    "Market",
    "Qty",
    "TCG",
    "eBay",
    "",
]
_NAME_COL = 0
_PRINTING_COL = _CARD_HEADERS.index("Printing")
# Every column but Name is fixed; Name absorbs whatever is left so the table
# always fills its pane exactly — no dead space on the right, and no horizontal
# scrollbar stealing the row that holds the add button. The combo and button
# columns are sized from their own sizeHints (they elide or clip their text);
# the line edits and spin boxes are free to be narrower, since their content
# scrolls.
_CARD_WIDTHS = [0, 108, 52, 76, 56, 116, 80, 40, 56, 60, 28]
# The three identity columns share the spare width; every other column is fixed.
# Name used to absorb all of it alone, which left it around 345px on a maximised
# window — nearly triple the longest name anyone types — while Set truncated
# ordinary set names at 108px. The shares below come from what these fields
# actually hold, measured across the collection: 95% of names render under
# 122px, set names under 174px and numbers under 85px, but the long tail is far
# longer for Set ("Garchomp vs Charizard SP Deck Kit (Charizard)", 273px) and
# for # ("SWSH291/SWSH307", 127px) than for Name, so they take the bigger cuts.
#          column -> (floor, ceiling, share of the surplus)
_FLEX_COLS = {
    _CARD_HEADERS.index("Name"): (120, 260, 0.25),
    # Set has no ceiling: it's also where any leftover lands, and its content
    # runs longest, so extra room there is never wasted.
    _CARD_HEADERS.index("Set"): (108, None, 0.43),
    _CARD_HEADERS.index("#"): (52, 145, 0.32),
}
_SET_COL = _CARD_HEADERS.index("Set")
_NAME_MIN = _FLEX_COLS[_NAME_COL][0]  # the floor that keeps the row on screen

_UNSET = object()  # sentinel: history not supplied -> open the default DB
_THUMB_BORDER = theme.DARK.line2  # neutral thumbnail frame


class _IconButton(QPushButton):
    """A flat icon button that recolours its Lucide glyph on hover.

    QIcon can't be restyled through QSS pseudo-states, so we swap the rendered
    pixmap on enter/leave to move between the idle and hover colours.
    """

    def __init__(self, icon_name: str, idle: str, hover: str, size: int = 16) -> None:
        super().__init__()
        self._icon_name = icon_name
        self._idle = idle
        self._hover = hover
        self._px = size
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(size, size))
        self.setIcon(icons.icon(icon_name, idle, size))

    def enterEvent(self, event) -> None:
        self.setIcon(icons.icon(self._icon_name, self._hover, self._px))
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.setIcon(icons.icon(self._icon_name, self._idle, self._px))
        super().leaveEvent(event)


def _confirm_delete(parent, title: str, heading: str, body: str) -> bool:
    """A themed frameless confirm dialog with a danger primary. Returns True on
    confirm. Used for irreversible auction deletes."""
    t = theme.active()
    dlg = QDialog(parent)
    dlg.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
    dlg.setAttribute(Qt.WA_TranslucentBackground)
    dlg.setModal(True)

    outer = QVBoxLayout(dlg)
    outer.setContentsMargins(18, 18, 18, 18)  # room for the drop shadow
    card = QFrame()
    card.setObjectName("confirmCard")
    card.setStyleSheet(
        f"QFrame#confirmCard{{background:{t.surface};border:1px solid {t.line2};"
        f"border-radius:16px;}}"
    )
    shadow = QGraphicsDropShadowEffect(dlg)
    shadow.setBlurRadius(38)
    shadow.setOffset(0, 10)
    shadow.setColor(QColor(0, 0, 0, 150))
    card.setGraphicsEffect(shadow)
    outer.addWidget(card)

    v = QVBoxLayout(card)
    v.setContentsMargins(22, 20, 22, 18)
    v.setSpacing(13)

    top = QHBoxLayout()
    top.setSpacing(13)
    badge = QLabel()
    badge.setFixedSize(40, 40)
    badge.setAlignment(Qt.AlignCenter)
    badge.setPixmap(icons.pixmap("triangle-alert", t.neg, 20))
    badge.setStyleSheet(f"background:{t.neg_soft};border-radius:20px;")
    top.addWidget(badge, 0, Qt.AlignTop)
    text_col = QVBoxLayout()
    text_col.setSpacing(4)
    h = QLabel(heading)
    h.setStyleSheet(f"color:{t.ink};font-size:15px;font-weight:700;")
    h.setWordWrap(True)
    msg = QLabel(body)
    msg.setStyleSheet(f"color:{t.ink2};font-size:12px;")
    msg.setWordWrap(True)
    text_col.addWidget(h)
    text_col.addWidget(msg)
    top.addLayout(text_col, 1)
    v.addLayout(top)

    btns = QHBoxLayout()
    btns.setSpacing(9)
    btns.addStretch(1)
    cancel = QPushButton("Cancel")
    cancel.setCursor(Qt.PointingHandCursor)
    cancel.setStyleSheet(
        f"QPushButton{{background:transparent;border:1px solid {t.line2};"
        f"border-radius:9px;color:{t.ink2};padding:8px 16px;font-size:12px;"
        f"font-weight:600;}}QPushButton:hover{{border-color:{t.ink3};"
        f"color:{t.ink};}}"
    )
    cancel.clicked.connect(dlg.reject)
    confirm = QPushButton("Delete")
    confirm.setCursor(Qt.PointingHandCursor)
    confirm.setStyleSheet(
        f"QPushButton{{background:{t.neg};border:none;border-radius:9px;"
        f"color:{t.accent_on};padding:8px 18px;font-size:12px;font-weight:700;}}"
        f"QPushButton:hover{{background:{t.neg_soft};color:{t.neg};}}"
    )
    confirm.clicked.connect(dlg.accept)
    btns.addWidget(cancel)
    btns.addWidget(confirm)
    v.addLayout(btns)

    dlg.setWindowTitle(title)
    return dlg.exec() == QDialog.Accepted


def _is_ended(ends_at: str | None, now: datetime) -> bool:
    """True when ``ends_at`` (ISO local) is set and already in the past."""
    if not ends_at:
        return False
    try:
        return (datetime.fromisoformat(ends_at) - now).total_seconds() <= 0
    except ValueError:
        return False


def _countdown_text(ends_at: str | None, now: datetime) -> str:
    """Human countdown to ``ends_at`` (ISO local): "ENDED" once past, "—" when
    unset/unparseable, else "Xd HH:MM:SS" (days dropped when zero)."""
    if not ends_at:
        return "—"
    try:
        end = datetime.fromisoformat(ends_at)
    except ValueError:
        return "—"
    secs = int((end - now).total_seconds())
    if secs <= 0:
        return "ENDED"
    days, rem = divmod(secs, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02}:{minutes:02}:{seconds:02}"
    return f"{hours:02}:{minutes:02}:{seconds:02}"


class _GallerySink:
    """Hover-preview sink for the auction editor's gallery.

    Hovering a card row (or an autofill match) focuses that single card in the
    gallery; leaving reverts to the full gallery of the auction's cards — the
    same show_single/show_gallery toggle the Price Lookup window uses.
    """

    def __init__(self, gallery: CardGallery) -> None:
        self._gallery = gallery

    def show_card(self, card_id, url) -> None:
        if card_id and url:
            self._gallery.show_single(card_id, url)

    def clear(self) -> None:
        self._gallery.show_gallery()


def _money_spin() -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setPrefix("$")
    spin.setDecimals(2)
    spin.setMaximum(10_000_000.0)
    spin.setGroupSeparatorShown(True)
    spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
    return spin


class _AuctionCardRow(CardEntryMixin):
    """Inline editor row for one card bundled in an auction.

    The Name field autocompletes from the local catalog; picking a match fills
    set/number/language and triggers a price lookup that fills Market.
    """

    def __init__(self, conn, is_auto, on_change, on_pick=None, sink=None) -> None:
        self._conn = conn
        self._is_auto = is_auto  # () -> bool : is "Auto Detect Card" on?
        self._on_change = on_change
        self._on_pick = on_pick  # (row, CatalogCard) -> None : after a pick
        self._sink = sink  # card-image preview target
        self._tcgplayer_url: str | None = None
        self._language: str = "EN"

        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Charizard Obsidian Flames 125")
        self._init_catalog_search()  # shared catalog autocomplete + hover preview

        # Minimal field set (+ condition) — the rest of the card details are
        # entered in the Transaction window after export.
        self.set = QLineEdit()
        self.number = QLineEdit()
        self.condition = _combo(Condition)
        _select(self.condition, Condition.NM.value)
        self.condition.currentIndexChanged.connect(lambda *_: on_change())
        # Holo sits beside Cond and Printing: together they decide which printing
        # of the card this row is, and it carries through to the exported item.
        self.holo = _combo(HoloPattern)
        _select(self.holo, HoloPattern.NON_HOLO.value)
        self.holo.currentIndexChanged.connect(lambda *_: on_change())
        self.init_treatments()  # narrow to the printings the card comes in
        self.market = _money_spin()
        self.market.valueChanged.connect(self._on_market_changed)
        self.qty = QSpinBox()
        self.qty.setMinimum(1)
        self.qty.setMaximum(9999)
        self.qty.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.qty.valueChanged.connect(lambda *_: on_change())
        # TCGplayer link button — enabled once a product URL is resolved. Short
        # labels (the header carries the rest) so the columns stay narrow enough
        # for Name to have real room; the tooltips say what they open.
        self.tcg_btn = QPushButton("TCG")
        self.tcg_btn.setEnabled(False)
        self.tcg_btn.setToolTip("Open this card on TCGplayer")
        self.tcg_btn.clicked.connect(self._open_tcgplayer)
        # eBay button — a derived sold-listings search from the card identity
        # (no stored URL needed); enabled as soon as there's a name.
        self.ebay_btn = QPushButton("eBay")
        self.ebay_btn.setEnabled(False)
        self.ebay_btn.setToolTip("Search eBay sold listings for this card")
        self.ebay_btn.clicked.connect(self._open_ebay)
        self.name.textChanged.connect(
            lambda *_: self.ebay_btn.setEnabled(bool(self.name.text().strip()))
        )
        # Printing picker (shared standard): shows in its own column only when a
        # card has >1 tcgplayer product (base + stamped variants).
        self._init_printing_picker()
        # Track whether Market was hand-edited away from the auto-fetched price,
        # so export can note it, overwrite the item price, and mark for review.
        self._auto_market_cents: int | None = None
        self._market_is_manual = False
        self._setting_market = False

    def widgets(self) -> list[QWidget]:
        """In column order — the table places these by index (see _CARD_HEADERS)."""
        return [
            self.name,
            self.set,
            self.number,
            self.condition,
            self.holo,
            self.printing,
            self.market,
            self.qty,
            self.tcg_btn,
            self.ebay_btn,
        ]

    def _apply_variant(self, variant) -> None:
        self.set_market_cents(variant.price_cents)
        self.set_tcgplayer_url(variant.tcgplayer_url)
        # A stamped printing may offer different foils than the base one.
        self.offer_treatments()

    def _on_market_changed(self, *_args) -> None:
        # A user edit (not our own programmatic set) that differs from the last
        # auto-fetched price counts as a manual override.
        if not self._setting_market:
            cents = round(self.market.value() * 100)
            self._market_is_manual = (
                self._auto_market_cents is None or cents != self._auto_market_cents
            )
        self._on_change()

    def _open_tcgplayer(self) -> None:
        # Layer the card's condition/printing/language onto the product link.
        card = self.to_card()
        url = tcgplayer_product_url(card) if card else None
        url = url or self._tcgplayer_url
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _open_ebay(self) -> None:
        name = self.name.text().strip()
        if not name:
            return
        url = search_url(
            name,
            self.set.text().strip(),
            self.number.text().strip(),
            self.condition.currentData(),
        )
        QDesktopServices.openUrl(QUrl(url))

    @property
    def market_is_manual(self) -> bool:
        return self._market_is_manual

    def set_tcgplayer_url(self, url: str | None) -> None:
        self._tcgplayer_url = url or None
        self.tcg_btn.setEnabled(bool(self._tcgplayer_url))
        self.tcg_btn.setToolTip(
            self._tcgplayer_url or "No TCGplayer link for this card"
        )

    # -- catalog autofill ---------------------------------------------------
    # Search, hover-preview, and pick-match behaviour comes from CardEntryMixin.

    def apply_catalog_card(self, card) -> None:
        self._language = getattr(card, "language", None) or "EN"
        self.set_tcgplayer_url(None)  # cleared until the price lookup resolves it
        self._apply_catalog_common(card)  # identity + name/set/number + preview
        self._on_change()

    # -- (de)serialise ------------------------------------------------------

    def set_market_cents(self, cents: int) -> None:
        """Programmatically fill Market from the auto price (not a manual edit)."""
        self._setting_market = True
        self.market.setValue(cents / 100)
        self._setting_market = False
        self._auto_market_cents = cents
        self._market_is_manual = False

    def set_card(self, card: AuctionCard) -> None:
        self._card_id = card.card_id
        self._image_url = card.image_url
        self._language = card.language or "EN"
        self.set_tcgplayer_url(card.tcgplayer_url)
        self.name.setText(card.name)
        self.set.setText(card.set_name or "")
        self.number.setText(card.number or "")
        if card.condition:
            _select(self.condition, card.condition)
        if card.holo_pattern:
            _select(self.holo, card.holo_pattern)
        self._setting_market = True
        self.market.setValue(card.market_value_cents / 100)
        self._setting_market = False
        self._market_is_manual = card.market_is_manual
        self._auto_market_cents = (
            None if card.market_is_manual else card.market_value_cents
        )
        self.qty.setValue(card.quantity)

    def card_id(self) -> str | None:
        return self._card_id

    def image_url(self) -> str | None:
        return self._image_url

    def value_cents(self) -> int:
        return round(self.market.value() * 100) * self.qty.value()

    def to_card(self) -> AuctionCard | None:
        name = self.name.text().strip()
        if not name:
            return None
        return AuctionCard(
            name=name,
            card_id=self._card_id,
            set_name=self.set.text().strip() or None,
            number=self.number.text().strip() or None,
            language=self._language,
            condition=self.condition.currentData(),
            holo_pattern=self.holo.currentData(),
            quantity=self.qty.value(),
            market_value_cents=round(self.market.value() * 100),
            image_url=self._image_url,
            tcgplayer_url=self._tcgplayer_url,
            market_is_manual=self._market_is_manual,
        )


class _ThumbStack(QWidget):
    """A small overlapping stack of card thumbnails (+ an "×N" overflow chip)."""

    _W, _H, _STEP = 38, 53, 25

    def __init__(self, card_ids: list[str], extra: str | None, parent=None) -> None:
        super().__init__(parent)
        self._pixmaps: list[QPixmap | None] = []
        for cid in card_ids[:3]:
            pm = None
            if cid:
                path = cardimages.cache_path(cid)
                if path.exists():
                    src = QPixmap(str(path))
                    pm = None if src.isNull() else src
            self._pixmaps.append(pm)
        self._extra = extra
        slots = max(1, len(self._pixmaps)) + (1 if extra else 0)
        self.setFixedSize(self._W + (slots - 1) * self._STEP, self._H)

    def paintEvent(self, _event) -> None:
        t = theme.active()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        x = 0
        slots = self._pixmaps or [None]
        for pm in slots:
            rect = QRectF(x, 0, self._W, self._H)
            ring = QRectF(x - 1, -1, self._W + 2, self._H + 2)
            ring_path = QPainterPath()
            ring_path.addRoundedRect(ring, 6, 6)
            p.fillPath(ring_path, QColor(t.surface))
            path = QPainterPath()
            path.addRoundedRect(rect, 5, 5)
            if pm is not None:
                p.save()
                p.setClipPath(path)
                scaled = pm.scaled(
                    self._W,
                    self._H,
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation,
                )
                p.drawPixmap(int(x), 0, scaled)
                p.restore()
            else:
                grad = QLinearGradient(x, 0, x, self._H)
                grad.setColorAt(0, QColor("#4a2f1c"))
                grad.setColorAt(1, QColor("#241a12"))
                p.fillPath(path, QBrush(grad))
            x += self._STEP
        if self._extra:
            rect = QRectF(x, 0, self._W, self._H)
            ring = QRectF(x - 1, -1, self._W + 2, self._H + 2)
            rp = QPainterPath()
            rp.addRoundedRect(ring, 6, 6)
            p.fillPath(rp, QColor(t.surface))
            path = QPainterPath()
            path.addRoundedRect(rect, 5, 5)
            p.fillPath(path, QColor(t.surface2))
            p.setPen(QColor(t.ink3))
            f = QFont(self.font())
            f.setPointSizeF(9.5)
            f.setBold(True)
            p.setFont(f)
            p.drawText(rect, int(Qt.AlignCenter), self._extra)
        p.end()


class _BidLadder(QWidget):
    """The max-bid ladder: four rungs (70/80/90/100%) as labelled bars. The
    recommended rung is accent, the 100% ceiling is brick-red."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bids: dict[int, int] = {}
        self.setFixedHeight(126)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_bids(self, bids: dict[int, int]) -> None:
        self._bids = dict(bids)
        self.update()

    def paintEvent(self, _event) -> None:
        t = theme.active()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        box = QRectF(0.5, 0.5, w - 1, h - 1)
        path = QPainterPath()
        path.addRoundedRect(box, 10, 10)
        p.fillPath(path, QColor(t.zebra))
        p.setPen(QColor(t.line))
        p.drawPath(path)

        pad = 12
        small = QFont(self.font())
        small.setPointSizeF(8.0)
        small.setBold(True)
        p.setFont(small)
        p.setPen(QColor(t.ink3))
        p.drawText(QRectF(pad, 8, w - 2 * pad, 14), int(Qt.AlignLeft), "MAX BID LADDER")
        p.drawText(
            QRectF(pad, 8, w - 2 * pad, 14),
            int(Qt.AlignRight),
            "all-in cost as % of value",
        )

        pcts = sorted(self._bids)
        if not pcts:
            p.end()
            return
        ceiling = max(1, self._bids.get(100, max(self._bids.values())))
        top = 30
        rung_h = (h - top - 8) / len(pcts)
        track_x = pad + 40
        track_r = w - pad - 82
        mono = QFont(theme.mono_family())
        for i, pct in enumerate(pcts):
            bid = self._bids[pct]
            cy = top + rung_h * i + rung_h / 2
            is_rec = pct == _RECOMMENDED_PCT
            is_ceil = pct == 100
            if is_rec:
                hl = QRectF(pad - 4, cy - rung_h / 2 + 1, w - 2 * pad + 8, rung_h - 2)
                hp = QPainterPath()
                hp.addRoundedRect(hl, 6, 6)
                wash = QColor(t.accent)
                wash.setAlpha(20)
                p.fillPath(hp, wash)
            # pct label
            mono.setPointSizeF(9.0)
            mono.setBold(is_rec)
            p.setFont(mono)
            p.setPen(QColor(t.accent_ink if is_rec else t.ink3))
            p.drawText(
                QRectF(pad, cy - 8, 36, 16),
                int(Qt.AlignLeft | Qt.AlignVCenter),
                f"{pct}%",
            )
            # track + fill
            track = QRectF(track_x, cy - 4, track_r - track_x, 8)
            tp = QPainterPath()
            tp.addRoundedRect(track, 4, 4)
            p.fillPath(tp, QColor(t.surface2))
            frac = max(0.06, min(1.0, bid / ceiling))
            fill = QRectF(track_x, cy - 4, (track_r - track_x) * frac, 8)
            fp = QPainterPath()
            fp.addRoundedRect(fill, 4, 4)
            if is_ceil:
                col = QColor(t.accent2)
            elif is_rec:
                col = QColor(t.accent)
            else:
                col = QColor(t.pos)
            p.fillPath(fp, col)
            # amount
            mono.setPointSizeF(10.0)
            mono.setBold(is_rec)
            p.setFont(mono)
            p.setPen(QColor(t.ink if is_rec else t.ink2))
            p.drawText(
                QRectF(track_r + 6, cy - 8, w - pad - track_r - 6, 16),
                int(Qt.AlignRight | Qt.AlignVCenter),
                money(bid),
            )
        p.end()


class _AuctionCard(QFrame):
    """One watched auction rendered as a card (thumbnails, countdown, value, and
    the max-bid ladder), with inline actions. Clicking the body loads it into the
    editor; hovering previews its cards in the gallery."""

    def __init__(self, view: AuctionsView, auction: Auction) -> None:
        super().__init__()
        self._view = view
        self._auction = auction
        self.auction_id = auction.id
        self.setObjectName("auc")
        self.setFixedWidth(432)
        self.setCursor(Qt.PointingHandCursor)
        t = theme.active()

        v = QVBoxLayout(self)
        v.setContentsMargins(15, 14, 15, 12)
        v.setSpacing(11)

        # -- header: thumbnails · title/count · countdown
        ids = [c.card_id for c in auction.cards]
        n_cards = len(auction.cards)
        total_qty = sum(c.quantity for c in auction.cards)
        if n_cards == 1 and total_qty > 1:
            extra = f"×{total_qty}"
        elif n_cards > 3:
            extra = f"+{n_cards - 3}"
        else:
            extra = None
        arow = QHBoxLayout()
        arow.setSpacing(12)
        arow.addWidget(_ThumbStack(ids, extra), 0, Qt.AlignTop)

        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        self._title_lbl = QLabel(auction.title or "(untitled)")
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setStyleSheet(f"color:{t.ink};font-weight:700;font-size:14px;")
        meta = QLabel(f"{total_qty} card{'s' if total_qty != 1 else ''}")
        meta.setStyleSheet(f"color:{t.ink3};font-size:11.5px;")
        title_box.addWidget(self._title_lbl)
        title_box.addWidget(meta)
        arow.addLayout(title_box, 1)

        cd_box = QVBoxLayout()
        cd_box.setSpacing(2)
        self._clk_lbl = QLabel("—")
        self._clk_lbl.setFont(_mono_font(15, bold=True))
        self._clk_lbl.setAlignment(Qt.AlignRight)
        self._cd_lbl = QLabel("")
        self._cd_lbl.setStyleSheet(
            f"color:{t.ink3};font-size:9.5px;font-weight:700;letter-spacing:.5px;"
        )
        self._cd_lbl.setAlignment(Qt.AlignRight)
        cd_box.addWidget(self._clk_lbl)
        cd_box.addWidget(self._cd_lbl)
        arow.addLayout(cd_box, 0)
        v.addLayout(arow)

        # -- value row
        val_row = QHBoxLayout()
        vk = QLabel("CARD VALUE")
        vk.setStyleSheet(
            f"color:{t.ink3};font-size:10px;font-weight:700;letter-spacing:.5px;"
        )
        self._value_lbl = QLabel(money(auction.value_cents))
        self._value_lbl.setFont(_mono_font(14, bold=True))
        self._value_lbl.setStyleSheet(f"color:{t.ink};")
        terms = QLabel(
            f"ship {money(auction.shipping_cents)} · "
            f"tax {percent(auction.tax_rate_bps / 100)}"
        )
        terms.setStyleSheet(f"color:{t.ink3};font-size:11px;")
        val_row.addWidget(vk)
        val_row.addSpacing(8)
        val_row.addWidget(self._value_lbl)
        val_row.addStretch(1)
        val_row.addWidget(terms)
        v.addLayout(val_row)

        # -- bid ladder
        ladder = _BidLadder()
        ladder.set_bids(
            auction_math.breakdown(
                auction.value_cents, auction.shipping_cents, auction.tax_rate_bps
            ).bids
        )
        v.addWidget(ladder)

        # -- actions
        acts = QHBoxLayout()
        acts.setSpacing(7)
        open_btn = self._link_button("Open ↗", primary=True)
        open_btn.setEnabled(bool(auction.url))
        open_btn.setToolTip(auction.url or "No link")
        if auction.url:
            open_btn.clicked.connect(
                lambda _=False, u=auction.url: QDesktopServices.openUrl(QUrl(u))
            )
        export_btn = self._link_button("Export →")
        export_btn.setToolTip("Send these cards to the Transaction window")
        export_btn.clicked.connect(lambda _=False, a=auction: view._export_auction(a))
        acts.addWidget(open_btn)
        acts.addWidget(export_btn)
        acts.addStretch(1)
        refresh = self._icon_button(
            "rotate-cw", "Refresh card values from current prices"
        )
        refresh.clicked.connect(lambda _=False, a=auction: view._refresh_values(a))
        archive = self._link_button("Unarchive" if auction.archived else "Archive")
        archive.setToolTip("Save this auction for later (hidden from the watchlist)")
        archive.clicked.connect(lambda _=False, a=auction: view._toggle_archived(a))
        delete = self._icon_button("trash-2", "Delete this auction", danger=True)
        delete.clicked.connect(lambda _=False, a=auction: view._delete(a))
        acts.addWidget(refresh)
        acts.addWidget(archive)
        acts.addWidget(delete)
        top_line = QFrame()
        top_line.setFixedHeight(1)
        top_line.setStyleSheet(f"background:{t.line};")
        v.addWidget(top_line)
        v.addLayout(acts)

        self.update_countdown(datetime.now())

    # -- styled buttons ----------------------------------------------------

    def _link_button(self, text: str, primary: bool = False) -> QPushButton:
        t = theme.active()
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setObjectName("lkp" if primary else "lk")
        if primary:
            b.setStyleSheet(
                f"QPushButton#lkp{{background:{t.accent_soft};border:1px solid "
                f"{t.accent};border-radius:8px;color:{t.accent_ink};padding:5px "
                f"11px;font-size:11.5px;font-weight:600;}}"
                f"QPushButton#lkp:disabled{{color:{t.ink3};border-color:{t.line2};"
                f"background:transparent;}}"
            )
        else:
            b.setStyleSheet(
                f"QPushButton#lk{{background:transparent;border:1px solid "
                f"{t.line2};border-radius:8px;color:{t.ink2};padding:5px 11px;"
                f"font-size:11.5px;font-weight:600;}}"
                f"QPushButton#lk:hover{{border-color:{t.accent};"
                f"color:{t.accent_ink};}}"
            )
        return b

    def _icon_button(
        self, icon_name: str, tip: str, danger: bool = False
    ) -> QPushButton:
        t = theme.active()
        hover = t.neg if danger else t.ink
        b = _IconButton(icon_name, t.ink3, hover, size=16)
        b.setToolTip(tip)
        b.setFixedSize(30, 30)
        b.setObjectName("icd" if danger else "icb")
        b.setStyleSheet(
            f"QPushButton#{b.objectName()}{{background:transparent;border:none;"
            f"border-radius:8px;}}"
            f"QPushButton#{b.objectName()}:hover{{background:{t.surface2};}}"
        )
        return b

    # -- test / update hooks -----------------------------------------------

    def title_text(self) -> str:
        return self._title_lbl.text()

    def value_text(self) -> str:
        return self._value_lbl.text()

    def countdown_text(self) -> str:
        return self._clk_lbl.text()

    def update_countdown(self, now: datetime) -> None:
        text = _countdown_text(self._auction.ends_at, now)
        self._clk_lbl.setText(text)
        t = theme.active()
        ends = self._auction.ends_at
        if not ends or text == "—":
            urgency, lbl, colr = "ok", "no end time", t.ink3
        elif text == "ENDED":
            urgency, lbl, colr = "ended", "ended", t.ink3
        else:
            secs = 0
            try:
                secs = int((datetime.fromisoformat(ends) - now).total_seconds())
            except ValueError:
                secs = 10**9
            if secs < 3600:
                urgency, lbl, colr = "urgent", "ending now", t.neg
            elif secs < 86_400:
                urgency, lbl, colr = "soon", "ending soon", t.warn
            else:
                urgency, lbl, colr = "ok", "plenty of time", t.pos
        self._clk_lbl.setStyleSheet(
            f"color:{colr};" + ("font-size:13px;" if urgency == "ended" else "")
        )
        self._cd_lbl.setText(lbl.upper())
        rail = {"urgent": t.neg, "soon": t.warn}.get(urgency, t.line2)
        border = {"urgent": t.neg, "soon": t.warn}.get(urgency, t.line)
        self.setStyleSheet(
            f"QFrame#auc{{background:{t.surface};border:1px solid {border};"
            f"border-left:3px solid {rail};border-radius:14px;}}"
        )

    # -- interaction -------------------------------------------------------

    def enterEvent(self, _event) -> None:  # noqa: N802
        self._view._on_card_hover(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.auction_id is not None:
            self._view._load_auction(self.auction_id)
        super().mousePressEvent(event)


def _mono_font(size: float, bold: bool = False) -> QFont:
    f = QFont(theme.mono_family())
    f.setPointSizeF(size)
    f.setBold(bold)
    return f


class AuctionsView(QWidget):
    """The eBay Auctions screen (watchlist + max-bid editor)."""

    # Emitted with (title, [IncomingCard]) to prefill the Transaction window.
    export_to_transaction = Signal(object)

    def __init__(
        self,
        conn: sqlite3.Connection,
        auctions_conn: sqlite3.Connection | None = None,
        history=_UNSET,
    ) -> None:
        super().__init__()
        self._conn = conn  # main DB (catalog search)
        self._auctions = auctions_conn if auctions_conn is not None else open_auctions()
        self._repo = AuctionRepository(self._auctions)
        # ``history`` supplied (incl. None) is honoured; omitted -> open default.
        self._history = _safe_open_history() if history is _UNSET else history

        self._rows: list[_AuctionCardRow] = []
        self._price_workers: list[PriceLookupWorker] = []
        self._edit_id: int | None = None
        self._inference_pipeline = None  # lazily built (image -> [Detection])
        # Owned by the view, so closing it cancels a pending fit. A bare
        # QTimer.singleShot outlives its target and fires against widgets Qt
        # has already deleted; it also coalesces a burst of resizes into one.
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_split_to_cards)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._on_view_resized)
        self._ended_ids: set[int] = set()  # which auctions were ended last refresh
        self._cards: list[_AuctionCard] = []  # all auction cards, in order
        self._page = 0  # current watchlist page (one row of cards per page)
        self._shown_key: tuple | None = None  # (page, per_page, count) rendered
        self._hover_card_id: int | None = None
        # Multi-card display (same widget/logic as the Price Lookup window):
        # shows every card in the auction, or a single focused card on hover.
        self._gallery = CardGallery(
            empty_hint="Pick cards from the catalog to see them here"
        )
        self._row_sink = _GallerySink(self._gallery)

        body = QSplitter(Qt.Vertical)
        body.addWidget(self._build_watchlist())
        editor_scroll = QScrollArea()
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setFrameShape(QFrame.NoFrame)
        editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        editor_scroll.setWidget(self._build_editor())
        body.addWidget(editor_scroll)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)
        body.setChildrenCollapsible(False)
        body.setSizes([520, 380])  # first guess; refined to hug the card grid
        # The default split hugs the bottom of the last auction-card row (the
        # composer takes the rest), but stops auto-fitting once the user drags.
        self._body = body
        self._split_user_set = False
        self._fitting = False
        body.splitterMoved.connect(self._on_splitter_moved)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addLayout(self._build_header())
        layout.addLayout(self._build_toolbar())
        layout.addWidget(body, 1)

        # Once-a-second timer drives the live countdowns (editor label + each
        # auction card). Always running so a card flips to "ENDED" on its own,
        # with no refresh/navigation needed.
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()

        self.refresh()
        self._update_breakdown()
        self._refresh_gallery()

    def _on_tick(self) -> None:
        now = datetime.now()
        self._countdown.setText(_countdown_text(self._ends_iso(), now))
        ended: set[int] = set()
        for card in self._cards:
            card.update_countdown(now)
            if _is_ended(card._auction.ends_at, now) and card.auction_id is not None:
                ended.add(card.auction_id)
        # An auction just crossed into "ENDED" → re-sort so it sinks to the
        # bottom (only rebuilds when the ended set actually changes).
        if ended != self._ended_ids:
            self.refresh()

    # -- header + toolbar ---------------------------------------------------

    def _build_header(self) -> QHBoxLayout:
        t = theme.active()
        row = QHBoxLayout()
        h1 = QLabel("Auctions")
        h1.setStyleSheet(f"font-size:20px;font-weight:800;color:{t.ink};")
        row.addWidget(h1)
        row.addStretch(1)
        self._tiles: dict[str, QLabel] = {}
        for key, label, hero in (
            ("watching", "Watching", False),
            ("value", "Combined value", True),
            ("soon", "Ending < 1h", False),
            ("archived", "Archived", False),
        ):
            tile = QFrame()
            tile.setObjectName("tile")
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(13, 7, 15, 8)
            tl.setSpacing(2)
            cap = QLabel(label.upper())
            cap.setStyleSheet(
                f"color:{t.ink3};font-size:10px;font-weight:700;letter-spacing:.5px;"
                "background:transparent;border:none;"
            )
            val = QLabel("—")
            val.setFont(_mono_font(16, bold=True))
            val.setStyleSheet(f"color:{t.ink};background:transparent;border:none;")
            tl.addWidget(cap)
            tl.addWidget(val)
            rail = f"border-left:3px solid {t.accent};" if hero else ""
            tile.setStyleSheet(
                f"QFrame#tile{{background:{t.surface};border:1px solid {t.line};"
                f"border-radius:11px;{rail}}}"
            )
            self._tiles[key] = val
            row.addWidget(tile)
        return row

    def _build_toolbar(self) -> QHBoxLayout:
        t = theme.active()
        row = QHBoxLayout()
        row.setSpacing(10)
        # Watching / Archived segmented control (backed by a hidden checkbox so
        # the archive state stays a single source of truth).
        self._show_archived = QCheckBox()
        self._show_archived.setVisible(False)
        self._show_archived.toggled.connect(lambda *_: self._sync_segment())
        self._show_archived.toggled.connect(lambda *_: self.refresh())
        seg = QWidget()
        seg.setObjectName("segwrap")
        seg.setStyleSheet(
            f"QWidget#segwrap{{background:{t.surface};border:1px solid {t.line2};"
            f"border-radius:9px;}}"
            f"QWidget#segwrap QPushButton{{background:transparent;border:none;"
            f"color:{t.ink2};padding:5px 13px;border-radius:6px;font-weight:600;"
            f"font-size:12px;}}"
            f"QWidget#segwrap QPushButton:checked{{background:{t.accent_soft};"
            f"color:{t.accent_ink};}}"
        )
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(3, 3, 3, 3)
        sl.setSpacing(2)
        self._seg_group = QButtonGroup(self)
        self._seg_group.setExclusive(True)
        self._seg_watch = QPushButton("Watching")
        self._seg_arch = QPushButton("Archived")
        for button, archived in ((self._seg_watch, False), (self._seg_arch, True)):
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(
                lambda _=False, a=archived: self._show_archived.setChecked(a)
            )
            self._seg_group.addButton(button)
            sl.addWidget(button)
        self._seg_watch.setChecked(True)
        row.addWidget(seg)
        sort = QLabel("Sorted by ending soonest")
        sort.setStyleSheet(f"color:{t.ink3};font-size:12px;")
        row.addWidget(sort)
        row.addStretch(1)
        plan = QPushButton("  Plan a bid")
        plan.setObjectName("plan")
        plan.setCursor(Qt.PointingHandCursor)
        plan.setIcon(icons.icon("plus", "#0e0f13", 16))
        plan.setToolTip("Clear the editor to plan a new auction bid")
        plan.setStyleSheet(
            f"QPushButton#plan{{background:{t.accent};color:#0e0f13;border:none;"
            f"border-radius:9px;padding:7px 14px;font-weight:700;font-size:12.5px;}}"
        )
        plan.clicked.connect(self._new)
        row.addWidget(plan)
        return row

    def _sync_segment(self) -> None:
        archived = self._show_archived.isChecked()
        self._seg_arch.setChecked(archived)
        self._seg_watch.setChecked(not archived)

    # -- watchlist (single row of cards + pager) ---------------------------

    def _build_watchlist(self) -> QWidget:
        t = theme.active()
        self._empty = QLabel(
            "No watched auctions yet — add cards below and Save to track one."
        )
        self._empty.setStyleSheet(f"color:{t.ink3};padding:26px 4px;")
        self._empty.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        # A single non-wrapping row of cards; overflow goes to further pages.
        self._row_host = QWidget()
        self._row = QHBoxLayout(self._row_host)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(14)

        self._pager_host = QWidget()
        self._pager = QHBoxLayout(self._pager_host)
        self._pager.setContentsMargins(0, 4, 0, 2)
        self._pager.setSpacing(6)
        self._pager_host.setVisible(False)

        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(2, 2, 2, 2)
        wl.setSpacing(6)
        wl.addWidget(self._empty)
        wl.addWidget(self._row_host)
        wl.addStretch(1)
        wl.addWidget(self._pager_host)
        wrap.installEventFilter(self)  # leave -> revert gallery
        self._watch_widget = wrap
        return wrap

    def refresh(self) -> None:
        """Reload + re-sort the watchlist (soonest-ending first, ended last)."""
        now = datetime.now()
        archived = self._show_archived.isChecked()
        auctions = self._repo.list_all(archived=archived)
        self._empty.setText(
            "No archived auctions yet."
            if archived
            else "No watched auctions yet — add cards below and Save to track one."
        )
        auctions.sort(key=lambda a: self._sort_key(a, now))
        self._clear_row()
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards = []
        self._hover_card_id = None
        self._page = 0
        for auction in auctions:
            self._add_auction_card(auction)
        self._empty.setVisible(not auctions)
        self._row_host.setVisible(bool(auctions))
        self._ended_ids = {a.id for a in auctions if _is_ended(a.ends_at, now)}
        self._update_tiles(now)
        self._shown_key = None  # force a re-render of the visible page
        self._render_page()
        self._fit_timer.start(0)

    def _add_auction_card(self, auction: Auction) -> None:
        # Built for every auction (so the tick can update all countdowns), but
        # only the current page's cards are placed in the visible row.
        self._cards.append(_AuctionCard(self, auction))

    # -- pagination --------------------------------------------------------

    def _clear_row(self) -> None:
        while self._row.count():
            item = self._row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)  # detached, kept alive by self._cards

    def _cards_per_page(self) -> int:
        width = self._row_host.width()
        if width <= 1:
            width = self._watch_widget.width() - 4
        if width <= 1:
            return max(1, len(self._cards)) or 1
        card_w, gap = 432, 14
        return max(1, (width + gap) // (card_w + gap))

    def _render_page(self) -> None:
        """Show only the current page's cards in the single row + build pager."""
        per = self._cards_per_page()
        n = len(self._cards)
        pages = max(1, (n + per - 1) // per)
        self._page = max(0, min(self._page, pages - 1))
        key = (self._page, per, n)
        if key == self._shown_key:
            return
        self._shown_key = key
        self._clear_row()
        start = self._page * per
        for card in self._cards[start : start + per]:
            self._row.addWidget(card)
        self._row.addStretch(1)  # left-align the row
        self._build_pager(pages)

    def _goto(self, page: int) -> None:
        self._page = page
        self._shown_key = None
        self._render_page()

    def _build_pager(self, pages: int) -> None:
        t = theme.active()
        while self._pager.count():
            item = self._pager.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._pager_host.setVisible(pages > 1)
        if pages <= 1:
            return
        self._pager.addStretch(1)
        prev = self._pager_arrow("‹", self._page > 0)
        prev.clicked.connect(lambda: self._goto(self._page - 1))
        self._pager.addWidget(prev)
        for i in range(pages):
            on = i == self._page
            dot = QPushButton(str(i + 1))
            dot.setCursor(Qt.PointingHandCursor)
            dot.setFixedSize(26, 26)
            dot.setStyleSheet(
                f"QPushButton{{border:1px solid {t.accent if on else t.line2};"
                f"border-radius:7px;background:"
                f"{t.accent_soft if on else 'transparent'};"
                f"color:{t.accent_ink if on else t.ink2};font-size:11px;"
                f"font-weight:700;}}"
                f"QPushButton:hover{{border-color:{t.accent};}}"
            )
            dot.clicked.connect(lambda _=False, p=i: self._goto(p))
            self._pager.addWidget(dot)
        nxt = self._pager_arrow("›", self._page < pages - 1)
        nxt.clicked.connect(lambda: self._goto(self._page + 1))
        self._pager.addWidget(nxt)
        self._pager.addStretch(1)

    def _pager_arrow(self, glyph: str, enabled: bool) -> QPushButton:
        t = theme.active()
        b = QPushButton(glyph)
        b.setFixedSize(28, 26)
        b.setEnabled(enabled)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton{{border:none;background:transparent;color:{t.ink2};"
            f"font-size:17px;font-weight:700;}}"
            f"QPushButton:hover{{color:{t.accent_ink};}}"
            f"QPushButton:disabled{{color:{t.ink3};}}"
        )
        return b

    # -- split sizing (watchlist hugs the single card row) -----------------

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._resize_timer.start(0)

    def _on_view_resized(self) -> None:
        self._render_page()  # width change may alter cards-per-page
        self._fit_split_to_cards()
        self._fit_card_columns()  # Name absorbs the new width

    def _on_splitter_moved(self, *_args) -> None:
        # A user drag (not our own setSizes) pins the split — stop auto-fitting.
        if not self._fitting:
            self._split_user_set = True

    def _fit_split_to_cards(self) -> None:
        """Size the top pane so it ends just below the single card row + pager;
        the composer takes the rest. No-op once the user drags the splitter."""
        if self._split_user_set:
            return
        total = self._body.height()
        if total <= 0:
            return
        card_h = self._cards[0].sizeHint().height() if self._cards else 200
        pager_h = (
            self._pager_host.sizeHint().height() if self._pager_host.isVisible() else 0
        )
        needed = card_h + pager_h + 22
        top = max(200, min(needed, total - 240))  # keep the composer usable
        self._fitting = True
        self._body.setSizes([top, total - top])
        self._fitting = False

    def _update_tiles(self, now: datetime) -> None:
        active = self._repo.list_all(archived=False)
        archived = self._repo.list_all(archived=True)
        total = sum(a.value_cents for a in active)
        soon = 0
        for a in active:
            if not a.ends_at or _is_ended(a.ends_at, now):
                continue
            try:
                if (datetime.fromisoformat(a.ends_at) - now).total_seconds() < 3600:
                    soon += 1
            except ValueError:
                continue
        self._tiles["watching"].setText(str(len(active)))
        self._tiles["value"].setText(money(total))
        self._tiles["soon"].setText(str(soon))
        self._tiles["archived"].setText(str(len(archived)))

    @staticmethod
    def _sort_key(auction: Auction, now: datetime) -> tuple[int, str]:
        """Group active auctions first (soonest end at top), then no-end-time
        ones, then ended auctions at the very bottom."""
        if not auction.ends_at:
            return (1, "")  # no end time → middle
        if _is_ended(auction.ends_at, now):
            return (2, auction.ends_at)  # ended → bottom
        return (0, auction.ends_at)  # active → top, soonest first

    def _on_card_hover(self, card: _AuctionCard) -> None:
        """Hovering a watched auction shows its cards in the gallery display."""
        if card.auction_id == self._hover_card_id:
            return
        self._hover_card_id = card.auction_id
        auction = (
            self._repo.get(card.auction_id) if card.auction_id is not None else None
        )
        if auction is None:
            return
        entries = [
            (c.card_id, c.image_url, _THUMB_BORDER)
            for c in auction.cards
            if c.card_id and c.image_url
        ]
        self._gallery.set_cards(entries)
        self._gallery.show_gallery()

    def eventFilter(self, obj, event) -> bool:
        # Leaving the watchlist reverts the gallery to the editor's own cards.
        if obj is getattr(self, "_watch_widget", None) and event.type() == QEvent.Leave:
            self._hover_card_id = None
            self._refresh_gallery()
        return super().eventFilter(obj, event)

    def _delete(self, auction: Auction) -> None:
        name = auction.title or "(untitled)"
        cards = len(auction.cards)
        detail = f"{cards} card{'' if cards == 1 else 's'}" if cards else "no cards"
        if not _confirm_delete(
            self,
            "Delete auction",
            f"Delete “{name}”?",
            f"This watchlist entry ({detail}) will be permanently removed. "
            "This can't be undone.",
        ):
            return
        self._repo.delete(auction.id)
        if self._edit_id == auction.id:
            self._new()
        self.refresh()

    # -- refresh values -----------------------------------------------------

    def _history_price(self, card_id: str | None) -> int | None:
        if not card_id or self._history is None:
            return None
        try:
            return self._history.current_price(card_id)
        except Exception:
            return None

    def _refresh_values(self, auction: Auction) -> None:
        """Re-pull each card's market value for a saved auction and recompute the
        breakdown. History-first (instant/offline); a live tcgcsv fetch fills any
        card not yet in ``pricehistory.db``. Cards with no catalog match (no
        ``card_id``) can't be repriced and are left unchanged."""
        live: list[AuctionCard] = []
        refreshed = 0
        unmatched: list[str] = []
        for card in auction.cards:
            if not card.card_id:
                unmatched.append(card.name)
                continue
            price = self._history_price(card.card_id)
            if price:
                card.market_value_cents = price
                refreshed += 1
            elif card.set_name and card.number:
                live.append(card)
            else:
                unmatched.append(card.name)

        self._repo.save(auction)
        self._reflect_values_saved(auction, reload_editor=True)

        for card in live:
            category = 85 if card.language == "JP" else 3
            worker = PriceLookupWorker(
                card.set_name,
                card.number,
                card.name,
                category,
                set_code=getattr(card, "set_code", None),
            )
            worker.done.connect(
                lambda variants, a=auction, c=card, w=worker: self._on_refresh_price(
                    a, c, w, variants
                )
            )
            self._price_workers.append(worker)
            worker.start()

        parts = [f"Refreshed {refreshed} card value(s) from current prices."]
        if live:
            parts.append(f"Fetching {len(live)} more live…")
        if unmatched:
            shown = ", ".join(unmatched[:5]) + (
                f" (+{len(unmatched) - 5})" if len(unmatched) > 5 else ""
            )
            parts.append(
                f"{len(unmatched)} card(s) have no catalog match and were left "
                f"unchanged: {shown}."
            )
        QMessageBox.information(self, "Refresh values", "\n".join(parts))

    def _on_refresh_price(self, auction, card, worker, variants) -> None:
        if worker in self._price_workers:
            self._price_workers.remove(worker)
        if not variants:
            return
        base = variants[0]  # base print is authoritative for a stored card
        card.market_value_cents = base.price_cents
        if base.tcgplayer_url:
            card.tcgplayer_url = base.tcgplayer_url
        self._repo.save(auction)
        # Watchlist only (no editor reload) so an async result never clobbers
        # in-progress edits.
        self._reflect_values_saved(auction, reload_editor=False)

    def _reflect_values_saved(self, auction: Auction, *, reload_editor: bool) -> None:
        self.refresh()
        if reload_editor and self._edit_id == auction.id:
            self._load_auction(auction.id)

    # -- editor -------------------------------------------------------------

    def _build_editor(self) -> QWidget:
        t = theme.active()
        box = QFrame()
        box.setObjectName("composer")
        box.setStyleSheet(
            f"QFrame#composer{{background:{t.surface};border:1px solid {t.line};"
            f"border-radius:14px;}}"
        )
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header bar: "Plan a bid" + state badge.
        header = QHBoxLayout()
        header.setContentsMargins(16, 12, 16, 12)
        h2 = QLabel("Plan a bid")
        h2.setStyleSheet(f"font-size:14px;font-weight:700;color:{t.ink};")
        self._banner = QLabel("New auction")
        self._banner.setObjectName("badge")
        self._banner.setStyleSheet(
            f"QLabel#badge{{background:{t.accent_soft};color:{t.accent_ink};"
            f"border-radius:6px;padding:3px 9px;font-size:10px;font-weight:800;}}"
        )
        hint = QLabel("Add the cards, set shipping & tax — the ladder updates live")
        hint.setStyleSheet(f"color:{t.ink3};font-size:11.5px;")
        header.addWidget(h2)
        header.addWidget(self._banner)
        header.addStretch(1)
        header.addWidget(hint)
        outer.addLayout(header)
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background:{t.line};")
        outer.addWidget(divider)

        body = QVBoxLayout()
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(12)

        self._title = QLineEdit()
        self._url = QLineEdit()
        self._url.setPlaceholderText("https://www.ebay.com/itm/…")
        self._shipping = _money_spin()
        self._shipping.valueChanged.connect(self._update_breakdown)
        self._tax = QDoubleSpinBox()
        self._tax.setSuffix(" %")
        self._tax.setDecimals(2)
        self._tax.setMaximum(100.0)
        self._tax.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._tax.setValue(auction_math.DEFAULT_TAX_RATE_BPS / 100)
        self._tax.setToolTip("Sales tax applied to bid + shipping")
        self._tax.valueChanged.connect(self._update_breakdown)

        # End date and time as two separate editable fields, + a live countdown.
        self._ends_date = QDateEdit()
        self._ends_date.setCalendarPopup(True)
        self._ends_date.setDisplayFormat("yyyy-MM-dd")
        self._ends_date.setDate(QDateTime.currentDateTime().addDays(7).date())
        self._ends_date.dateChanged.connect(self._update_countdown)
        self._ends_time = QTimeEdit()
        self._ends_time.setDisplayFormat("HH:mm")
        self._ends_time.setTime(QDateTime.currentDateTime().time())
        self._ends_time.timeChanged.connect(self._update_countdown)
        self._countdown = QLabel("")
        self._countdown.setStyleSheet("font-weight: 600;")
        ends_row = QHBoxLayout()
        ends_row.addWidget(QLabel("Date:"))
        ends_row.addWidget(self._ends_date)
        ends_row.addWidget(QLabel("Time:"))
        ends_row.addWidget(self._ends_time)
        ends_row.addWidget(self._countdown)
        ends_row.addStretch(1)

        form = QFormLayout()
        form.addRow("Title:", self._title)
        form.addRow("eBay URL:", self._url)
        form.addRow("Shipping:", self._shipping)
        form.addRow("Sales tax:", self._tax)
        form.addRow("Ends:", ends_row)

        # Card table + auto-detect. The ＋ lives in a bottom row of the table
        # itself (see _install_card_add_row) so it sits under the last card.
        self._card_table = QTableWidget(0, len(_CARD_HEADERS))
        self._card_table.setHorizontalHeaderLabels(_CARD_HEADERS)
        _tidy(self._card_table, _CARD_WIDTHS)
        # Printing only exists for cards with more than one TCGplayer product, so
        # the column stays hidden until a row actually has a choice to offer.
        self._card_table.setColumnHidden(_PRINTING_COL, True)
        # Room for the header, a few cards and the ＋ row — the button used to be
        # clipped whenever the pane got short.
        self._card_table.setMinimumHeight(172)
        self._install_card_add_row()

        self._auto_detect = QCheckBox("Auto Detect Card")
        self._auto_detect.setChecked(bool(config.get_pref("auction_auto_detect", True)))
        self._auto_detect.toggled.connect(
            lambda on: config.set_pref("auction_auto_detect", on)
        )
        self._catalog_hint = QLabel("")
        self._catalog_hint.setStyleSheet(f"color: {theme.active().warn};")
        self._autofill_btn = QPushButton("Autofill from photo…")
        self._autofill_btn.setIcon(icons.icon("scan-line", theme.active().ink2, 15))
        self._autofill_btn.setToolTip(
            "Detect and identify the cards in one or more scene photos, then add "
            "them as auction rows to confirm."
        )
        self._autofill_btn.clicked.connect(self._autofill_from_photo)
        self._autofill_clip_btn = QPushButton("From clipboard")
        self._autofill_clip_btn.setIcon(
            icons.icon("clipboard-paste", theme.active().ink2, 15)
        )
        self._autofill_clip_btn.setToolTip(
            "Same autofill, from the image on your clipboard — a screenshot or a "
            "copied photo, no file needed."
        )
        self._autofill_clip_btn.clicked.connect(self._autofill_from_clipboard)
        head = QHBoxLayout()
        head.addWidget(self._autofill_btn)
        head.addWidget(self._autofill_clip_btn)
        head.addWidget(self._catalog_hint)
        head.addStretch(1)
        head.addWidget(self._auto_detect)

        cards_col = QVBoxLayout()
        cards_col.addLayout(head)
        cards_col.addWidget(self._card_table, 1)
        cards_widget = QWidget()
        cards_widget.setLayout(cards_col)

        preview_col = QVBoxLayout()
        preview_col.addWidget(QLabel("Cards in this auction"))
        preview_col.addWidget(self._gallery, 1)
        preview_widget = QWidget()
        preview_widget.setLayout(preview_col)

        # 50/50 split: card entry on the left, the card gallery on the right.
        middle = QHBoxLayout()
        middle.addWidget(cards_widget, 1)
        middle.addWidget(preview_widget, 1)

        new_btn = QPushButton("New")
        new_btn.setToolTip("Clear the editor for a fresh auction")
        new_btn.clicked.connect(self._new)
        export_btn = QPushButton("Export to Transaction →")
        export_btn.setToolTip(
            "Send these cards to the Transaction window to add them as a purchase"
        )
        export_btn.clicked.connect(self._export_current)
        self._save_btn = QPushButton("Save auction")
        self._save_btn.setObjectName("primary")  # accent styling from the theme
        self._save_btn.clicked.connect(self._save)
        footer = QHBoxLayout()
        footer.setSpacing(10)
        footer.addWidget(self._build_bid_readout(), 1)
        footer.addWidget(new_btn, 0, Qt.AlignBottom)
        footer.addWidget(export_btn, 0, Qt.AlignBottom)
        footer.addWidget(self._save_btn, 0, Qt.AlignBottom)

        body.addLayout(form)
        body.addLayout(middle, 1)
        body.addLayout(footer)
        outer.addLayout(body)
        self._update_catalog_hint()
        self._update_breakdown()
        return box

    def _build_bid_readout(self) -> QWidget:
        """The composer's live max-bid figure: the recommended bid big, plus a
        compact four-rung reference strip."""
        t = theme.active()
        box = QFrame()
        box.setObjectName("readout")
        box.setStyleSheet(
            f"QFrame#readout{{background:{t.zebra};border:1px solid {t.line2};"
            f"border-radius:12px;}}"
        )
        lay = QHBoxLayout(box)
        lay.setContentsMargins(14, 9, 14, 10)
        lay.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(1)
        cap = QLabel(f"RECOMMENDED MAX BID · {_RECOMMENDED_PCT}%")
        cap.setStyleSheet(
            f"color:{t.ink3};font-size:10px;font-weight:700;letter-spacing:.5px;"
        )
        self._bid_amount = QLabel("$0.00")
        self._bid_amount.setFont(_mono_font(24, bold=True))
        self._bid_amount.setStyleSheet(f"color:{t.accent_ink};")
        self._bid_caption = QLabel("")
        self._bid_caption.setStyleSheet(f"color:{t.ink3};font-size:11px;")
        left.addWidget(cap)
        left.addWidget(self._bid_amount)
        left.addWidget(self._bid_caption)
        lay.addLayout(left)

        rungs = QHBoxLayout()
        rungs.setSpacing(6)
        self._mini: dict[int, QLabel] = {}
        for pct in auction_math.PERCENTAGES:
            cell = QFrame()
            on = pct == _RECOMMENDED_PCT
            cell.setStyleSheet(
                f"background:{'rgba(245,133,46,0.10)' if on else t.surface};"
                f"border:1px solid {t.accent if on else t.line};border-radius:7px;"
            )
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(9, 5, 9, 5)
            cl.setSpacing(1)
            pk = QLabel(f"{pct}%")
            pk.setAlignment(Qt.AlignCenter)
            pk.setStyleSheet(
                f"color:{t.ink3};font-size:9.5px;font-weight:700;border:none;"
                "background:transparent;"
            )
            amt = QLabel("$0")
            amt.setAlignment(Qt.AlignCenter)
            amt.setFont(_mono_font(11, bold=on))
            amt.setStyleSheet(
                f"color:{t.accent_ink if on else t.ink2};border:none;"
                "background:transparent;"
            )
            cl.addWidget(pk)
            cl.addWidget(amt)
            self._mini[pct] = amt
            rungs.addWidget(cell)
        lay.addLayout(rungs)
        return box

    def _update_catalog_hint(self) -> None:
        empty = CardCatalogRepository(self._conn).count() == 0
        self._catalog_hint.setText(
            "Card database is empty — run File ▸ Update card database"
            if (self._auto_detect.isChecked() and empty)
            else ""
        )

    # -- card rows (insert/remove; never a full rebuild) --------------------

    def _add_card_row(self) -> _AuctionCardRow:
        row = _AuctionCardRow(
            self._conn,
            lambda: self._auto_detect.isChecked(),
            self._update_breakdown,
            on_pick=self._on_card_picked,
            sink=self._row_sink,
        )
        index = len(self._rows)  # insert above the trailing ＋ add-row
        self._card_table.insertRow(index)
        for col, widget in enumerate(row.widgets()):
            self._card_table.setCellWidget(index, col, widget)
        self._card_table.setCellWidget(
            index, len(_CARD_HEADERS) - 1, self._delete_button(row)
        )
        self._rows.append(row)
        row.name.setFocus()
        self._fit_card_columns()  # a new row can bring the v-scrollbar in
        self._update_breakdown()
        return row

    def _install_card_add_row(self) -> None:
        """Put the add button in a bottom row of the table so it sits directly
        under the last card (and scrolls with the rows).

        An icon button, not a "＋" label: the app stylesheet gives every
        QPushButton 14px of side padding, which clipped the glyph to a sliver
        inside a narrow cell. It also matches the row's delete button.
        """
        t = theme.active()
        add = _IconButton("plus", t.ink3, t.accent_ink, size=15)
        add.setFixedSize(30, 26)
        add.setToolTip("Add a card in this auction")
        add.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:6px;}"
            f"QPushButton:hover{{background:{t.surface2};}}"
        )
        add.clicked.connect(self._add_card_row)
        r = self._card_table.rowCount()
        self._card_table.insertRow(r)
        self._card_table.setCellWidget(r, 0, add)

    # -- column fitting -----------------------------------------------------

    def _fit_card_columns(self) -> None:
        """Hold the fixed columns at their tuned widths and split the rest
        between **Name**, **Set** and **#** by :data:`_FLEX_COLS`.

        The card table used to carry fixed widths that added up to more than the
        pane: the last columns fell off the right edge, a horizontal scrollbar
        appeared and ate the row holding ＋. Sizing the identity columns from
        what's left means the table fills the pane exactly at any width, and
        sharing that space three ways keeps Name from hoarding it.
        """
        table = getattr(self, "_card_table", None)
        if table is None:
            return
        try:
            available = table.viewport().width()
        except RuntimeError:  # torn down mid-resize
            return
        if available <= 1:
            return
        fixed = 0
        for col, width in enumerate(_CARD_WIDTHS):
            if col in _FLEX_COLS or table.isColumnHidden(col):
                continue
            table.setColumnWidth(col, width)
            fixed += width

        floors = sum(floor for floor, _cap, _share in _FLEX_COLS.values())
        # Below the floors the row simply doesn't fit; the columns stay at their
        # minimums and the table scrolls, exactly as it did before.
        surplus = max(0, available - fixed - floors)
        widths = {}
        for col, (floor, cap, share) in _FLEX_COLS.items():
            width = floor + round(surplus * share)
            widths[col] = width if cap is None else min(cap, width)
        # Rounding and the ceilings can leave a few pixels over; Set takes them
        # so the table still ends flush with the pane.
        widths[_SET_COL] += max(0, available - fixed - sum(widths.values()))
        for col, width in widths.items():
            table.setColumnWidth(col, width)

    def _sync_printing_column(self) -> None:
        """Show the Printing column only while some row has printings to pick.

        Asks each row whether it *has* printings rather than whether its combo is
        visible — a widget in an unshown window reports invisible either way.
        """
        table = self._card_table
        hidden = not any(row.has_printings() for row in self._rows)
        if table.isColumnHidden(_PRINTING_COL) != hidden:
            table.setColumnHidden(_PRINTING_COL, hidden)
            self._fit_card_columns()

    def _delete_button(self, row: _AuctionCardRow) -> QPushButton:
        t = theme.active()
        btn = _IconButton("trash-2", t.ink3, t.neg, size=15)
        btn.setFixedSize(28, 26)
        btn.setToolTip("Remove this card")
        btn.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:6px;}"
            f"QPushButton:hover{{background:{t.surface2};}}"
        )
        btn.clicked.connect(lambda _=False, r=row: self._remove_row(r))
        return btn

    def _remove_row(self, row: _AuctionCardRow) -> None:
        if row in self._rows:
            index = self._rows.index(row)
            self._rows.remove(row)
            self._card_table.removeRow(index)
        self._sync_printing_column()
        self._fit_card_columns()
        self._update_breakdown()
        self._refresh_gallery()

    # -- gallery (multi-card display) ---------------------------------------

    def _refresh_gallery(self) -> None:
        """Rebuild the preview gallery from the cards currently in the editor.

        Only rebuilt when the card set changes (pick / remove / load), so
        thumbnail images load once and persist — the Price Lookup pattern.
        """
        entries = [
            (r.card_id(), r.image_url(), _THUMB_BORDER)
            for r in self._rows
            if r.card_id() and r.image_url()
        ]
        self._gallery.set_cards(entries)
        self._gallery.show_gallery()

    def _on_card_picked(self, row: _AuctionCardRow, card) -> None:
        self._request_price(row, card)
        self._refresh_gallery()

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
        self._autofill_paths(paths)

    def _autofill_from_clipboard(self) -> None:
        """Same autofill, sourced from the clipboard: copied image file(s), or a
        raw bitmap (screenshot / copied image) spooled to a temp PNG."""
        paths = clipboard_image_paths()
        if not paths:
            QMessageBox.information(
                self,
                "Nothing to autofill",
                "The clipboard doesn't contain an image. Copy a card photo (or an "
                "image file) and try again.",
            )
            return
        self._autofill_paths(paths, pasted=True)

    def _autofill_paths(self, paths, *, pasted: bool = False) -> None:
        """Run the detect → review → add-rows flow over each scene image.

        ``pasted`` only affects wording — a spooled temp path means nothing to
        the user, so a miss is reported as "the pasted image"."""
        if not paths:
            return
        try:
            pipeline = self._ensure_pipeline()
        except Exception as exc:  # model/deps unavailable
            QMessageBox.warning(self, "Autofill unavailable", str(exc))
            return
        for path in paths:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                detections = pipeline(path)
            finally:
                QApplication.restoreOverrideCursor()
            if not detections:
                where = "the pasted image" if pasted else str(path)
                QMessageBox.information(
                    self, "No cards found", f"No cards detected in {where}."
                )
                continue
            dialog = AutofillDialog(path, detections, self._conn, self)
            if dialog.exec() != QDialog.Accepted:
                continue
            accepted_cards = dialog.result_cards()
            for accepted in accepted_cards:
                self._apply_accepted_card(accepted)
            inference_write_back(
                self._inference_pipeline, path, "auction", accepted_cards
            )
        self._update_breakdown()

    def _apply_accepted_card(self, accepted) -> None:
        row = self._add_card_row()
        card = (
            CardCatalogRepository(self._conn).get(accepted.card_id)
            if accepted.card_id
            else None
        )
        if card is not None:
            row.apply_catalog_card(card)
            self._request_price(row, card)
        else:
            row.name.setText(accepted.name)
            if accepted.set_name:
                row.set.setText(accepted.set_name)
            if accepted.card_number:
                row.number.setText(accepted.card_number)
        row.qty.setValue(accepted.quantity)

    # -- price autofill -----------------------------------------------------

    def _request_price(self, row: _AuctionCardRow, card) -> None:
        """Fill Market from ``pricehistory.db`` as an **instant provisional**,
        then a live tcgcsv lookup resolves the exact TCGplayer product and
        **corrects the price** to that product's current value (name-checked, so
        it can't land on the wrong card). The live figure wins because the cached
        history can be stale/mis-resolved; history is the offline fallback."""
        if self._history is not None and getattr(card, "card_id", None):
            try:
                hist_price = self._history.current_price(card.card_id)
            except Exception:
                hist_price = None
            if hist_price:
                row.set_market_cents(hist_price)
                self._update_breakdown()
        if not card.set_name or not card.number:
            return
        category = 85 if getattr(card, "language", "EN") == "JP" else 3
        worker = PriceLookupWorker(
            card.set_name,
            card.number,
            card.name,
            category,
            set_code=getattr(card, "set_code", None),
        )
        worker.done.connect(
            lambda variants, r=row, w=worker: self._on_price_ready(r, w, variants)
        )
        self._price_workers.append(worker)
        worker.start()

    def _on_price_ready(self, row, worker, variants) -> None:
        if worker in self._price_workers:
            self._price_workers.remove(worker)
        if not variants or row not in self._rows:
            return
        base = variants[0]
        if base.tcgplayer_url:
            row.set_tcgplayer_url(base.tcgplayer_url)  # exact product link
        # Live base price is authoritative — apply it over the history
        # provisional, but never stomp a value the user hand-edited.
        if not row.market_is_manual:
            row.set_market_cents(base.price_cents)
        row.set_variants(variants)  # offer the printing picker when >1 product
        row.offer_treatments()  # then narrow Foil to what it comes in
        self._sync_printing_column()  # …and reveal its column when it appears
        self._update_breakdown()

    # -- breakdown / save / load --------------------------------------------

    def _current_value_cents(self) -> int:
        return sum(r.value_cents() for r in self._rows)

    def _tax_bps(self) -> int:
        return round(self._tax.value() * 100)

    def _ends_iso(self) -> str:
        """The editor's end date + time (two fields) as one ISO local string."""
        return QDateTime(self._ends_date.date(), self._ends_time.time()).toString(
            Qt.ISODate
        )

    def _update_countdown(self, *_args) -> None:
        self._countdown.setText(_countdown_text(self._ends_iso(), datetime.now()))

    def _update_breakdown(self, *_args) -> None:
        value = self._current_value_cents()
        shipping = round(self._shipping.value() * 100)
        bd = auction_math.breakdown(value, shipping, self._tax_bps())
        self._bid_amount.setText(money(bd.bids[_RECOMMENDED_PCT]))
        self._bid_caption.setText(
            f"All-in ≈ {_RECOMMENDED_PCT}% of {money(value)} card value · "
            f"keeps ~{100 - _RECOMMENDED_PCT}% margin"
        )
        for pct, label in self._mini.items():
            label.setText(money(bd.bids[pct]))

    def _save(self) -> None:
        cards = [c for c in (r.to_card() for r in self._rows) if c]
        if not cards:
            QMessageBox.information(
                self, "Nothing to save", "Add at least one card to the auction."
            )
            return
        auction = Auction(
            id=self._edit_id,
            title=self._title.text().strip(),
            url=self._url.text().strip() or None,
            shipping_cents=round(self._shipping.value() * 100),
            tax_rate_bps=self._tax_bps(),
            ends_at=self._ends_iso(),
            cards=cards,
        )
        saved = self._repo.save(auction)
        self._edit_id = saved.id
        self._banner.setText(f"Editing saved auction #{saved.id}")
        self.refresh()

    def _export_current(self) -> None:
        """Send the editor's cards to the Transaction window as a purchase."""
        cards = [c for c in (r.to_card() for r in self._rows) if c]
        if not cards:
            QMessageBox.information(
                self, "Nothing to export", "Add at least one card first."
            )
            return
        title = self._title.text().strip() or "eBay auction"
        self.export_to_transaction.emit(
            (title, [_auction_card_to_incoming(c) for c in cards])
        )

    def _export_auction(self, auction: Auction) -> None:
        """Send a saved/archived auction's cards to the Transaction window."""
        if not auction.cards:
            QMessageBox.information(
                self, "Nothing to export", "This auction has no cards."
            )
            return
        title = auction.title or "eBay auction"
        self.export_to_transaction.emit(
            (title, [_auction_card_to_incoming(c) for c in auction.cards])
        )

    def _toggle_archived(self, auction: Auction) -> None:
        self._repo.set_archived(auction.id, not auction.archived)
        self.refresh()

    def _load_auction(self, auction_id: int) -> None:
        auction = self._repo.get(auction_id)
        if auction is None:
            return
        self._clear_editor()
        self._edit_id = auction.id
        self._banner.setText(f"Editing saved auction #{auction.id}")
        self._title.setText(auction.title)
        self._url.setText(auction.url or "")
        self._shipping.setValue(auction.shipping_cents / 100)
        self._tax.setValue(auction.tax_rate_bps / 100)
        if auction.ends_at:
            dt = QDateTime.fromString(auction.ends_at, Qt.ISODate)
            if dt.isValid():
                self._ends_date.setDate(dt.date())
                self._ends_time.setTime(dt.time())
        for card in auction.cards:
            row = self._add_card_row()
            row.set_card(card)
        self._update_breakdown()
        self._update_countdown()
        self._refresh_gallery()

    def _new(self) -> None:
        self._clear_editor()
        self._banner.setText("New auction")
        self._update_breakdown()
        self._refresh_gallery()

    # -- Recognition export -------------------------------------------------

    def editor_has_content(self) -> bool:
        """True if the bid editor holds anything worth confirming before a clear."""
        return bool(self._rows) or bool(self._title.text().strip())

    def import_recognized(self, cards) -> None:
        """Start a fresh lot and fill its rows from Recognition's labeled cards,
        inheriting the same catalog + price autofill the ＋ add-card path uses."""
        self._new()
        for card in cards:
            row = self._add_card_row()
            catalog = (
                CardCatalogRepository(self._conn).get(card.card_id)
                if getattr(card, "card_id", None)
                else None
            )
            if catalog is not None:
                row.apply_catalog_card(catalog)
                self._request_price(row, catalog)
            else:
                row.name.setText(card.name)
                if card.set_name:
                    row.set.setText(card.set_name)
                if card.card_number:
                    row.number.setText(card.card_number)
            if card.condition:
                _select(row.condition, card.condition)
            if getattr(card, "holo_pattern", None):
                _select(row.holo, _value_of(card.holo_pattern))
            row.qty.setValue(max(1, card.quantity))
        self._update_breakdown()
        self._refresh_gallery()

    def _clear_editor(self) -> None:
        self._rows.clear()
        self._card_table.setRowCount(0)
        self._install_card_add_row()  # setRowCount(0) removed the ＋ add-row
        self._title.clear()
        self._url.clear()
        self._shipping.setValue(0.0)
        self._tax.setValue(auction_math.DEFAULT_TAX_RATE_BPS / 100)
        self._ends_date.setDate(QDateTime.currentDateTime().addDays(7).date())
        self._ends_time.setTime(QDateTime.currentDateTime().time())
        self._edit_id = None
        self._update_countdown()


def _safe_open_history():
    """Open ``pricehistory.db`` for price autofill, or ``None`` if unavailable."""
    try:
        from tcginventory.data.pricehistory import PriceHistoryRepository, open_history

        return PriceHistoryRepository(open_history())
    except Exception:
        return None


def _tidy(table: QTableWidget, widths: list[int]) -> None:
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setSectionResizeMode(QHeaderView.Interactive)
    header.setMinimumSectionSize(22)
    for col, width in enumerate(widths):
        if width:
            table.setColumnWidth(col, width)
