"""Vision corpus: capture ingestion, region storage, and the Recognition window."""

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
        [CardLabel(card_id="cel-4"), CardLabel(card_id="cel-5")],
        base_dir=store,
    )
    assert capture_id is not None
    assert repo.capture_count() == 1
    assert repo.image_count() == 2
    assert repo.capture_card_count() == 2
    for content in (b"photo-one", b"photo-two"):
        sha = hashlib.sha256(content).hexdigest()
        assert (store / "vision" / "images" / f"{sha}.png").exists()


def test_ingest_capture_noop_without_photos_or_cards(repo, tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"x")
    assert vision_svc.ingest_capture(repo, [], "transaction", [CardLabel()]) is None
    assert vision_svc.ingest_capture(repo, [p], "transaction", []) is None
    assert repo.capture_count() == 0


def test_add_region_stores_box_and_label(repo):
    capture_id = repo.add_capture(source="transaction")
    image_id = repo.add_image(capture_id, "sha", "/x.png", width=200, height=100)
    repo.add_region(
        image_id,
        1,
        (0.1, 0.2, 0.3, 0.4),
        "give",
        CardLabel(card_id="cel-4", name="Charizard"),
    )
    assert repo.region_count() == 1
    row = repo.regions_for_image(image_id)[0]
    assert (row["number"], row["side"], row["card_id"]) == (1, "give", "cel-4")
    assert (row["x"], row["y"], row["w"], row["h"]) == (0.1, 0.2, 0.3, 0.4)


def test_labeled_card_ids_are_distinct_and_skip_unlabeled(repo):
    capture_id = repo.add_capture()
    image_id = repo.add_image(capture_id, "sha", "/x.png", width=200, height=100)
    repo.add_region(image_id, 1, (0, 0, 0.5, 0.5), None, CardLabel(card_id="cel-4"))
    repo.add_region(image_id, 2, (0.5, 0, 0.5, 0.5), None, CardLabel(card_id="cel-4"))
    repo.add_region(image_id, 3, (0, 0.5, 0.5, 0.5), None, CardLabel(card_id="cel-5"))
    repo.add_region(image_id, 4, (0.5, 0.5, 0.5, 0.5), None, CardLabel(name="mystery"))
    assert repo.labeled_card_ids() == ["cel-4", "cel-5"]


# ---------------------------------------------------------------------------
# Recognition window (offscreen)
# ---------------------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def _quiet(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)


def _png(path, w=200, h=100):
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor("white"))
    img.save(str(path))
    return str(path)


def test_recognition_window_saves_boxes_as_regions(app, _quiet, tmp_path):
    from PySide6.QtCore import QRectF

    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views.recognition import RecognitionView

    conn = connect(":memory:")
    migrate(conn)
    vrepo = VisionRepository(open_vision(":memory:"))
    view = RecognitionView(conn, vision_repo=vrepo, vision_dir=tmp_path)
    view._mode.setCurrentIndex(0)  # Transaction (I Give / They Give)

    view.add_image_paths([_png(tmp_path / "scene.png", 200, 100)])
    # A card on the left (→ give) and one on the right (→ receive).
    left = view._add_box(QRectF(10, 10, 40, 60))
    left.card = CardLabel(card_id="cel-4", name="Charizard")
    right = view._add_box(QRectF(150, 10, 40, 60))
    right.card = CardLabel(card_id="cel-5", name="Blastoise")
    view._save()

    assert vrepo.capture_count() == 1
    assert vrepo.image_count() == 1
    assert vrepo.region_count() == 2
    image_id = vrepo.conn.execute("SELECT id FROM image").fetchone()["id"]
    rows = {r["card_id"]: r for r in vrepo.regions_for_image(image_id)}
    assert rows["cel-4"]["side"] == "give"  # left of the mid divider
    assert rows["cel-5"]["side"] == "receive"  # right of it
    # Geometry is normalised to 0..1 of the image.
    assert abs(rows["cel-4"]["x"] - 10 / 200) < 1e-6
    view.deleteLater()


