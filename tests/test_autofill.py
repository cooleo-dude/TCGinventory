"""Inference autofill UI (Part 2): the review dialog + Transaction/Auction wiring."""

from __future__ import annotations

import os

import pytest
from PIL import Image

from tcginventory.data.vision import CardLabel
from tcginventory.services.inference import Detection

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


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
    assert [c for c, _ in vrepo.region_vectors("pixel-24")] == ["cel-4"]
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
