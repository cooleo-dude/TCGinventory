"""Vision corpus: region storage, exemplars, and the Recognition window."""

from __future__ import annotations

import os

import pytest

from tcginventory.data.vision import CardLabel, VisionRepository, open_vision

# ---------------------------------------------------------------------------
# data + service layers (headless)
# ---------------------------------------------------------------------------


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


def test_corpus_summary_rows_counts_and_growth(repo):
    # Session 1 (Jan 1): one image, two boxes, one of them labeled.
    c1 = repo.add_capture(source="transaction", now="2026-01-01T09:00:00+00:00")
    i1 = repo.add_image(
        c1, "s1", "/a.png", width=200, height=100, now="2026-01-01T09:00:00+00:00"
    )
    repo.add_region(i1, 1, (0, 0, 0.5, 1), None, CardLabel("cel-4", name="Zard"))
    repo.add_region(i1, 2, (0.5, 0, 0.5, 1), None, CardLabel())  # drawn, unlabeled
    # Session 2 (Jan 3): two images; a box labeled by name only (card_id NULL) —
    # how the recognition tool actually stores most identities.
    c2 = repo.add_capture(
        source="listing", note="binder", now="2026-01-03T10:00:00+00:00"
    )
    i2 = repo.add_image(
        c2, "s2", "/b.png", width=200, height=100, now="2026-01-03T10:00:00+00:00"
    )
    repo.add_image(
        c2, "s3", "/c.png", width=200, height=100, now="2026-01-03T10:05:00+00:00"
    )
    repo.add_region(i2, 1, (0, 0, 1, 1), None, CardLabel(name="Pikachu 25/100"))

    rows = repo.capture_rows()
    assert [r["id"] for r in rows] == [c1, c2]  # oldest first
    assert (
        rows[0]["source"],
        rows[0]["images"],
        rows[0]["regions"],
        rows[0]["labeled"],
    ) == ("transaction", 1, 2, 1)  # 1 of 2 boxes carries an identity
    assert (
        rows[1]["source"],
        rows[1]["note"],
        rows[1]["images"],
        rows[1]["labeled"],
    ) == ("listing", "binder", 2, 1)

    by_cap: dict[int, list[str]] = {}
    for row in repo.capture_cards():
        by_cap.setdefault(row["capture_id"], []).append(row["name"])
    # A name-only label counts; a truly-blank box does not.
    assert by_cap == {c1: ["Zard"], c2: ["Pikachu 25/100"]}

    assert repo.image_count() == 3
    assert repo.labeled_region_count() == 2
    assert repo.labeled_card_count() == 2  # distinct: cel-4, "Pikachu 25/100"
    # Cumulative images per day — the growth curve.
    assert repo.image_growth_series() == [("2026-01-01", 1), ("2026-01-03", 3)]
    assert len(repo.capture_regions(c1)) == 2  # both boxes, with image path


def test_export_yolo_dataset_writes_images_labels_and_yaml(repo, tmp_path):
    from PIL import Image

    from tcginventory.services.detector_dataset import export_yolo_dataset

    for i in range(2):
        p = tmp_path / f"img{i}.png"
        Image.new("RGB", (100, 60), "white").save(p)
        cap = repo.add_capture()
        image_id = repo.add_image(cap, f"sha{i}", str(p), width=100, height=60)
        repo.add_region(image_id, 1, (0.1, 0.1, 0.2, 0.3), None, CardLabel(card_id="a"))
    # An image with no regions is excluded from the training set.
    cap = repo.add_capture()
    repo.add_image(cap, "shaX", str(tmp_path / "img0.png"), width=100, height=60)

    out = tmp_path / "yolo"
    summary = export_yolo_dataset(repo, out, val_frac=0.5, seed=0)

    assert summary.images == 2
    assert summary.train == 1 and summary.val == 1
    assert summary.boxes == 2
    assert (out / "data.yaml").exists()
    labels = list((out / "labels").rglob("*.txt"))
    images = list((out / "images").rglob("*.png"))
    assert len(labels) == 2 and len(images) == 2
    parts = labels[0].read_text().strip().split()
    assert parts[0] == "0" and len(parts) == 5  # class + centre-form box
    assert abs(float(parts[1]) - 0.2) < 1e-6  # cx = 0.1 + 0.2/2


