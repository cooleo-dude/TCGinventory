"""Recognition → Transaction / Auction export: payload collection + ingestion."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF  # noqa: E402

from tcginventory.data.auctions import open_auctions  # noqa: E402
from tcginventory.data.db import connect  # noqa: E402
from tcginventory.data.migrator import migrate  # noqa: E402
from tcginventory.data.vision import CardLabel  # noqa: E402
from tcginventory.domain.enums import Category  # noqa: E402
from tcginventory.domain.models import Item  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def conn():
    c = connect(":memory:")
    migrate(c)
    yield c
    c.close()


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
