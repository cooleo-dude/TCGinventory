"""Inference autofill UI (Part 2): the review dialog + Transaction/Auction wiring."""

from __future__ import annotations

import os

import pytest
from PIL import Image

from tcginventory.data.vision import CardLabel
from tcginventory.services.inference import Detection

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _scene(path, w=200, h=100):
    Image.new("RGB", (w, h), "white").save(path)
    return str(path)


def _det(number, box, label, score, confident):
    return Detection(number, box, label, score, confident, [])


def test_autofill_dialog_maps_include_edit_and_confidence(app, tmp_path):
    from tcginventory.ui.views._autofill_dialog import AutofillDialog

    scene = _scene(tmp_path / "scene.png")
    dets = [
        _det(
            1,
            (0.0, 0.0, 0.3, 1.0),
            CardLabel(card_id="cel-4", name="Charizard"),
            0.95,
            True,
        ),
        _det(
            2,
            (0.3, 0.0, 0.3, 1.0),
            CardLabel(card_id="cel-5", name="Blastoise"),
            0.40,
            False,
        ),
        _det(3, (0.6, 0.0, 0.3, 1.0), None, 0.0, False),  # unidentified → no name
    ]
    dlg = AutofillDialog(scene, dets)

    cards = dlg.result_cards()
    assert len(cards) == 2  # the unidentified, name-less row is excluded
    assert cards[0].card_id == "cel-4" and cards[0].review is False
    assert cards[1].card_id == "cel-5" and cards[1].review is True  # low confidence

    dlg._rows[0].include.setChecked(False)  # deselect the confident one
    assert len(dlg.result_cards()) == 1

    dlg._rows[1].name.setText("Hand typed")  # editing drops the guessed id
    edited = dlg.result_cards()[0]
    assert edited.card_id is None and edited.review is True
    dlg.deleteLater()


def test_autofill_dialog_live_count_and_hover_highlight(app, tmp_path):
    """The header count / primary button track what's actually going in, and
    hovering a row cross-highlights its box in the scene."""
    from tcginventory.ui.views._autofill_dialog import AutofillDialog

    scene = _scene(tmp_path / "scene.png")
    dets = [
        _det(
            1,
            (0.0, 0.0, 0.4, 1.0),
            CardLabel(card_id="cel-4", name="Charizard"),
            0.9,
            True,
        ),
        _det(
            2,
            (0.5, 0.0, 0.4, 1.0),
            CardLabel(card_id="cel-5", name="Blastoise"),
            0.3,
            False,
        ),
    ]
    dlg = AutofillDialog(scene, dets)

    assert dlg._count.text() == "2 to add"
    dlg._rows[1].include.setChecked(False)
    assert dlg._count.text() == "1 to add"
    assert dlg._cards[2].property("off") is True
    dlg._rows[0].include.setChecked(False)
    assert dlg._count.text() == "0 to add"
    assert not dlg._add.isEnabled()  # nothing to add → can't submit

    dlg._on_hover(1, True)
    assert dlg._cards[1].property("hl") is True
    assert dlg._box_items[1].zValue() == 1
    dlg._on_hover(1, False)
    assert dlg._cards[1].property("hl") is False
    dlg.deleteLater()


def _patch_flow(monkeypatch, scene, dialog_cls):
    from PySide6.QtWidgets import QDialog, QFileDialog

    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([scene], ""))
    )
    monkeypatch.setattr(dialog_cls, "exec", lambda self: QDialog.Accepted)


def test_transaction_autofill_adds_receive_rows(app, monkeypatch, tmp_path):
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views import transaction as txn_mod
    from tcginventory.ui.views.transaction import TransactionView

    scene = _scene(tmp_path / "scene.png")
    conn = connect(":memory:")
    migrate(conn)
    view = TransactionView(conn)
    view._inference_pipeline = lambda _p: [
        _det(
            1,
            (0, 0, 0.5, 1),
            CardLabel(name="Charizard", set_name="Base", card_number="4"),
            0.9,
            True,
        ),
        _det(2, (0.5, 0, 0.5, 1), None, 0.0, False),  # dropped (no name)
    ]
    _patch_flow(monkeypatch, scene, txn_mod.AutofillDialog)

    view._autofill_from_photo()

    assert len(view._receive_rows) == 1
    assert view._receive_rows[0].name.text() == "Charizard"
    assert view._receive_rows[0].review.isChecked()
    view.deleteLater()