# ---------------------------------------------------------------------------
# Recognition window (offscreen)
# ---------------------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def _quiet(monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    # The "annotations saved" confirmation is a themed QDialog, not a QMessageBox;
    # keep it from blocking the headless test on exec().
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.Accepted)


def _png(path, w=200, h=100):
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor("white"))
    img.save(str(path))
    return str(path)


def _pixel_recognizer(vrepo):
    """A fast, dependency-free matcher so UI tests don't load the ONNX model."""
    from tcginventory.services.recognizer import PixelEmbedder, Recognizer

    return Recognizer(vrepo, embedder=PixelEmbedder(32))


def test_recognition_window_saves_boxes_as_regions(app, _quiet, tmp_path):
    from PySide6.QtCore import QRectF

    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views.recognition import RecognitionView

    conn = connect(":memory:")
    migrate(conn)
    vrepo = VisionRepository(open_vision(":memory:"))
    view = RecognitionView(
        conn,
        vision_repo=vrepo,
        vision_dir=tmp_path,
        recognizer=_pixel_recognizer(vrepo),
    )
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
    view = RecognitionView(
        conn,
        vision_repo=vrepo,
        vision_dir=tmp_path,
        recognizer=_pixel_recognizer(vrepo),
    )
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
                card_id="cel-4",
                name="Charizard",
                set_id="cel",
                set_name="Celebrations",
                number="4",
                printed_total=25,
            ),
            CatalogCard(
                card_id="cel-5",
                name="Blastoise",
                set_id="cel",
                set_name="Celebrations",
                number="5",
                printed_total=25,
            ),
        ],
        "2026-01-01",
    )
    return conn


class _FakeRecognizer:
    """Returns canned rankings, restricted to the given candidate pool."""

    # The view reads this to pick the right slice of the catalog index.
    model_version = "test:v1"

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


def test_save_indexes_labeled_crops_as_exemplars(app, _quiet, tmp_path):
    import numpy as np
    from PIL import Image
    from PySide6.QtCore import QRectF

    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views.recognition import RecognitionView

    scene = tmp_path / "scene.png"
    arr = np.random.default_rng(0).integers(0, 256, (100, 200, 3), dtype=np.uint8)
    Image.fromarray(arr, "RGB").save(scene)

    conn = connect(":memory:")
    migrate(conn)
    vrepo = VisionRepository(open_vision(":memory:"))
    view = RecognitionView(
        conn,
        vision_repo=vrepo,
        vision_dir=tmp_path,
        recognizer=_pixel_recognizer(vrepo),
    )
    view._mode.setCurrentIndex(1)  # listing
    view.add_image_paths([str(scene)])
    left = view._add_box(QRectF(5, 5, 90, 90))
    left.card = CardLabel(card_id="L", name="Left")
    right = view._add_box(QRectF(105, 5, 90, 90))
    right.card = CardLabel(card_id="R", name="Right")
    view._add_box(QRectF(150, 50, 20, 20))  # unlabeled → not indexed
    view._save()

    assert vrepo.region_count() == 3
    assert vrepo.region_embedding_count() == 2  # only the two labeled crops
    cards = sorted(card_id for card_id, _ in vrepo.region_vectors("pixel-32"))
    assert cards == ["L", "R"]


def test_condition_set_while_labeling_saves_to_corpus(app, _quiet, tmp_path):
    """The per-card condition picker is available on any card (not just backs),
    and its value is written to the saved region."""
    from PySide6.QtCore import QRectF

    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views.recognition import RecognitionView

    conn = connect(":memory:")
    migrate(conn)
    vrepo = VisionRepository(open_vision(":memory:"))
    view = RecognitionView(
        conn,
        vision_repo=vrepo,
        vision_dir=tmp_path,
        recognizer=_pixel_recognizer(vrepo),
    )
    view._mode.setCurrentIndex(1)  # listing — a plain front image
    view.add_image_paths([_png(tmp_path / "s.png", 200, 100)])
    box = view._add_box(QRectF(10, 10, 40, 60))
    box.card = CardLabel(card_id="cel-4", name="Charizard")
    assert box.condition == "NM"  # defaults to Near Mint
    assert view._condition_boxes[box.number].currentData() == "NM"  # picker shows it

    view._condition_boxes[box.number].set_condition("LP")  # picker on every card
    assert box.condition == "LP"

    view._save()
    assert vrepo.conn.execute("SELECT condition FROM region").fetchone()[0] == "LP"
    view.deleteLater()


