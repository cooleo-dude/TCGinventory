"""Vision corpus: content-addressed ingestion + labels, and ingest-on-commit."""

from __future__ import annotations

import hashlib
import os

import pytest

from tcginventory.data.vision import CardLabel, VisionRepository, open_vision
from tcginventory.services import vision as vision_svc

# ---------------------------------------------------------------------------
# data + service layers (headless)
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo():
    conn = open_vision(":memory:")
    yield VisionRepository(conn)
    conn.close()


def test_ingest_capture_groups_photos_and_cards(repo, tmp_path):
    photos = []
    for i, content in enumerate((b"photo-one", b"photo-two")):
        p = tmp_path / f"p{i}.png"
        p.write_bytes(content)
        photos.append(p)
    store = tmp_path / "store"

    capture_id = vision_svc.ingest_capture(
        repo,
        photos,
        "transaction",
        [
            CardLabel(card_id="cel-4", name="Charizard", condition="NM"),
            CardLabel(card_id="cel-5", name="Blastoise", condition="LP"),
            CardLabel(card_id="cel-6", name="Venusaur"),
        ],
        base_dir=store,
    )
    assert capture_id is not None
    assert repo.capture_count() == 1  # one purchase…
    assert repo.image_count() == 2  # …two photos of it…
    assert repo.capture_card_count() == 3  # …documenting three cards
    assert repo.captures_with_card("cel-4") == 1

    # Files are content-addressed under the store dir.
    for content in (b"photo-one", b"photo-two"):
        sha = hashlib.sha256(content).hexdigest()
        assert (store / "vision" / "images" / f"{sha}.png").exists()


def test_ingest_capture_dedups_identical_files_on_disk(repo, tmp_path):
    a = tmp_path / "a.png"
    a.write_bytes(b"same")
    b = tmp_path / "b.png"
    b.write_bytes(b"same")  # identical content, different filename
    store = tmp_path / "s"

    vision_svc.ingest_capture(
        repo, [a, b], "transaction", [CardLabel(card_id="c1")], base_dir=store
    )
    # Two image rows (two supplied photos) but one file on disk (content hash).
    assert repo.image_count() == 2
    sha = hashlib.sha256(b"same").hexdigest()
    assert list((store / "vision" / "images").glob(f"{sha}*")) != []


def test_ingest_capture_persists_front_back_pair(repo, tmp_path):
    front = tmp_path / "front.png"
    front.write_bytes(b"front-face")
    back = tmp_path / "back.png"
    back.write_bytes(b"back-face")
    solo = tmp_path / "solo.png"
    solo.write_bytes(b"unpaired")

    vision_svc.ingest_capture(
        repo,
        [(front, "front", 1), (back, "back", 1), (solo, None, None)],
        "transaction",
        [CardLabel(card_id="cel-4")],
        base_dir=tmp_path / "s",
    )
    rows = repo.conn.execute(
        "SELECT side, pair_group FROM image ORDER BY id"
    ).fetchall()
    assert [(r["side"], r["pair_group"]) for r in rows] == [
        ("front", 1),
        ("back", 1),
        (None, None),
    ]


def test_ingest_capture_noop_without_photos_or_cards(repo, tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"x")
    assert vision_svc.ingest_capture(repo, [], "transaction", [CardLabel()]) is None
    assert vision_svc.ingest_capture(repo, [p], "transaction", []) is None
    assert repo.capture_count() == 0


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


def test_transaction_submit_ingests_scene_photo(app, _quiet, tmp_path):
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

    # Two received cards + one photo of the pile, then submit (a purchase).
    for name in ("Pikachu", "Charizard"):
        row = view._add_receive_row()
        row.set_card(
            IncomingCard(
                item=Item(name=name, category=Category.SINGLE),
                quantity=1,
                market_value_cents=1000,
                intent=Intent.RESALE,
            )
        )
    # Several photos of the one purchase.
    view._photos.set_paths([_png(tmp_path / "front.png"), _png(tmp_path / "back.png")])
    view.submit()

    assert vrepo.capture_count() == 1  # one purchase…
    assert vrepo.image_count() == 2  # …with both photos…
    assert vrepo.capture_card_count() == 2  # …documenting both received cards
    view.deleteLater()


def test_auction_save_ingests_scene_photo(app, _quiet, tmp_path):
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
    for name in ("Charizard", "Blastoise"):
        row = view._add_card_row()
        row.name.setText(name)
        row.market.setValue(20.0)
    view._photos.set_paths([_png(tmp_path / "a.png"), _png(tmp_path / "b.png")])
    view._save()

    assert vrepo.capture_count() == 1  # one auction…
    assert vrepo.image_count() == 2  # …with both photos…
    assert vrepo.capture_card_count() == 2  # …documenting both cards
    # Cleared after ingest so a second save doesn't duplicate the capture.
    view._save()
    assert vrepo.capture_count() == 1
    view.deleteLater()


def test_photo_dialog_links_and_unlinks_front_back(app):
    from tcginventory.ui.image_capture import PhotoEntry, _ImageListDialog

    dialog = _ImageListDialog([PhotoEntry("a.png"), PhotoEntry("b.png")])
    dialog._list.selectAll()
    dialog._link()  # topmost = front, next = back, shared pair
    assert [(p.side, p.pair) for p in dialog.photos()] == [
        ("front", 1),
        ("back", 1),
    ]

    dialog._list.selectAll()
    dialog._unlink()
    assert all(p.side is None and p.pair is None for p in dialog.photos())
    dialog.deleteLater()
