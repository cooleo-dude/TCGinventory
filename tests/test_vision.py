"""Vision corpus: content-addressed ingestion + labels, and ingest-on-commit."""

from __future__ import annotations

import hashlib
import os

import pytest

from tcginventory.data.vision import ImageLabel, VisionRepository, open_vision
from tcginventory.services import vision as vision_svc

# ---------------------------------------------------------------------------
# data + service layers (headless)
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo():
    conn = open_vision(":memory:")
    yield VisionRepository(conn)
    conn.close()


def test_ingest_stores_file_and_label(repo, tmp_path):
    src = tmp_path / "card.png"
    src.write_bytes(b"fake-png-bytes")
    store = tmp_path / "store"

    image_id = vision_svc.ingest(
        repo,
        src,
        "transaction",
        ImageLabel(card_id="cel-4", name="Charizard", condition="NM"),
        base_dir=store,
    )
    assert image_id > 0
    assert repo.image_count() == 1
    assert repo.label_count() == 1
    assert repo.labels_for_card("cel-4") == 1

    # File is content-addressed under the store dir.
    sha = hashlib.sha256(b"fake-png-bytes").hexdigest()
    assert (store / "vision" / "images" / f"{sha}.png").exists()


def test_ingest_dedups_identical_content_but_keeps_labels(repo, tmp_path):
    a = tmp_path / "a.png"
    a.write_bytes(b"same")
    b = tmp_path / "b.png"
    b.write_bytes(b"same")  # identical content, different filename
    store = tmp_path / "s"

    i1 = vision_svc.ingest(
        repo, a, "transaction", ImageLabel(card_id="c1"), base_dir=store
    )
    i2 = vision_svc.ingest(repo, b, "auction", ImageLabel(card_id="c2"), base_dir=store)

    assert i1 == i2  # one image row (deduped by content hash)
    assert repo.image_count() == 1
    assert repo.label_count() == 2  # but both labels recorded


# ---------------------------------------------------------------------------
# ingest-on-commit through the screens (offscreen)
# ---------------------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def _quiet(monkeypatch):
    """Don't block on the success/confirmation dialogs during a commit."""
    from PySide6.QtWidgets import QDialog, QMessageBox

    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.Accepted)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)


def _png(path):
    path.write_bytes(b"png")
    return str(path)


def test_transaction_submit_ingests_photos(app, _quiet, tmp_path):
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.domain.enums import Category, Intent
    from tcginventory.domain.models import Item
    from tcginventory.services.transactions import IncomingCard
    from tcginventory.ui.views.transaction import TransactionView

    conn = connect(":memory:")
    migrate(conn)
    vrepo = VisionRepository(open_vision(":memory:"))
    view = TransactionView(conn, vision_repo=vrepo, vision_dir=tmp_path)

    # A received card with a photo attached, then submit (a purchase).
    row = view._add_receive_row()
    row.set_card(
        IncomingCard(
            item=Item(name="Pikachu", category=Category.SINGLE),
            quantity=1,
            market_value_cents=1000,
            intent=Intent.RESALE,
        )
    )
    row.images.set_paths([_png(tmp_path / "pika.png")])
    view.submit()

    assert vrepo.image_count() == 1
    assert vrepo.label_count() == 1
    view.deleteLater()


def test_auction_save_ingests_photos(app, _quiet, tmp_path):
    from tcginventory.data.auctions import open_auctions
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views.auctions import AuctionsView

    conn = connect(":memory:")
    migrate(conn)
    vrepo = VisionRepository(open_vision(":memory:"))
    view = AuctionsView(
        conn,
        auctions_conn=open_auctions(":memory:"),
        history=None,
        vision_repo=vrepo,
        vision_dir=tmp_path,
    )
    row = view._add_card_row()
    row.name.setText("Charizard")
    row.market.setValue(20.0)
    row.images.set_paths([_png(tmp_path / "zard.png")])
    view._save()

    assert vrepo.image_count() == 1
    assert vrepo.label_count() == 1
    # Cleared after ingest so a second save doesn't duplicate the label.
    view._save()
    assert vrepo.label_count() == 1
    view.deleteLater()
