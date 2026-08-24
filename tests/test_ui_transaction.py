"""Drive the write UI end-to-end: purchase → sale → trade → rip, edit, void.

Exercises the Transaction view and History actions (the M4 exit criterion:
a full lifecycle done through the UI, reconciling with the ledger).
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import type_catalog_search  # noqa: E402
from tcginventory.data.repositories import (  # noqa: E402
    ItemRepository,
    LotRepository,
    TransactionRepository,
)
from tcginventory.domain.enums import (  # noqa: E402
    Category,
    Intent,
    TransactionStatus,
)
from tcginventory.domain.models import Item  # noqa: E402
from tcginventory.services.inventory import InventoryService  # noqa: E402
from tcginventory.services.reporting import ReportingService  # noqa: E402
from tcginventory.services.transactions import IncomingCard, OutgoingCard  # noqa: E402


@pytest.fixture(autouse=True)
def _silence_dialogs(monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    # The success confirmation is a custom QDialog — don't block on exec().
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.Accepted)


def _inc(name, mv, intent=Intent.RESALE, qty=1) -> IncomingCard:
    return IncomingCard(Item(name=name, category=Category.SINGLE), qty, mv, intent)


def _first_item_id(conn, name):
    for item in ItemRepository(conn).list_all():
        if item.name == name:
            return item.id
    raise AssertionError(f"item {name} not found")


def test_autofill_radiant_collection_number(app, conn):
    """Picking a Radiant Collection card fills the subset denominator (RC2/RC25),
    not the parent set's total (RC2/RC113)."""
    from tcginventory.data.repositories import CardCatalogRepository
    from tcginventory.domain.models import CatalogCard
    from tcginventory.ui.views.transaction import TransactionView

    cards = [
        CatalogCard(
            card_id=f"bw11-RC{i}",
            name=("Servine" if i == 2 else f"C{i}"),
            set_id="bw11",
            set_name="Legendary Treasures",
            set_code="LTR",
            number=f"RC{i}",
            printed_total=113,
            language="EN",
        )
        for i in range(1, 26)  # RC1..RC25
    ]
    CardCatalogRepository(conn).replace_all(cards, "t")
    conn.commit()

    view = TransactionView(conn)
    row = view._add_receive_row()
    row.apply_catalog_card(CardCatalogRepository(conn).get("bw11-RC2"))
    assert row.number.text() == "RC2/RC25"


