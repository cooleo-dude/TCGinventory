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
    QModelIndex,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QDesktopServices,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QCompleter,
    QDateEdit,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
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
from tcginventory.data.vision import ImageLabel, VisionRepository, open_vision
from tcginventory.services import auctions as auction_math
from tcginventory.services import vision as vision_svc
from tcginventory.ui.card_gallery import CardGallery
from tcginventory.ui.card_preview import install_hover_preview
from tcginventory.ui.formatting import money, percent
from tcginventory.ui.image_capture import ImageCaptureButton

_CARD_HEADERS = ["Name", "Set", "#", "Market", "Qty", "TCGplayer", "Photos", ""]
_CARD_WIDTHS = [200, 150, 70, 90, 50, 96, 56, 30]

_WATCH_HEADERS = [
    "Title",
    "Cards",
    "Value",
    "70%",
    "80%",
    "90%",
    "100%",
    "Ship",
    "Tax",
    "Time left",
    "",
    "Link",
    "",
]
_WATCH_WIDTHS = [220, 50, 90, 90, 90, 90, 90, 80, 60, 110, 34, 60, 30]
_TIME_LEFT_COL = _WATCH_HEADERS.index("Time left")
_FIRST_BUTTON_COL = _TIME_LEFT_COL + 1  # refresh / link / delete follow
_ID_ROLE = Qt.UserRole + 1
_ENDS_ROLE = Qt.UserRole + 2  # ISO end datetime stored on the "Time left" cell
_UNSET = object()  # sentinel: history not supplied -> open the default DB
_THUMB_BORDER = "#504945"  # neutral thumbnail frame (no chart-line colours here)


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


class _LivePriceWorker(QThread):
    """Fetch a picked card's current tcgcsv market price off the UI thread.

    A fallback for cards that aren't in ``pricehistory.db`` yet (mirrors the
    Transaction window's price-on-pick).
    """

    done = Signal(object)  # (price_cents, tcgplayer_url) or None

    def __init__(self, set_name, number, name, category=3) -> None:
        super().__init__()
        self._set = set_name
        self._number = number
        self._name = name
        self._category = category

    def run(self) -> None:
        from tcginventory.services.pricing import TcgcsvProvider

        try:
            result = TcgcsvProvider().price_for_card(
                self._set, self._number, self._name, category=self._category
            )
        except Exception:  # network/parse failure -> skip the auto-price
            result = None
        self.done.emit(result)


class _MatchDelegate(QStyledItemDelegate):
    """Paint autofill popup rows with the fuller "name · set #number" label,
    while QCompleter still inserts just the clean card name."""

    def __init__(self, label_role: int, parent=None) -> None:
        super().__init__(parent)
        self._label_role = label_role

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        label = index.data(self._label_role)
        if label:
            option.text = label


