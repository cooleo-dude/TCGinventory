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

from PySide6.QtCore import QModelIndex, Qt, QThread, QUrl, Signal
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
from tcginventory.services import auctions as auction_math
from tcginventory.ui.card_preview import CardImagePane, install_hover_preview
from tcginventory.ui.formatting import money, percent

_CARD_HEADERS = ["Name", "Set", "#", "Market", "Qty", ""]
_CARD_WIDTHS = [200, 150, 70, 90, 50, 30]

_WATCH_HEADERS = [
    "Title", "Cards", "Value", "70%", "80%", "90%", "100%", "Ship", "Tax",
    "Link", "",
]
_WATCH_WIDTHS = [220, 50, 90, 90, 90, 90, 90, 80, 60, 60, 30]
_ID_ROLE = Qt.UserRole + 1
_UNSET = object()  # sentinel: history not supplied -> open the default DB


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

    def widgets(self) -> list[QWidget]:
        return [self.name, self.set, self.number, self.market, self.qty]

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
        self.name.setText(card.name)
        self.set.setText(card.set_name or "")
        self.number.setText(card.number or "")
        self.market.setValue(card.market_value_cents / 100)
        self.qty.setValue(card.quantity)

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
        )


class AuctionsView(QWidget):
    """The eBay Auctions screen (watchlist + max-bid editor)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        auctions_conn: sqlite3.Connection | None = None,
        history=_UNSET,
    ) -> None:
        super().__init__()
        self._conn = conn  # main DB (catalog search)
        self._auctions = (
            auctions_conn if auctions_conn is not None else open_auctions()
        )
        self._repo = AuctionRepository(self._auctions)
        # ``history`` supplied (incl. None) is honoured; omitted -> open default.
        self._history = _safe_open_history() if history is _UNSET else history

        self._rows: list[_AuctionCardRow] = []
        self._price_workers: list[_LivePriceWorker] = []
        self._edit_id: int | None = None
        self._preview_pane = CardImagePane()

        body = QSplitter(Qt.Vertical)
        body.addWidget(self._build_watchlist())
        body.addWidget(self._build_editor())
        body.setStretchFactor(0, 1)
        body.setStretchFactor(1, 2)
        body.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.addWidget(body)

        self.refresh()
        self._update_breakdown()

    # -- watchlist ----------------------------------------------------------

    def _build_watchlist(self) -> QGroupBox:
        box = QGroupBox("Watched auctions")
        self._watch = QTableWidget(0, len(_WATCH_HEADERS))
        self._watch.setHorizontalHeaderLabels(_WATCH_HEADERS)
        _tidy(self._watch, _WATCH_WIDTHS)
        self._watch.cellClicked.connect(self._on_watch_cell_clicked)
        self._empty = QLabel(
            "No watched auctions yet — add cards below and Save to track one."
        )
        self._empty.setStyleSheet("color: #928374;")
        layout = QVBoxLayout(box)
        layout.addWidget(self._empty)
        layout.addWidget(self._watch, 1)
        return box

    def refresh(self) -> None:
        """Reload the watchlist from ``auctions.db``."""
        auctions = self._repo.list_all()
        self._empty.setVisible(not auctions)
        self._watch.setRowCount(0)
        for auction in auctions:
            self._append_watch_row(auction)

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

        link = QPushButton("Open ↗")
        link.setEnabled(bool(auction.url))
        link.setToolTip(auction.url or "No link")
        if auction.url:
            link.clicked.connect(
                lambda _=False, u=auction.url: QDesktopServices.openUrl(QUrl(u))
            )
        self._watch.setCellWidget(r, 9, link)

        remove = QPushButton("✕")
        remove.setFixedWidth(28)
        remove.setToolTip("Delete this auction")
        remove.clicked.connect(lambda _=False, a=auction: self._delete(a))
        self._watch.setCellWidget(r, 10, remove)

    def _on_watch_cell_clicked(self, row: int, col: int) -> None:
        # The Link (9) and Delete (10) columns are buttons; other cells load.
        if col >= 9:
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

        form = QFormLayout()
        form.addRow("Title:", self._title)
        form.addRow("eBay URL:", self._url)
        form.addRow("Shipping:", self._shipping)
        form.addRow("Sales tax:", self._tax)

        # Card table + add / auto-detect.
        self._card_table = QTableWidget(0, len(_CARD_HEADERS))
        self._card_table.setHorizontalHeaderLabels(_CARD_HEADERS)
        _tidy(self._card_table, _CARD_WIDTHS)

        add = QPushButton("＋")
        add.setFixedWidth(32)
        add.setToolTip("Add a card in this auction")
        add.clicked.connect(self._add_card_row)
        self._auto_detect = QCheckBox("Auto Detect Card")
        self._auto_detect.setChecked(
            bool(config.get_pref("auction_auto_detect", True))
        )
        self._auto_detect.toggled.connect(
            lambda on: config.set_pref("auction_auto_detect", on)
        )
        self._catalog_hint = QLabel("")
        self._catalog_hint.setStyleSheet("color: #b26a00;")
        head = QHBoxLayout()
        head.addWidget(add)
        head.addWidget(self._catalog_hint)
        head.addStretch(1)
        head.addWidget(self._auto_detect)

        cards_col = QVBoxLayout()
        cards_col.addLayout(head)
        cards_col.addWidget(self._card_table, 1)
        cards_widget = QWidget()
        cards_widget.setLayout(cards_col)

        preview_col = QVBoxLayout()
        preview_col.addWidget(QLabel("Card preview"))
        preview_col.addWidget(self._preview_pane, 1)
        preview_widget = QWidget()
        preview_widget.setLayout(preview_col)

        middle = QHBoxLayout()
        middle.addWidget(cards_widget, 3)
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
            on_pick=self._request_price,
            sink=self._preview_pane,
        )
        self._rows.append(row)
        index = self._card_table.rowCount()
        self._card_table.insertRow(index)
        for col, widget in enumerate(row.widgets()):
            self._card_table.setCellWidget(index, col, widget)
        self._card_table.setCellWidget(
            index, len(_CARD_HEADERS) - 1, self._delete_button(row)
        )
        row.name.setFocus()
        self._update_breakdown()
        return row

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

    # -- price autofill -----------------------------------------------------

    def _request_price(self, row: _AuctionCardRow, card) -> None:
        """Fill Market from ``pricehistory.db`` (instant); fall back to a live
        tcgcsv fetch for cards not yet in the local history."""
        price = None
        if self._history is not None and getattr(card, "card_id", None):
            try:
                price = self._history.current_price(card.card_id)
            except Exception:
                price = None
        if price:
            row.set_market_cents(price)
            self._update_breakdown()
            return
        if not card.set_name or not card.number:
            return
        category = 85 if getattr(card, "language", "EN") == "JP" else 3
        worker = _LivePriceWorker(card.set_name, card.number, card.name, category)
        worker.done.connect(
            lambda result, r=row, w=worker: self._on_price_ready(r, w, result)
        )
        self._price_workers.append(worker)
        worker.start()

    def _on_price_ready(self, row, worker, result) -> None:
        if worker in self._price_workers:
            self._price_workers.remove(worker)
        if result is None or row not in self._rows:
            return
        cents, _url = result
        row.set_market_cents(cents)
        self._update_breakdown()

    # -- breakdown / save / load --------------------------------------------

    def _current_value_cents(self) -> int:
        return sum(r.value_cents() for r in self._rows)

    def _tax_bps(self) -> int:
        return round(self._tax.value() * 100)

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
            cards=cards,
        )
        saved = self._repo.save(auction)
        self._edit_id = saved.id
        self._banner.setText(f"Editing saved auction #{saved.id}")
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
        for card in auction.cards:
            row = self._add_card_row()
            row.set_card(card)
        self._update_breakdown()

    def _new(self) -> None:
        self._clear_editor()
        self._banner.setText("New auction")
        self._update_breakdown()

    def _clear_editor(self) -> None:
        self._rows.clear()
        self._card_table.setRowCount(0)
        self._title.clear()
        self._url.clear()
        self._shipping.setValue(0.0)
        self._tax.setValue(auction_math.DEFAULT_TAX_RATE_BPS / 100)
        self._edit_id = None


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