def test_add_receive_row_appends_a_card_widget(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    assert view._receive_rows == []
    assert view._recv_list.count() == 0
    row = view._add_receive_row()
    assert view._receive_rows == [row]
    assert view._recv_list.count() == 1  # the row's card widget in the list
    assert view._recv_list.itemAt(0).widget() is row._card_widget
    view.deleteLater()
    view.deleteLater()


def test_give_dropdown_shows_notes_without_review_marker(app, conn):
    from tcginventory.services.review import with_review_note
    from tcginventory.services.transactions import (
        TransactionRequest,
        TransactionService,
    )
    from tcginventory.ui.views.transaction import TransactionView

    # A resale card whose notes carry user text + the auto review marker.
    notes = with_review_note("Signed by artist")  # + "Manual price changed detected."
    TransactionService(conn).post(
        TransactionRequest(
            occurred_at="2026-07-20",
            cash_out_cents=1000,
            incoming=[
                IncomingCard(
                    item=Item(name="Pikachu", category=Category.SINGLE, notes=notes),
                    quantity=1,
                    market_value_cents=1000,
                    intent=Intent.RESALE,
                )
            ],
        )
    )
    view = TransactionView(conn)
    row = view._add_give_row()
    row._fill("")  # populate the completion model (as typing/focus would)
    labels = [row._model.item(i).text() for i in range(row._model.rowCount())]

    assert labels
    assert any("Signed by artist" in label for label in labels)  # user note shown
    assert all("on hand" not in label for label in labels)  # count removed
    assert all("Manual price" not in label for label in labels)  # marker stripped
    view.deleteLater()


def test_give_side_hover_resolves_card_image(app, conn):
    from tcginventory.data.repositories import CardCatalogRepository
    from tcginventory.domain.models import CatalogCard
    from tcginventory.services.transactions import (
        TransactionRequest,
        TransactionService,
    )
    from tcginventory.ui.views.transaction import TransactionView

    # Catalog card with an image, matching the purchased card's identity.
    CardCatalogRepository(conn).replace_all(
        [
            CatalogCard(
                card_id="cel-5",
                name="Pikachu",
                set_id="cel",
                set_name="Celebrations",
                set_code="CEL",
                number="5",
                image_small="http://img/pika.png",
                language="EN",
            )
        ],
        "t",
    )
    # Buy one so it's sellable (resale) stock the give side can pick.
    TransactionService(conn).post(
        TransactionRequest(
            occurred_at="2026-07-20",
            cash_out_cents=1000,
            incoming=[
                IncomingCard(
                    item=Item(
                        name="Pikachu",
                        category=Category.SINGLE,
                        set_name="Celebrations",
                        card_number="5",
                    ),
                    quantity=1,
                    market_value_cents=1000,
                    intent=Intent.RESALE,
                )
            ],
        )
    )
    view = TransactionView(conn)
    row = view._add_give_row()
    assert row is not None
    # The give row starts empty — nothing to preview until a card is chosen.
    assert row._hover_card() is None
    assert row.to_card() is None
    # After choosing a card, hovering resolves its catalog image.
    row.set_item(_first_item_id(conn, "Pikachu"), 1)
    assert row._hover_card() == ("cel-5", "http://img/pika.png")
    view.deleteLater()


def test_ui_purchase_infers_type_and_allocates(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    view.add_incoming(_inc("A", 6000))
    view.add_incoming(_inc("B", 4000, Intent.PERSONAL))
    view._cash_out.setValue(80.00)

    assert view._badge.text() == "PURCHASE"
    assert view._submit.isEnabled()

    view.submit()

    rows = {r.item.name: r for r in InventoryService(conn).inventory_rows()}
    assert rows["A"].purchase_price_cents == 4800  # 80 × 60/100
    assert rows["B"].purchase_price_cents == 3200  # 80 × 40/100
    # Form cleared after a successful post.
    assert view._badge.text() == "—"


def test_ui_sale_realizes_margin(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    view.add_incoming(_inc("A", 9000))
    view._cash_out.setValue(90.00)
    view.submit()

    item_id = _first_item_id(conn, "A")
    view.add_outgoing(OutgoingCard(item_id, 1), "A")
    view._cash_in.setValue(120.00)
    assert view._badge.text() == "SALE"
    view.submit()

    assert ReportingService(conn).realized_pl_cents() == 3000
    assert InventoryService(conn).on_hand(item_id) == 0


def test_ui_give_row_caps_quantity_to_on_hand(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    view.add_incoming(_inc("A", 1000))
    view._cash_out.setValue(10.00)
    view.submit()

    item_id = _first_item_id(conn, "A")
    view.add_outgoing(OutgoingCard(item_id, 5), "A")  # only 1 on hand
    # The inline row caps quantity at what's available, so oversell can't be
    # entered; a valid one-card sale is submittable.
    assert view._give_rows[0].qty.value() == 1
    view._cash_in.setValue(5.00)
    assert view._submit.isEnabled() is True


def _buy(conn, item: Item, mv=1000):
    """Purchase one of ``item`` so it becomes sellable give-side stock."""
    from tcginventory.services.transactions import (
        TransactionRequest,
        TransactionService,
    )

    TransactionService(conn).post(
        TransactionRequest(
            occurred_at="2026-08-24",
            cash_out_cents=mv,
            incoming=[IncomingCard(item, 1, mv, Intent.RESALE)],
        )
    )


def test_give_row_tells_two_conditions_of_one_card_apart(app, conn):
    """Two Haunters differing only in wear must not read alike in the picker.

    Both used to render "Haunter (Fossil)", so a user could not see — and a test
    could not assert — which lot a row was about to draw down.
    """
    from tcginventory.domain.enums import Condition
    from tcginventory.ui.views.transaction import TransactionView

    for condition in (Condition.MP, Condition.LP):
        _buy(
            conn,
            Item(
                name="Haunter",
                category=Category.SINGLE,
                set_name="Fossil",
                card_number="6/62",
                condition=condition,
            ),
        )

    view = TransactionView(conn)
    row = view._add_give_row()
    row._fill("")
    labels = [row._model.item(i).text() for i in range(row._model.rowCount())]

    assert sorted(labels) == [
        "Haunter (Fossil · 6/62 · LP)",
        "Haunter (Fossil · 6/62 · MP)",
    ]
    view.deleteLater()


def test_give_row_caps_how_many_matches_the_popup_holds(app, conn):
    """The popup is fitted to its widest row, and re-fitting a wide list is
    super-linear in row count: over a 769-card collection an uncapped refill
    cost 1.73 s of UI-thread work, against 3.9 ms at this limit."""
    from tcginventory.services.transactions import (
        TransactionRequest,
        TransactionService,
    )
    from tcginventory.ui.views.transaction import _MATCH_LIMIT, TransactionView

    count = _MATCH_LIMIT + 5
    TransactionService(conn).post(
        TransactionRequest(
            occurred_at="2026-08-24",
            cash_out_cents=100 * count,
            incoming=[
                IncomingCard(
                    Item(name=f"Card {n:03d}", category=Category.SINGLE),
                    1,
                    100,
                    Intent.RESALE,
                )
                for n in range(count)
            ],
        )
    )

    view = TransactionView(conn)
    row = view._add_give_row()
    row._fill("")
    assert row._model.rowCount() == _MATCH_LIMIT
    view.deleteLater()


def test_give_row_picks_the_second_of_two_identical_looking_cards(app, conn):
    """Picking row 2 must select row 2's item — and keep it selected.

    ``_on_text_changed`` re-derives the label to decide the selection still
    stands, so an unstable label would silently clear the pick right after it
    was made.
    """
    from tcginventory.domain.enums import Condition
    from tcginventory.ui.views.transaction import TransactionView

    for condition in (Condition.MP, Condition.LP):
        _buy(
            conn,
            Item(
                name="Haunter",
                category=Category.SINGLE,
                set_name="Fossil",
                card_number="6/62",
                condition=condition,
            ),
        )

    view = TransactionView(conn)
    row = view._add_give_row()
    row._fill("")
    wanted = next(
        i
        for i in range(row._model.rowCount())
        if row._model.item(i).text().endswith("· LP)")
    )
    entry = row._model.item(wanted)

    row._set_chosen(entry.data(row._ITEM_ID_ROLE))

    card = row.to_card()
    assert card is not None, "the label write-back cleared the selection"
    chosen = ItemRepository(conn).get(card.item_id)
    assert chosen.condition is Condition.LP
    view.deleteLater()


def test_ui_trade_and_rip_lifecycle(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)

    # Purchase a card to trade away later.
    view.add_incoming(_inc("Snorlax", 1000))
    view._cash_out.setValue(10.00)
    view.submit()
    snorlax = _first_item_id(conn, "Snorlax")

    # Trade Snorlax (market $10) for Gengar (market $12) + $3 cash back.
    view.add_outgoing(OutgoingCard(snorlax, 1), "Snorlax")
    view.add_incoming(_inc("Gengar", 1200))
    view._cash_in.setValue(3.00)
    assert view._badge.text() == "TRADE"
    view.submit()

    # Snorlax proceeds = cash_in 300 + Gengar market 1200 = 1500; − 1000 cost.
    assert ReportingService(conn).realized_pl_cents() == 500
    # Gengar basis = Snorlax market 1000 − cash back 300 = 700.
    gengar = _first_item_id(conn, "Gengar")
    (gengar_lot,) = LotRepository(conn).list_for_item(gengar)
    assert gengar_lot.unit_cost_cents == 700

    # Rip: buy a sealed box tagged ripping → valued at $0 (a loss).
    view.add_incoming(_inc("Booster Box", 12000, Intent.RIPPING))
    view._cash_out.setValue(110.00)
    view.submit()
    box = _first_item_id(conn, "Booster Box")
    pos = InventoryService(conn).item_position(box)
    assert pos.market_value_cents == 0
    assert pos.unrealized_pl_cents == -11000

    # Ripping lot is not sellable.
    assert box not in {i.id for i, _ in InventoryService(conn).sellable_items()}


def test_incoming_dialog_returns_enums_and_defaults_resale(app):
    from tcginventory.domain.enums import Category, Condition, Language
    from tcginventory.ui.views._card_dialogs import IncomingCardDialog

    dlg = IncomingCardDialog()
    dlg._name.setText("Test Card")
    dlg._market.setValue(50.0)
    card = dlg.result_card()

    assert card.intent is Intent.RESALE  # default is Resale
    # Real enums, not the plain strings QComboBox.currentData() hands back.
    assert isinstance(card.item.category, Category)
    assert isinstance(card.item.condition, Condition)
    assert isinstance(card.item.language, Language)
    assert card.market_value_cents == 5000
    dlg.deleteLater()


def test_incoming_card_renders_and_posts_via_dialog(app, conn):
    from tcginventory.data.repositories import ItemRepository
    from tcginventory.ui.views._card_dialogs import IncomingCardDialog
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    dlg = IncomingCardDialog()
    dlg._name.setText("Blastoise")
    dlg._set_name.setText("Base")
    dlg._market.setValue(50.0)
    view.add_incoming(dlg.result_card())

    # The inline receive row carries the card's fields as editable widgets.
    row = view._receive_rows[0]
    assert row.name.text() == "Blastoise"
    assert row.condition.currentText() == "NM"
    assert row.market.value() == 50.0
    assert row.intent.currentText() == "Resale"

    view._cash_out.setValue(50.0)
    view.submit()
    assert any(i.name == "Blastoise" for i in ItemRepository(conn).list_all())
    dlg.deleteLater()


def test_ui_give_dialog_sku_lookup(app, conn):
    from conftest import buy
    from tcginventory.services.inventory import InventoryService
    from tcginventory.services.transactions import TransactionService
    from tcginventory.ui.views._card_dialogs import OutgoingCardDialog

    svc = TransactionService(conn)
    buy(svc, "Snorlax", 1000, 1000, "2026-01-01", set_code="BS", card_number="011/102")
    lot_sku = InventoryService(conn).inventory_rows()[0].sku

    sellable = InventoryService(conn).sellable_items()
    dialog = OutgoingCardDialog(sellable, conn)
    dialog._sku.setText(lot_sku)
    dialog._lookup_sku()
    # The combo now points at the looked-up card. Assert the *item*, not the
    # label: the label carries set/number (and any disambiguator) by design, so
    # matching it exactly would pin this test to formatting it does not own.
    card, name = dialog.result_card()
    assert ItemRepository(conn).get(card.item_id).name == "Snorlax"
    assert name.startswith("Snorlax")
    assert "on hand" not in name  # the count is still split off
    dialog.deleteLater()


def test_removing_a_middle_receive_row_does_not_crash(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    r0 = view._add_receive_row()
    view._add_receive_row()
    r2 = view._add_receive_row()
    r0.name.setText("first")
    r2.name.setText("third")

    # Remove the first (not the last) row — used to delete surviving widgets.
    view._remove_row("in", r0)
    assert len(view._receive_rows) == 2
    assert view._recv_list.count() == 2  # two card widgets remain in the list
    # Surviving rows' widgets are still live and correct.
    assert view._receive_rows[-1].name.text() == "third"
    # Deleting again still works.
    view._remove_row("in", r2)
    assert len(view._receive_rows) == 1
    view.deleteLater()


def test_receive_row_autofills_set_and_number_from_catalog(app, conn):
    from tcginventory.data.repositories import CardCatalogRepository
    from tcginventory.domain.models import CatalogCard
    from tcginventory.ui.views.transaction import TransactionView

    CardCatalogRepository(conn).replace_all(
        [
            CatalogCard(
                card_id="sv9-163",
                name="Iono's Kilowattrel",
                set_id="sv9",
                set_name="Journey Together",
                set_code="JTG",
                number="163",
            )
        ],
        "2026-07-13T00:00:00Z",
    )
    conn.commit()

    view = TransactionView(conn)
    view._auto_detect_cb.setChecked(True)
    row = view._add_receive_row()
    # Typing a name (+set) searches the catalog; picking a match fills fields.
    type_catalog_search(row.name, "Kilowattrel Journey Together", row)
    (card,) = row._matches.values()
    row.apply_catalog_card(card)
    assert row.name.text() == "Iono's Kilowattrel"
    assert row.set.text() == "Journey Together"
    assert row.number.text() == "163"
    assert row.category.currentData() == "single"


def test_ui_edit_reposts_and_history_void(app, conn):
    from tcginventory.ui.views.history import HistoryView
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    view.add_incoming(_inc("A", 8000))
    view._cash_out.setValue(80.00)
    view.submit()
    old_id = TransactionRepository(conn).list_all()[0].id

    # Edit: change the price to $90; original is voided, a new txn posted.
    assert view.load_transaction(old_id)
    view._cash_out.setValue(90.00)
    view.submit()

    assert TransactionRepository(conn).get(old_id).status is TransactionStatus.VOIDED
    item_id = _first_item_id(conn, "A")
    (lot,) = LotRepository(conn).open_lots_for_item(item_id)
    assert lot.unit_cost_cents == 9000

    # Void the re-posted purchase from History → lot removed.
    hist = HistoryView(conn)
    posted = [
        r
        for r in TransactionRepository(conn).list_all()
        if r.status is TransactionStatus.POSTED
    ]
    # select the posted row in the table and void it
    for proxy_row in range(hist._proxy.rowCount()):
        src = hist._proxy.mapToSource(hist._proxy.index(proxy_row, 0))
        if hist._model.row_at(src.row()).transaction.id == posted[0].id:
            hist._table.selectRow(proxy_row)
            break
    hist._void_selected()

    voided = TransactionRepository(conn).get(posted[0].id)
    assert voided.status is TransactionStatus.VOIDED
    assert LotRepository(conn).list_for_item(item_id) == []


def test_transaction_has_docked_card_preview(app, conn):
    """Each side has its own docked preview pane; card rows feed the pane on their
    side on hover / search so the hovered card's art shows in that space."""
    from tcginventory.ui.card_preview import CardImagePane
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    assert isinstance(view._sink_you, CardImagePane)
    assert isinstance(view._sink_them, CardImagePane)
    # Rows are wired to feed their own side's docked pane on hover / search.
    receive = view._add_receive_row()
    assert receive._sink is view._sink_them
    give = view._add_give_row()  # None when there's no sellable stock to give
    if give is not None:
        assert give._sink is view._sink_you
    view.deleteLater()


def test_receive_row_printing_picker(app, conn):
    from tcginventory.services.pricing import CardVariant
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    row = view._add_receive_row()

    # A single printing → no choice to make, picker stays hidden.
    row.set_variants([CardVariant("Base", "u-base", 1370, True)])
    assert row._printing_cell.isHidden()

    # Two printings → picker shows, base selected, base price/url applied earlier.
    variants = [
        CardVariant("Base", "u-base", 1370, True),
        CardVariant("Pokemon Center Exclusive", "u-excl", 9413, False),
    ]
    row.set_variants(variants)
    assert not row._printing_cell.isHidden()
    assert [row.printing.itemText(i) for i in range(row.printing.count())] == [
        "Base",
        "Pokemon Center Exclusive",
    ]

    # Choosing the exclusive re-links the URL and market price.
    row.printing.setCurrentIndex(1)
    row._on_printing_changed()
    assert row.tcgplayer.text() == "u-excl"
    assert round(row.market.value() * 100) == 9413
    view.deleteLater()
