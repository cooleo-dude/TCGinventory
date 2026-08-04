"""Shared building blocks for inline card-entry rows.

Both the Transaction "receive" row and the Auction card row let the user type a
card name, autocomplete it from the local catalog, preview art on hover, and get
a price (plus a printing picker when a card has more than one TCGplayer product).
That behaviour used to be copy-pasted in both screens and had already drifted
apart. It now lives here as the single standard; each screen supplies only its
own widget *layout* and the domain object it emits.

A host row must:
  * create ``self.name`` (a QLineEdit), ``self.set`` and ``self.number`` line
    edits, and set ``self._conn``, ``self._is_auto``, ``self._on_change``,
    ``self._on_pick``, ``self._sink`` before calling ``_init_catalog_search()``;
  * for the printing picker, call ``_init_printing_picker()`` (creates
    ``self.printing``) and implement ``_apply_variant(variant)``;
  * implement ``apply_catalog_card(card)`` — typically calling
    ``self._apply_catalog_common(card)`` then filling its own extra fields.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt, QThread, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QStyledItemDelegate,
)

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.ui.card_preview import install_hover_preview
from tcginventory.ui.completer_popup import show_fitted


class MatchDelegate(QStyledItemDelegate):
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


class PriceLookupWorker(QThread):
    """Fetch a picked card's tcgcsv printings (base + variants) off the UI thread.

    Emits ``list[CardVariant]`` (base first), possibly empty. Falls back to the
    single-product ``price_for_card`` (wrapped as one base variant) for cards
    whose exact number resolves to just one product.
    """

    done = Signal(object)  # list[CardVariant] (base first), possibly empty

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
        from tcginventory.services.pricing import CardVariant, TcgcsvProvider

        variants: list = []
        try:
            provider = TcgcsvProvider()
            variants = provider.variants_for_card(
                self._set, self._number, self._name,
                category=self._category, set_code=self._set_code,
            )
            if not variants:  # suffix-strip / single-product fallback
                result = provider.price_for_card(
                    self._set, self._number, self._name,
                    category=self._category, set_code=self._set_code,
                )
                if result:
                    variants = [CardVariant("Base", result[1], result[0], True)]
        except Exception:  # network/parse failure -> just skip the auto-price
            variants = []
        self.done.emit(variants)


class CardEntryMixin:
    """Catalog autocomplete, hover preview, and printing picker for a card row.

    Layout-agnostic: the host builds the widgets; this supplies the behaviour.
    """

    _CARD_ID_ROLE = Qt.UserRole + 1
    _LABEL_ROLE = Qt.UserRole + 2

    # -- catalog autocomplete ----------------------------------------------

    def _init_catalog_search(self) -> None:
        """Wire ``self.name`` to the local catalog. Call once, after the field
        and the ``_conn`` / ``_is_auto`` / ``_on_change`` / ``_on_pick`` /
        ``_sink`` attributes exist."""
        self._matches: dict[str, object] = {}
        self._card_id: str | None = None
        self._image_url: str | None = None
        self._search_model = QStandardItemModel(self.name)
        self._completer = QCompleter(self._search_model, self.name)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.name.setCompleter(self._completer)
        self.name.textEdited.connect(self._on_name_edited)
        self._completer.activated[QModelIndex].connect(self._pick_match)
        self.name.textChanged.connect(lambda *_: self._on_change())
        # Hover the Name field to preview the card image; hover a match in the
        # autofill popup to preview it before picking. Previews land in the
        # host screen's preview pane (the sink).
        install_hover_preview(self.name, self._hover_card, self._sink)
        popup = self._completer.popup()
        popup.setMouseTracking(True)
        popup.entered.connect(self._on_match_hover)
        self._match_delegate = MatchDelegate(self._LABEL_ROLE, popup)
        popup.setItemDelegate(self._match_delegate)
        install_hover_preview(popup, lambda: None, self._sink)  # clear on leave

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
            # Name field); the popup shows the fuller label via MatchDelegate.
            item = QStandardItem(card.name)
            label = (
                f"{card.name}  ·  {card.set_name or '?'}  "
                f"#{repo.display_number(card) or '?'}"
            )
            item.setData(card.card_id, self._CARD_ID_ROLE)
            item.setData(label, self._LABEL_ROLE)
            self._search_model.appendRow(item)
        if matches:
            show_fitted(self._completer, self.name)

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

    def _apply_catalog_common(self, card) -> str:
        """Fill the fields shared by every screen (identity + name/set/number)
        and preview the art. Returns the resolved display number; the host's
        ``apply_catalog_card`` fills its own extras and calls ``_on_change``."""
        self._card_id = card.card_id
        self._image_url = getattr(card, "image_small", None)
        self.name.setText(card.name)
        self.set.setText(card.set_name or "")
        # Catalog-derived denominator: correct for prefixed subsets like Radiant
        # Collection ("RC19/RC32"), where the parent set's total would be wrong.
        number = CardCatalogRepository(self._conn).display_number(card)
        number = number or card.full_number or card.number or ""
        self.number.setText(number)
        # Keep the chosen card showing in the pane after the popup closes.
        if self._sink is not None and self._image_url:
            self._sink.show_card(self._card_id, self._image_url)
        return number

    # -- printing picker ----------------------------------------------------

    def _init_printing_picker(self) -> None:
        """Create ``self.printing`` (hidden until a card has >1 TCGplayer
        product). The host places it in its layout and implements
        ``_apply_variant``."""
        self._variants: list = []
        self.printing = QComboBox()
        self.printing.setVisible(False)
        self.printing.activated.connect(self._on_printing_changed)

    def set_variants(self, variants: list) -> None:
        """Populate the printing picker from tcgcsv variants (base first). It
        shows only when there's a real choice (>1 printing)."""
        self._variants = variants or []
        self.printing.blockSignals(True)
        self.printing.clear()
        for v in self._variants:
            self.printing.addItem(v.label, v)
        self.printing.setCurrentIndex(0)
        self.printing.blockSignals(False)
        show = len(self._variants) > 1
        self.printing.setVisible(show)
        cell = getattr(self, "_printing_cell", None)
        if cell is not None:
            cell.setVisible(show)

    def _on_printing_changed(self, *_args) -> None:
        variant = self.printing.currentData()
        if variant is not None:
            self._apply_variant(variant)

    def _apply_variant(self, variant) -> None:  # pragma: no cover - overridden
        """Apply a chosen printing's price/URL. Host-specific; must override."""
        raise NotImplementedError
