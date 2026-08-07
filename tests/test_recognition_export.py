"""Recognition → Transaction / Auction export: payload collection + ingestion."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt  # noqa: E402

from tcginventory.data.auctions import open_auctions  # noqa: E402
from tcginventory.data.vision import CardLabel  # noqa: E402
from tcginventory.domain.enums import Category  # noqa: E402
from tcginventory.domain.models import Item  # noqa: E402


@pytest.fixture(autouse=True)
def _silence(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)


def _png(path, w=200, h=100):
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor("white"))
    img.save(str(path))
    return str(path)


def _rc(name, **kw):
    from tcginventory.ui.views.recognition import RecognizedCard

    return RecognizedCard(
        card_id=kw.get("card_id"),
        name=name,
        set_name=kw.get("set_name"),
        card_number=kw.get("card_number"),
        language=kw.get("language"),
        quantity=kw.get("quantity", 1),
        side=kw.get("side"),
        condition=kw.get("condition"),
        is_graded=kw.get("is_graded", False),
        grade=kw.get("grade"),
        grading_company=kw.get("grading_company"),
    )


# -- Recognition: collecting the export payload -----------------------------


def test_collect_assigns_sides_and_aggregates_duplicates(app, conn, tmp_path):
    from tcginventory.ui.views.recognition import RecognitionView

    view = RecognitionView(conn)
    view._mode.setCurrentIndex(0)  # Transaction (I Give / They Give)
    view.add_image_paths([_png(tmp_path / "scene.png", 200, 100)])
    # Two of the same card on the left (→ give), one different on the right.
    for rect in (QRectF(5, 5, 40, 60), QRectF(20, 20, 40, 60)):
        view._add_box(rect).card = CardLabel(card_id="cel-4", name="Charizard")
    view._add_box(QRectF(150, 5, 40, 60)).card = CardLabel(
        card_id="cel-5", name="Blastoise"
    )

    cards = view._collect_export_cards()
    by_name = {c.name: c for c in cards}
    assert by_name["Charizard"].side == "give"
    assert by_name["Charizard"].quantity == 2  # duplicates aggregated
    assert by_name["Blastoise"].side == "receive"
    assert by_name["Blastoise"].quantity == 1
    view.deleteLater()


def test_listing_mode_cards_have_no_side(app, conn, tmp_path):
    from tcginventory.ui.views.recognition import RecognitionView

    view = RecognitionView(conn)
    view._mode.setCurrentIndex(1)  # Listing (one lot)
    view.add_image_paths([_png(tmp_path / "lot.png")])
    view._add_box(QRectF(10, 10, 40, 60)).card = CardLabel(name="Pikachu")

    cards = view._collect_export_cards()
    assert len(cards) == 1
    assert cards[0].side is None
    assert cards[0].name == "Pikachu"
    view.deleteLater()


def test_unlabeled_boxes_are_skipped(app, conn, tmp_path):
    from tcginventory.ui.views.recognition import RecognitionView

    view = RecognitionView(conn)
    view._mode.setCurrentIndex(1)
    view.add_image_paths([_png(tmp_path / "lot.png")])
    view._add_box(QRectF(10, 10, 40, 60))  # never labeled
    view._add_box(QRectF(60, 10, 40, 60)).card = CardLabel(name="Gengar")

    cards = view._collect_export_cards()
    assert [c.name for c in cards] == ["Gengar"]
    view.deleteLater()


# -- Transaction ingestion ---------------------------------------------------


def test_transaction_import_fills_receive_and_reports_unmatched_give(app, conn):
    from tcginventory.ui.views.transaction import TransactionView

    view = TransactionView(conn)
    cards = [
        _rc("Blastoise", side="receive", condition="LP"),
        _rc("Charizard", side="give", card_number="4/102"),  # nothing owned
    ]
    unmatched = view.import_recognized(cards)

    assert len(view._receive_rows) == 1
    assert view._receive_rows[0].name.text() == "Blastoise"
    assert view._receive_rows[0].condition.currentData() == "LP"
    assert len(view._give_rows) == 0  # give card had no inventory match
    assert unmatched == ["Charizard #4/102"]
    view.deleteLater()


def test_match_sellable_by_identity():
    from tcginventory.ui.views.transaction import _match_sellable

    def item(id_, name, number, set_name=None):
        it = Item(
            name=name,
            category=Category.SINGLE,
            set_name=set_name,
            card_number=number,
        )
        it.id = id_
        return it

    sellable = [
        (item(1, "Charizard", "4/102", "Base"), 3),
        (item(2, "Blastoise", "2/102", "Base"), 1),
    ]
    assert _match_sellable(sellable, _rc("Charizard", card_number="4/102")) == 1
    assert _match_sellable(sellable, _rc("Blastoise", card_number="2")) == 2
    assert _match_sellable(sellable, _rc("Charizard")) == 1  # name-only fallback
    assert _match_sellable(sellable, _rc("Mewtwo")) is None


# -- Recognition History: re-exporting a stored capture ----------------------


def _stored_capture(vrepo, *, source="transaction"):
    """A capture with: two identical give-side Charizards, one receive-side
    Blastoise, an unlabeled box, and a back-side shot of the Charizard."""
    cap = vrepo.add_capture(source=source, now="2026-03-01T00:00:00+00:00")
    front = vrepo.add_image(cap, "front-sha", "/front.png", "front")
    zard = CardLabel(card_id="cel-4", name="Charizard", condition="NM")
    vrepo.add_region(front, 1, (0.0, 0.0, 0.2, 0.4), "give", zard)
    vrepo.add_region(front, 2, (0.2, 0.0, 0.2, 0.4), "give", zard)  # duplicate
    vrepo.add_region(
        front, 3, (0.6, 0.0, 0.2, 0.4), "receive", CardLabel(name="Blastoise")
    )
    vrepo.add_region(
        front, 4, (0.8, 0.0, 0.1, 0.2), "receive", CardLabel()
    )  # unlabeled
    back = vrepo.add_image(cap, "back-sha", "/back.png", "back")
    vrepo.add_region(back, 1, (0.0, 0.0, 0.2, 0.4), "give", zard)  # same card's back
    return cap


def test_capture_export_cards_rebuilds_the_payload():
    """A stored capture re-exports exactly what the live window would collect."""
    from tcginventory.data.vision import VisionRepository, open_vision
    from tcginventory.services.corpus import capture_export_cards

    vrepo = VisionRepository(open_vision(":memory:"))
    cap = _stored_capture(vrepo)

    cards = capture_export_cards(vrepo, cap)
    assert [(c.name, c.side, c.quantity) for c in cards] == [
        ("Charizard", "give", 2),  # duplicates aggregated, back not double-counted
        ("Blastoise", "receive", 1),  # unlabeled box skipped
    ]
    assert cards[0].card_id == "cel-4"
    assert cards[0].condition == "NM"


def _click_export(rec, row: int, which: int) -> None:
    """Click one of a row's Export-column buttons the way a user would."""
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    from tcginventory.ui.models.recognition_history_model import EXPORT_COL

    index = rec._proxy.index(row, EXPORT_COL)
    delegate = rec._table.itemDelegateForColumn(EXPORT_COL)
    cell = rec._table.visualRect(index)
    center = delegate._button_rects(cell)[which].center()
    QTest.mouseClick(
        rec._table.viewport(),
        Qt.LeftButton,
        pos=QPoint(int(center.x()), int(center.y())),
    )