def test_name_pick_keeps_identity_through_focus_out(app):
    """Picking a catalog card must keep its id/set/number even when the completer
    leaves the display string in the field and focus-out fires (was clobbering it
    to a name-only free-text label, which broke the auction export)."""
    from PySide6.QtCore import QRectF

    from tcginventory.ui.views.recognition import _Box, _NameField

    conn = _catalog_conn()  # has cel-4 "Charizard" · Celebrations #4
    box = _Box(number=1, rect=QRectF(0, 0, 1, 1))
    field = _NameField(conn, box)
    field._on_edited("Charizard")  # populate completer matches
    field._pick(field._model.index(0, 0))  # pick the top match
    assert box.card.card_id == "cel-4" and box.card.set_name == "Celebrations"

    field.setText("Charizard  ·  Celebrations  #4")  # completer's display string
    field._on_free_text()  # focus-out
    assert box.card.card_id == "cel-4"  # identity survived
    assert box.card.set_name == "Celebrations" and box.card.card_number

    # A genuinely typed name (no pick) still records as free text.
    box2 = _Box(number=2, rect=QRectF(0, 0, 1, 1))
    f2 = _NameField(conn, box2)
    f2._on_edited("Custom Promo")
    f2.setText("Custom Promo")
    f2._on_free_text()
    assert box2.card.card_id is None and box2.card.name == "Custom Promo"


def test_export_counts_a_front_back_pair_once(app, _quiet, tmp_path):
    """A card labeled on the front and copied to the back exports as one card
    (quantity 1), not two."""
    from PySide6.QtCore import QRectF

    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views.recognition import RecognitionView

    conn = connect(":memory:")
    migrate(conn)
    vrepo = VisionRepository(open_vision(":memory:"))
    view = RecognitionView(
        conn,
        vision_repo=vrepo,
        vision_dir=tmp_path,
        recognizer=_pixel_recognizer(vrepo),
    )
    view._mode.setCurrentIndex(1)
    view.add_image_paths([_png(tmp_path / "front.png", 200, 120)])
    box = view._add_box(QRectF(10, 10, 40, 60))
    box.card = CardLabel(card_id="cel-4", name="Charizard")
    view._include_card_backs()
    view.add_image_paths([_png(tmp_path / "back.png", 200, 120)])

    cards = view._collect_export_cards()
    assert len(cards) == 1 and cards[0].quantity == 1
    view.deleteLater()


def test_add_new_image_slot_then_fill(app, _quiet, tmp_path):
    """'Add new image' appends a blank slot; uploading/pasting fills it in place
    rather than creating a second image."""
    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views.recognition import RecognitionView

    conn = connect(":memory:")
    migrate(conn)
    vrepo = VisionRepository(open_vision(":memory:"))
    view = RecognitionView(
        conn,
        vision_repo=vrepo,
        vision_dir=tmp_path,
        recognizer=_pixel_recognizer(vrepo),
    )
    view._mode.setCurrentIndex(1)

    view._add_new_image_slot()
    assert len(view._sessions) == 1
    assert view._sessions[view._current].awaiting  # blank, waiting for a photo

    view.add_image_paths([_png(tmp_path / "s.png", 100, 60)])
    assert len(view._sessions) == 1  # filled the slot, no duplicate
    assert not view._sessions[0].awaiting
    view.deleteLater()