def test_transaction_autofill_from_clipboard_adds_receive_rows(
    app, monkeypatch, tmp_path
):
    """The clipboard button runs the same flow, sourcing the scene from a pasted
    bitmap (spooled to a temp PNG) instead of a file dialog."""
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QDialog

    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui import clipboard_images
    from tcginventory.ui.views import transaction as txn_mod
    from tcginventory.ui.views.transaction import TransactionView

    conn = connect(":memory:")
    migrate(conn)
    view = TransactionView(conn)
    view._inference_pipeline = lambda _p: [
        _det(
            1,
            (0, 0, 1, 1),
            CardLabel(name="Snorlax", set_name="Base", card_number="11"),
            0.9,
            True,
        )
    ]
    monkeypatch.setattr(txn_mod.AutofillDialog, "exec", lambda self: QDialog.Accepted)

    # No image on the clipboard → an explanatory prompt, no rows.
    monkeypatch.setattr(txn_mod, "clipboard_image_paths", lambda: [])
    seen: list[str] = []
    monkeypatch.setattr(
        txn_mod.QMessageBox,
        "information",
        staticmethod(lambda *a, **k: seen.append(a[1])),
    )
    view._autofill_from_clipboard()
    assert seen and not view._receive_rows

    # A pasted bitmap is spooled to a real file the pipeline can read.
    image = QImage(200, 100, QImage.Format_RGB32)
    image.fill(0xFFFFFF)
    pasted = clipboard_images.spool_image(image)
    assert os.path.exists(pasted)
    monkeypatch.setattr(txn_mod, "clipboard_image_paths", lambda: [pasted])

    view._autofill_from_clipboard()

    assert len(view._receive_rows) == 1
    assert view._receive_rows[0].name.text() == "Snorlax"
    view.deleteLater()


class _CorpusPipe:
    """A pipeline stand-in that exposes repo + recognizer for write-back."""

    def __init__(self, repo, recognizer, detections):
        self.repo = repo
        self.recognizer = recognizer
        self._detections = detections

    def __call__(self, _path):
        return self._detections


def test_transaction_autofill_writes_confirmed_cards_back_to_corpus(
    app, monkeypatch, tmp_path
):
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.data.vision import VisionRepository, open_vision
    from tcginventory.services.recognizer import PixelEmbedder, Recognizer
    from tcginventory.ui.views import transaction as txn_mod
    from tcginventory.ui.views.transaction import TransactionView

    scene = _scene(tmp_path / "scene.png")
    conn = connect(":memory:")
    migrate(conn)
    vrepo = VisionRepository(open_vision(":memory:"))
    pipe = _CorpusPipe(
        vrepo,
        Recognizer(vrepo, embedder=PixelEmbedder(24)),
        [
            _det(
                1,
                (0, 0, 0.5, 1),
                CardLabel(card_id="cel-4", name="Charizard"),
                0.9,
                True,
            ),
            _det(2, (0.5, 0, 0.5, 1), None, 0.0, False),  # unidentified → dropped
        ],
    )
    view = TransactionView(conn)
    view._inference_pipeline = pipe
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))  # isolate stored image
    _patch_flow(monkeypatch, scene, txn_mod.AutofillDialog)

    view._autofill_from_photo()

    # The one confirmed, identified card was fed back: a region + a crop exemplar.
    assert vrepo.region_count() == 1
    assert vrepo.region_embedding_count() == 1
    assert [r[0] for r in vrepo.region_vectors("pixel-24")] == ["cel-4"]
    view.deleteLater()


def test_auction_autofill_adds_card_rows(app, monkeypatch, tmp_path):
    from tcginventory.data.auctions import open_auctions
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views import auctions as auc_mod
    from tcginventory.ui.views.auctions import AuctionsView

    scene = _scene(tmp_path / "scene.png")
    conn = connect(":memory:")
    migrate(conn)
    view = AuctionsView(conn, auctions_conn=open_auctions(":memory:"), history=None)
    view._inference_pipeline = lambda _p: [
        _det(
            1,
            (0, 0, 0.5, 1),
            CardLabel(name="Pikachu", set_name="Base", card_number="58"),
            0.8,
            True,
        ),
    ]
    _patch_flow(monkeypatch, scene, auc_mod.AutofillDialog)

    view._autofill_from_photo()

    assert len(view._rows) == 1
    assert view._rows[0].name.text() == "Pikachu"
    view.deleteLater()


def test_auction_autofill_from_clipboard_adds_card_rows(app, monkeypatch, tmp_path):
    """The auction editor's clipboard button runs the same flow from a pasted
    bitmap, and says so when the clipboard holds no image."""
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QDialog

    from tcginventory.data.auctions import open_auctions
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui import clipboard_images
    from tcginventory.ui.views import auctions as auc_mod
    from tcginventory.ui.views.auctions import AuctionsView

    conn = connect(":memory:")
    migrate(conn)
    view = AuctionsView(conn, auctions_conn=open_auctions(":memory:"), history=None)
    view._inference_pipeline = lambda _p: [
        _det(
            1,
            (0, 0, 1, 1),
            CardLabel(name="Gengar", set_name="Fossil", card_number="5"),
            0.85,
            True,
        )
    ]
    monkeypatch.setattr(auc_mod.AutofillDialog, "exec", lambda self: QDialog.Accepted)

    monkeypatch.setattr(auc_mod, "clipboard_image_paths", lambda: [])
    seen: list[str] = []
    monkeypatch.setattr(
        auc_mod.QMessageBox,
        "information",
        staticmethod(lambda *a, **k: seen.append(a[1])),
    )
    view._autofill_from_clipboard()
    assert seen and not view._rows

    image = QImage(200, 100, QImage.Format_RGB32)
    image.fill(0xFFFFFF)
    monkeypatch.setattr(
        auc_mod, "clipboard_image_paths", lambda: [clipboard_images.spool_image(image)]
    )

    view._autofill_from_clipboard()

    assert len(view._rows) == 1
    assert view._rows[0].name.text() == "Gengar"
    view.deleteLater()