def test_recognition_history_exports_a_row_from_its_export_buttons(app, conn):
    """Each row's Export column hands that capture's cards to Transaction /
    Auction — no selection needed, and rows with nothing labeled don't fire."""
    from tcginventory.data.vision import CardLabel, VisionRepository, open_vision
    from tcginventory.ui.views.history import HistoryView

    vrepo = VisionRepository(open_vision(":memory:"))
    _stored_capture(vrepo, source="listing")  # newest → row 0
    bare = vrepo.add_capture(source="manual", now="2026-01-01T00:00:00+00:00")
    img = vrepo.add_image(bare, "bare-sha", "/bare.png", "front")
    vrepo.add_region(img, 1, (0.0, 0.0, 0.2, 0.4), None, CardLabel())  # unlabeled

    view = HistoryView(conn, vision_repo=vrepo)
    view.resize(1100, 600)
    view.show()  # a real geometry, so visualRect gives clickable coordinates
    view._set_mode(1)  # → Recognition History
    app.processEvents()
    rec = view._rec
    assert rec._model.rowCount() == 2

    to_txn, to_auc = [], []
    view.export_to_transaction.connect(to_txn.append)  # relayed by HistoryView
    view.export_to_auction.connect(to_auc.append)

    _click_export(rec, 0, 1)  # the labeled capture → Auction
    assert len(to_auc) == 1
    assert [(c.name, c.quantity) for c in to_auc[0]] == [
        ("Charizard", 2),
        ("Blastoise", 1),
    ]
    _click_export(rec, 0, 0)  # → Transaction
    assert len(to_txn) == 1

    _click_export(rec, 1, 0)  # the capture with nothing labeled: dimmed, inert
    _click_export(rec, 1, 1)
    assert (len(to_txn), len(to_auc)) == (1, 1)

    view.close()
    view.deleteLater()