class _AuctionCardRow:
    """Inline editor row for one card bundled in an auction.

    The Name field autocompletes from the local catalog; picking a match fills
    set/number/language and triggers a price lookup that fills Market.
    """

    _CARD_ID_ROLE = Qt.UserRole + 1
    _LABEL_ROLE = Qt.UserRole + 2

    def __init__(self, conn, is_auto, on_change, on_pick=None, sink=None) -> None:
        self._conn = conn
        self._is_auto = is_auto  # () -> bool : is "Auto Detect Card" on?
        self._on_change = on_change
        self._on_pick = on_pick  # (row, CatalogCard) -> None : after a pick
        self._sink = sink  # card-image preview target
        self._matches: dict[str, object] = {}
        self._card_id: str | None = None
        self._image_url: str | None = None
        self._tcgplayer_url: str | None = None
        self._language: str = "EN"

        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Charizard Obsidian Flames 125")
        self._search_model = QStandardItemModel(self.name)
        self._completer = QCompleter(self._search_model, self.name)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.name.setCompleter(self._completer)
        self.name.textEdited.connect(self._on_name_edited)
        self._completer.activated[QModelIndex].connect(self._pick_match)
        self.name.textChanged.connect(lambda *_: on_change())
        install_hover_preview(self.name, self._hover_card, self._sink)
        popup = self._completer.popup()
        popup.setMouseTracking(True)
        popup.entered.connect(self._on_match_hover)
        self._match_delegate = _MatchDelegate(self._LABEL_ROLE, popup)
        popup.setItemDelegate(self._match_delegate)
        install_hover_preview(popup, lambda: None, self._sink)

        self.set = QLineEdit()
        self.number = QLineEdit()
        self.market = _money_spin()
        self.market.valueChanged.connect(lambda *_: on_change())
        self.qty = QSpinBox()
        self.qty.setMinimum(1)
        self.qty.setMaximum(9999)
        self.qty.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.qty.valueChanged.connect(lambda *_: on_change())
        # TCGplayer link button — enabled once a product URL is resolved (from
        # the price-on-pick lookup). Clicking opens the listing.
        self.tcg_btn = QPushButton("TCGplayer")
        self.tcg_btn.setEnabled(False)
        self.tcg_btn.setToolTip("Open this card on TCGplayer")
        self.tcg_btn.clicked.connect(self._open_tcgplayer)
        self.images = ImageCaptureButton()  # photos → vision training corpus

    def widgets(self) -> list[QWidget]:
        return [
            self.name, self.set, self.number, self.market, self.qty,
            self.tcg_btn, self.images,
        ]

    def _open_tcgplayer(self) -> None:
        if self._tcgplayer_url:
            QDesktopServices.openUrl(QUrl(self._tcgplayer_url))

    def set_tcgplayer_url(self, url: str | None) -> None:
        self._tcgplayer_url = url or None
        self.tcg_btn.setEnabled(bool(self._tcgplayer_url))
        self.tcg_btn.setToolTip(
            self._tcgplayer_url or "No TCGplayer link for this card"
        )

    # -- catalog autofill ---------------------------------------------------

    def _on_name_edited(self, text: str) -> None:
        if not self._is_auto() or len(text.strip()) < 2:
            return
        matches = CardCatalogRepository(self._conn).search(text, limit=25)
        self._matches = {c.card_id: c for c in matches}
        self._search_model.clear()
        for card in matches:
            item = QStandardItem(card.name)
            label = (
                f"{card.name}  ·  {card.set_name or '?'}  #{card.full_number or '?'}"
            )
            item.setData(card.card_id, self._CARD_ID_ROLE)
            item.setData(label, self._LABEL_ROLE)
            self._search_model.appendRow(item)
        if matches:
            self._completer.complete()

    def _on_match_hover(self, index: QModelIndex) -> None:
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
        self._card_id = card.card_id
        self._image_url = getattr(card, "image_small", None)
        self._language = getattr(card, "language", None) or "EN"
        self.set_tcgplayer_url(None)  # cleared until the price lookup resolves it
        self.name.setText(card.name)
        self.set.setText(card.set_name or "")
        self.number.setText(card.full_number or card.number or "")
        if self._sink is not None and self._image_url:
            self._sink.show_card(self._card_id, self._image_url)
        self._on_change()

    # -- (de)serialise ------------------------------------------------------

    def set_market_cents(self, cents: int) -> None:
        self.market.setValue(cents / 100)

    def set_card(self, card: AuctionCard) -> None:
        self._card_id = card.card_id
        self._image_url = card.image_url
        self._language = card.language or "EN"
        self.set_tcgplayer_url(card.tcgplayer_url)
        self.name.setText(card.name)
        self.set.setText(card.set_name or "")
        self.number.setText(card.number or "")
        self.market.setValue(card.market_value_cents / 100)
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
            quantity=self.qty.value(),
            market_value_cents=round(self.market.value() * 100),
            image_url=self._image_url,
            tcgplayer_url=self._tcgplayer_url,
        )