def test_recognition_listing_mode_has_no_side(app, _quiet, tmp_path):
    from PySide6.QtCore import QRectF

    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views.recognition import RecognitionView

    conn = connect(":memory:")
    migrate(conn)
    vrepo = VisionRepository(open_vision(":memory:"))
    view = RecognitionView(conn, vision_repo=vrepo, vision_dir=tmp_path)
    view._mode.setCurrentIndex(1)  # Listing (one lot)

    view.add_image_paths([_png(tmp_path / "lot.png")])
    box = view._add_box(QRectF(10, 10, 40, 60))
    box.card = CardLabel(name="Pikachu")
    view._save()

    row = vrepo.conn.execute("SELECT side FROM region").fetchone()
    assert row["side"] is None
    view.deleteLater()


def _catalog_conn():
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.data.repositories import CardCatalogRepository
    from tcginventory.domain.models import CatalogCard

    conn = connect(":memory:")
    migrate(conn)
    CardCatalogRepository(conn).replace_all(
        [
            CatalogCard(
                card_id="cel-4", name="Charizard", set_id="cel",
                set_name="Celebrations", number="4", printed_total=25,
            ),
            CatalogCard(
                card_id="cel-5", name="Blastoise", set_id="cel",
                set_name="Celebrations", number="5", printed_total=25,
            ),
        ],
        "2026-01-01",
    )
    return conn


class _FakeRecognizer:
    """Returns canned rankings, restricted to the given candidate pool."""

    def __init__(self, ranked):
        self._ranked = ranked  # list[(card_id, score)]

    def suggest(self, image, candidates, top=5):
        from tcginventory.services.recognizer import Suggestion

        return [Suggestion(c, s) for c, s in self._ranked if c in candidates][:top]


def test_suggestions_populate_and_picking_labels_the_box(app, _quiet, tmp_path):
    from PySide6.QtCore import QRectF

    from tcginventory.ui.views.recognition import RecognitionView

    conn = _catalog_conn()
    vrepo = VisionRepository(open_vision(":memory:"))
    # Seed the corpus so the candidate pool is non-empty.
    cap = vrepo.add_capture()
    img = vrepo.add_image(cap, "sha", "/x.png", width=10, height=10)
    vrepo.add_region(img, 1, (0, 0, 0.5, 1), None, CardLabel(card_id="cel-4"))
    vrepo.add_region(img, 2, (0.5, 0, 0.5, 1), None, CardLabel(card_id="cel-5"))

    view = RecognitionView(
        conn,
        vision_repo=vrepo,
        vision_dir=tmp_path,
        recognizer=_FakeRecognizer([("cel-5", 0.92), ("cel-4", 0.30)]),
    )
    view._mode.setCurrentIndex(1)  # listing
    view.add_image_paths([_png(tmp_path / "scene.png", 200, 100)])
    box = view._add_box(QRectF(10, 10, 40, 60))  # unlabeled

    view._run_suggestions()

    combo = view._suggest_boxes[box.number]
    assert combo.isEnabled()
    assert combo.count() == 3  # "— pick —" + two ranked guesses
    assert "Blastoise" in combo.itemText(1) and "92%" in combo.itemText(1)
    assert combo.itemText(2).startswith("⚠")  # low-confidence second guess

    combo._on_activated(1)  # simulate picking the top suggestion
    assert box.card.card_id == "cel-5"
    assert view._name_fields[box.number].text() == "Blastoise"
    view.deleteLater()


def test_suggestions_notify_when_no_labeled_corpus(app, monkeypatch, tmp_path):
    from PySide6.QtCore import QRectF
    from PySide6.QtWidgets import QMessageBox

    from tcginventory.ui.views.recognition import RecognitionView

    calls: list = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda *a, **k: calls.append(a) or QMessageBox.Ok
    )

    conn = _catalog_conn()
    vrepo = VisionRepository(open_vision(":memory:"))  # empty corpus
    view = RecognitionView(
        conn,
        vision_repo=vrepo,
        vision_dir=tmp_path,
        recognizer=_FakeRecognizer([("cel-5", 0.9)]),
    )
    view._mode.setCurrentIndex(1)
    view.add_image_paths([_png(tmp_path / "scene.png", 200, 100)])
    box = view._add_box(QRectF(10, 10, 40, 60))  # unlabeled → pool stays empty

    view._run_suggestions()

    assert calls  # the user was told the corpus is empty
    assert not view._suggest_boxes[box.number].isEnabled()  # nothing suggested
    view.deleteLater()