def test_export_column_replaced_the_note_column(app, conn):
    """The Note column gave up its slot to the buttons; the note lives on as a
    tooltip so it isn't lost."""
    from tcginventory.data.vision import VisionRepository, open_vision
    from tcginventory.ui.models.recognition_history_model import EXPORT_COL
    from tcginventory.ui.views.history import HistoryView

    vrepo = VisionRepository(open_vision(":memory:"))
    cap = _stored_capture(vrepo)
    vrepo.conn.execute("UPDATE capture SET note = ? WHERE id = ?", ("box break", cap))
    vrepo.conn.commit()

    view = HistoryView(conn, vision_repo=vrepo)
    view._set_mode(1)
    model = view._rec._model
    headers = [model.headerData(c, Qt.Horizontal) for c in range(model.columnCount())]
    assert "Note" not in headers
    assert headers[EXPORT_COL] == "Export"
    assert model.data(model.index(0, EXPORT_COL), Qt.DisplayRole) == ""
    assert model.data(model.index(0, 0), Qt.ToolTipRole) == "box break"
    view.deleteLater()


def test_history_export_payload_feeds_both_windows(app, conn):
    """The stored-capture payload is accepted by the real ingestion methods."""
    from tcginventory.data.vision import VisionRepository, open_vision
    from tcginventory.services.corpus import capture_export_cards
    from tcginventory.ui.views.auctions import AuctionsView
    from tcginventory.ui.views.transaction import TransactionView

    vrepo = VisionRepository(open_vision(":memory:"))
    cards = capture_export_cards(vrepo, _stored_capture(vrepo))

    txn = TransactionView(conn)
    unmatched = txn.import_recognized(cards)
    assert [r.name.text() for r in txn._receive_rows] == ["Blastoise"]
    assert unmatched == ["Charizard"]  # give side, nothing owned to match
    txn.deleteLater()

    auc = AuctionsView(conn, auctions_conn=open_auctions(":memory:"), history=None)
    auc.import_recognized(cards)
    assert [r.name.text() for r in auc._rows] == ["Charizard", "Blastoise"]
    assert auc._rows[0].qty.value() == 2
    auc.deleteLater()


# -- Auction ingestion -------------------------------------------------------


def test_auction_import_fills_editor_rows(app, conn):
    from tcginventory.ui.views.auctions import AuctionsView

    adb = open_auctions(":memory:")
    view = AuctionsView(conn, auctions_conn=adb, history=None)
    view.import_recognized([_rc("Pikachu", condition="NM", quantity=2)])

    assert len(view._rows) == 1
    assert view._rows[0].name.text() == "Pikachu"
    assert view._rows[0].qty.value() == 2
    view.deleteLater()