def test_card_backs_pairs_front_and_grades_from_back(app, _quiet, tmp_path):
    """Include card backs: the front is set aside, a back slot opens with the
    boxes copied over, and saving links the pair (front/back + pair_group) with
    the condition graded from the back."""
    from PIL import Image, ImageDraw
    from PySide6.QtCore import QRectF

    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views.recognition import RecognitionView

    # Back-like photo: whitening on the left half (worn), clean on the right.
    back_png = tmp_path / "backs.png"
    img = Image.new("RGB", (200, 120), (30, 40, 90))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 95, 8], fill=(255, 255, 255))
    draw.rectangle([0, 112, 95, 119], fill=(255, 255, 255))
    draw.rectangle([0, 0, 8, 119], fill=(255, 255, 255))
    img.save(back_png)

    conn = connect(":memory:")
    migrate(conn)
    vrepo = VisionRepository(open_vision(":memory:"))
    view = RecognitionView(
        conn,
        vision_repo=vrepo,
        vision_dir=tmp_path,
        recognizer=_pixel_recognizer(vrepo),
    )
    view._mode.setCurrentIndex(1)  # listing

    # Front: two labeled cards (left + right).
    view.add_image_paths([_png(tmp_path / "front.png", 200, 120)])
    left = view._add_box(QRectF(2, 2, 90, 116))
    left.card = CardLabel(card_id="L", name="Left")
    right = view._add_box(QRectF(100, 20, 90, 80))
    right.card = CardLabel(card_id="R", name="Right")

    view._include_card_backs()  # opens a back slot with the boxes copied over
    back = view._sessions[view._current]
    assert back.side == "back" and back.awaiting and len(back.boxes) == 2

    view.add_image_paths([str(back_png)])  # fills the awaiting slot
    back = view._sessions[view._current]
    assert not back.awaiting and back.side == "back"

    view._assess_conditions()  # grade each card from its back crop
    worn = next(b for b in back.boxes if b.rect.x() < 50)
    clean = next(b for b in back.boxes if b.rect.x() >= 50)
    assert worn.condition is not None and worn.condition != "NM"
    assert clean.condition == "NM"

    view._save()
    imgs = vrepo.conn.execute("SELECT side, pair_group FROM image").fetchall()
    assert {r["side"] for r in imgs} == {"front", "back"}
    assert len({r["pair_group"] for r in imgs}) == 1  # one shared pair group
    back_conds = vrepo.conn.execute(
        "SELECT r.condition FROM region r JOIN image i ON i.id = r.image_id "
        "WHERE i.side = 'back'"
    ).fetchall()
    assert any(r["condition"] and r["condition"] != "NM" for r in back_conds)
    ids = vrepo.conn.execute(
        "SELECT DISTINCT card_id FROM region WHERE card_id IS NOT NULL"
    ).fetchall()
    assert sorted(r["card_id"] for r in ids) == ["L", "R"]  # identity on both
    view.deleteLater()


def test_detector_proposals_overlay_then_accept_into_boxes(
    app, _quiet, tmp_path, monkeypatch
):
    """The YOLO toggle overlays the detector's boxes, discards them when switched
    off, and Accept promotes them into numbered boxes. The detector itself is
    stubbed — there's no untrained proposer since classic-CV was removed."""
    from PIL import Image

    from tcginventory.data.db import connect
    from tcginventory.data.migrator import migrate
    from tcginventory.ui.views.recognition import RecognitionView

    scene = tmp_path / "scene.png"
    Image.new("L", (240, 160), color=255).save(scene)

    monkeypatch.setattr(
        RecognitionView,
        "_detect",
        lambda self, source, path: [(0.08, 0.12, 0.25, 0.7), (0.62, 0.12, 0.25, 0.7)],
    )

    conn = connect(":memory:")
    migrate(conn)
    vrepo = VisionRepository(open_vision(":memory:"))
    view = RecognitionView(conn, vision_repo=vrepo, vision_dir=tmp_path)
    view._mode.setCurrentIndex(1)  # listing
    view.add_image_paths([str(scene)])

    view._yolo_toggle.setChecked(True)  # emits toggled → detect + overlay
    assert len(view._proposals["yolo"]) == 2
    assert view._accept_btn.isEnabled()

    view._yolo_toggle.setChecked(False)  # toggling off discards overlays
    assert view._proposals["yolo"] == []
    assert not view._accept_btn.isEnabled()

    view._yolo_toggle.setChecked(True)
    view._accept_proposals()  # promote all proposals to numbered boxes
    assert len(view._sessions[0].boxes) == 2
    assert view._proposals["yolo"] == []
    assert not view._accept_btn.isEnabled()
    view.deleteLater()


def test_suggestions_notify_when_there_is_nothing_to_match_against(
    app, monkeypatch, tmp_path
):
    """Neither an indexed catalog nor a labelled card: say so rather than
    silently returning nothing."""
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
    box = view._add_box(QRectF(10, 10, 40, 60))  # unlabeled, catalog unindexed

    view._run_suggestions()

    assert calls  # the user was told there is nothing to rank against
    assert not view._suggest_boxes[box.number].isEnabled()  # nothing suggested
    view.deleteLater()