class AuctionsView(QWidget):
    """The eBay Auctions screen (watchlist + max-bid editor)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        auctions_conn: sqlite3.Connection | None = None,
        history=_UNSET,
        vision_repo=None,
        vision_dir=None,
    ) -> None:
        super().__init__()
        self._conn = conn  # main DB (catalog search)
        # Vision training store (opened lazily on first image ingest; injectable).
        self._vision_repo = vision_repo
        self._vision_dir = vision_dir
        self._auctions = auctions_conn if auctions_conn is not None else open_auctions()
        self._repo = AuctionRepository(self._auctions)
        # ``history`` supplied (incl. None) is honoured; omitted -> open default.
        self._history = _safe_open_history() if history is _UNSET else history

        self._rows: list[_AuctionCardRow] = []
        self._price_workers: list[_LivePriceWorker] = []
        self._edit_id: int | None = None
        self._ended_ids: set[int] = set()  # which auctions were ended last refresh
        self._hover_watch_row: int | None = None
        # Multi-card display (same widget/logic as the Price Lookup window):
        # shows every card in the auction, or a single focused card on hover.
        self._gallery = CardGallery(
            empty_hint="Pick cards from the catalog to see them here"
        )
        self._row_sink = _GallerySink(self._gallery)

        body = QSplitter(Qt.Vertical)
        body.addWidget(self._build_watchlist())
        body.addWidget(self._build_editor())
        body.setStretchFactor(0, 1)
        body.setStretchFactor(1, 2)
        body.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.addWidget(body)

        # Once-a-second timer drives the live countdowns (editor label + the
        # watchlist "Time left" column). Always running so a row flips to
        # "ENDED" on its own, with no refresh/navigation needed.
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
        for r in range(self._watch.rowCount()):
            item = self._watch.item(r, _TIME_LEFT_COL)
            if item is None:
                continue
            ends_at = item.data(_ENDS_ROLE)
            item.setText(_countdown_text(ends_at, now))
            if _is_ended(ends_at, now):
                title = self._watch.item(r, 0)
                if title is not None:
                    ended.add(title.data(_ID_ROLE))
        # An auction just crossed into "ENDED" → re-sort so it sinks to the
        # bottom (only rebuilds when the ended set actually changes).
        if ended != self._ended_ids:
            self.refresh()

    # -- watchlist ----------------------------------------------------------

    def _build_watchlist(self) -> QGroupBox:
        box = QGroupBox("Watched auctions")
        self._watch = QTableWidget(0, len(_WATCH_HEADERS))
        self._watch.setHorizontalHeaderLabels(_WATCH_HEADERS)
        _tidy(self._watch, _WATCH_WIDTHS)
        self._watch.cellClicked.connect(self._on_watch_cell_clicked)
        self._watch.setMouseTracking(True)  # hover a row -> preview its cards
        self._watch.cellEntered.connect(self._on_watch_hover)
        self._watch.viewport().installEventFilter(self)  # leave -> revert gallery
        self._empty = QLabel(
            "No watched auctions yet — add cards below and Save to track one."
        )
        self._empty.setStyleSheet("color: #928374;")
        layout = QVBoxLayout(box)
        layout.addWidget(self._empty)
        layout.addWidget(self._watch, 1)
        return box

    def refresh(self) -> None:
        """Reload + re-sort the watchlist (soonest-ending first, ended last)."""
        now = datetime.now()
        auctions = self._repo.list_all()
        auctions.sort(key=lambda a: self._sort_key(a, now))
        self._empty.setVisible(not auctions)
        self._watch.setRowCount(0)
        self._hover_watch_row = None
        for auction in auctions:
            self._append_watch_row(auction)
        self._ended_ids = {a.id for a in auctions if _is_ended(a.ends_at, now)}

    @staticmethod
    def _sort_key(auction: Auction, now: datetime) -> tuple[int, str]:
        """Group active auctions first (soonest end at top), then no-end-time
        ones, then ended auctions at the very bottom."""
        if not auction.ends_at:
            return (1, "")  # no end time → middle
        if _is_ended(auction.ends_at, now):
            return (2, auction.ends_at)  # ended → bottom
        return (0, auction.ends_at)  # active → top, soonest first

    def _on_watch_hover(self, row: int, _col: int) -> None:
        """Hovering a watched auction shows its cards in the gallery display."""
        if row == self._hover_watch_row:
            return
        self._hover_watch_row = row
        title = self._watch.item(row, 0)
        if title is None:
            return
        auction_id = title.data(_ID_ROLE)
        auction = self._repo.get(int(auction_id)) if auction_id is not None else None
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
        if obj is self._watch.viewport() and event.type() == QEvent.Leave:
            self._hover_watch_row = None
            self._refresh_gallery()
        return super().eventFilter(obj, event)

    def _append_watch_row(self, auction: Auction) -> None:
        r = self._watch.rowCount()
        self._watch.insertRow(r)
        bd = auction_math.breakdown(
            auction.value_cents, auction.shipping_cents, auction.tax_rate_bps
        )

        title = QTableWidgetItem(auction.title or "(untitled)")
        title.setData(_ID_ROLE, auction.id)
        cells = [
            title,
            QTableWidgetItem(str(sum(c.quantity for c in auction.cards))),
            QTableWidgetItem(money(auction.value_cents)),
            QTableWidgetItem(money(bd.bids[70])),
            QTableWidgetItem(money(bd.bids[80])),
            QTableWidgetItem(money(bd.bids[90])),
            QTableWidgetItem(money(bd.bids[100])),
            QTableWidgetItem(money(auction.shipping_cents)),
            QTableWidgetItem(percent(auction.tax_rate_bps / 100)),
        ]
        for col, cell in enumerate(cells):
            self._watch.setItem(r, col, cell)

        # Live countdown cell — the end time rides on the item so the timer can
        # recompute it in place each second (see _on_tick).
        time_left = QTableWidgetItem(_countdown_text(auction.ends_at, datetime.now()))
        time_left.setData(_ENDS_ROLE, auction.ends_at)
        self._watch.setItem(r, _TIME_LEFT_COL, time_left)

        refresh = QPushButton("↻")
        refresh.setFixedWidth(30)
        refresh.setToolTip("Refresh card values from current market prices")
        refresh.clicked.connect(lambda _=False, a=auction: self._refresh_values(a))
        self._watch.setCellWidget(r, _FIRST_BUTTON_COL, refresh)

        link = QPushButton("Open ↗")
        link.setEnabled(bool(auction.url))
        link.setToolTip(auction.url or "No link")
        if auction.url:
            link.clicked.connect(
                lambda _=False, u=auction.url: QDesktopServices.openUrl(QUrl(u))
            )
        self._watch.setCellWidget(r, _FIRST_BUTTON_COL + 1, link)

        remove = QPushButton("✕")
        remove.setFixedWidth(28)
        remove.setToolTip("Delete this auction")
        remove.clicked.connect(lambda _=False, a=auction: self._delete(a))
        self._watch.setCellWidget(r, _FIRST_BUTTON_COL + 2, remove)

    def _on_watch_cell_clicked(self, row: int, col: int) -> None:
        # The trailing refresh/link/delete columns are buttons; data cells load.
        if col >= _FIRST_BUTTON_COL:
            return
        item = self._watch.item(row, 0)
        if item is None:
            return
        auction_id = item.data(_ID_ROLE)
        if auction_id is not None:
            self._load_auction(int(auction_id))

    def _delete(self, auction: Auction) -> None:
        name = auction.title or "(untitled)"
        if (
            QMessageBox.question(self, "Delete auction", f"Delete '{name}'?")
            != QMessageBox.Yes
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
            worker = _LivePriceWorker(card.set_name, card.number, card.name, category)
            worker.done.connect(
                lambda result, a=auction, c=card, w=worker: self._on_refresh_price(
                    a, c, w, result
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

    def _on_refresh_price(self, auction, card, worker, result) -> None:
        if worker in self._price_workers:
            self._price_workers.remove(worker)
        if result is None:
            return
        cents, url = result
        card.market_value_cents = cents
        if url:
            card.tcgplayer_url = url
        self._repo.save(auction)
        # Watchlist only (no editor reload) so an async result never clobbers
        # in-progress edits.
        self._reflect_values_saved(auction, reload_editor=False)

    def _reflect_values_saved(self, auction: Auction, *, reload_editor: bool) -> None:
        self.refresh()
        if reload_editor and self._edit_id == auction.id:
            self._load_auction(auction.id)

    # -- editor -------------------------------------------------------------

    def _build_editor(self) -> QGroupBox:
        box = QGroupBox("Auction details")
        self._banner = QLabel("New auction")
        self._banner.setStyleSheet("color: #b26a00; font-weight: 600;")

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
        self._install_card_add_row()

        self._auto_detect = QCheckBox("Auto Detect Card")
        self._auto_detect.setChecked(bool(config.get_pref("auction_auto_detect", True)))
        self._auto_detect.toggled.connect(
            lambda on: config.set_pref("auction_auto_detect", on)
        )
        self._catalog_hint = QLabel("")
        self._catalog_hint.setStyleSheet("color: #b26a00;")
        head = QHBoxLayout()
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

        self._breakdown = QLabel("")
        self._breakdown.setTextFormat(Qt.RichText)
        new_btn = QPushButton("New")
        new_btn.setToolTip("Clear the editor for a fresh auction")
        new_btn.clicked.connect(self._new)
        self._save_btn = QPushButton("Save auction")
        self._save_btn.clicked.connect(self._save)
        footer = QHBoxLayout()
        footer.addWidget(self._breakdown, 1)
        footer.addWidget(new_btn)
        footer.addWidget(self._save_btn)

        layout = QVBoxLayout(box)
        layout.addWidget(self._banner)
        layout.addLayout(form)
        layout.addLayout(middle, 1)
        layout.addLayout(footer)
        self._update_catalog_hint()
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
        self._update_breakdown()
        return row

    def _install_card_add_row(self) -> None:
        """Put the ＋ button in a bottom row of the table so it sits directly
        under the last card (and scrolls with the rows)."""
        add = QPushButton("＋")
        add.setFixedWidth(32)
        add.setToolTip("Add a card in this auction")
        add.clicked.connect(self._add_card_row)
        r = self._card_table.rowCount()
        self._card_table.insertRow(r)
        self._card_table.setCellWidget(r, 0, add)

    def _delete_button(self, row: _AuctionCardRow) -> QPushButton:
        btn = QPushButton("✕")
        btn.setFixedWidth(28)
        btn.setToolTip("Remove this card")
        btn.clicked.connect(lambda _=False, r=row: self._remove_row(r))
        return btn

    def _remove_row(self, row: _AuctionCardRow) -> None:
        if row in self._rows:
            index = self._rows.index(row)
            self._rows.remove(row)
            self._card_table.removeRow(index)
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

    # -- price autofill -----------------------------------------------------

    def _request_price(self, row: _AuctionCardRow, card) -> None:
        """Fill Market from ``pricehistory.db`` (instant, offline-friendly), then
        run a live tcgcsv lookup to resolve the exact TCGplayer product URL (and
        the price, if history had none)."""
        hist_price = None
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
        worker = _LivePriceWorker(card.set_name, card.number, card.name, category)
        worker.done.connect(
            lambda result, r=row, w=worker, had=bool(hist_price): self._on_price_ready(
                r, w, result, had
            )
        )
        self._price_workers.append(worker)
        worker.start()

    def _on_price_ready(self, row, worker, result, had_hist_price=False) -> None:
        if worker in self._price_workers:
            self._price_workers.remove(worker)
        if result is None or row not in self._rows:
            return
        cents, url = result
        if url:
            row.set_tcgplayer_url(url)  # exact product link for the button
        if not had_hist_price:  # history is preferred; live only backfills price
            row.set_market_cents(cents)
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
        parts = [f"Card value: <b>{money(value)}</b>", "&nbsp; Max bid →"]
        for pct in auction_math.PERCENTAGES:
            parts.append(f"{pct}%: <b>{money(bd.bids[pct])}</b>")
        self._breakdown.setText(" &nbsp;·&nbsp; ".join(parts))

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
        self._ingest_images()
        self.refresh()

    def _ingest_images(self) -> None:
        """Store photos attached to auction cards into the vision training store,
        labelled with each card's identity. Cleared after ingest so repeated
        saves don't duplicate. Never fails a save."""
        jobs = []
        for row in self._rows:
            paths = row.images.paths()
            card = row.to_card()
            if paths and card is not None:
                jobs.append(
                    (
                        row,
                        paths,
                        ImageLabel(
                            card_id=card.card_id,
                            name=card.name,
                            set_name=card.set_name,
                            card_number=card.number,
                            language=card.language,
                        ),
                    )
                )
        if not jobs:
            return
        repo = self._vision_repo or VisionRepository(open_vision())
        for row, paths, label in jobs:
            for path in paths:
                try:
                    vision_svc.ingest(
                        repo, path, "auction", label, base_dir=self._vision_dir
                    )
                except Exception:  # a bad photo must never block the save
                    pass
            row.images.set_paths([])  # don't re-ingest on the next save

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
        table.setColumnWidth(col, width)
