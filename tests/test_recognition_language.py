"""Each card in a scan chooses which catalog it is matched against.

English and Japanese releases share artwork exactly, so the picture alone cannot
separate them — measured on the corpus, 42 of 46 wrong suggestions were a card
losing to its counterpart from the other region. The scope is therefore stated
rather than inferred, and it lives on the box because one photo routinely holds
both.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF  # noqa: E402

from tcginventory.data.db import connect  # noqa: E402
from tcginventory.data.migrator import migrate  # noqa: E402
from tcginventory.data.repositories import CardCatalogRepository  # noqa: E402
from tcginventory.data.vision import VisionRepository, open_vision  # noqa: E402
from tcginventory.domain.models import CatalogCard  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)


def _view(tmp_path):
    from PIL import Image

    from tcginventory.ui.views.recognition import RecognitionView

    conn = connect(":memory:")
    migrate(conn)
    repo = VisionRepository(open_vision(":memory:"))
    _bilingual_catalog(conn, repo)
    scene = tmp_path / "front.png"
    Image.new("RGB", (240, 160), color=(190, 190, 190)).save(scene)
    view = RecognitionView(conn, vision_repo=repo, vision_dir=tmp_path)
    view._mode.setCurrentIndex(1)  # listing mode: no give/receive divider
    view.add_image_paths([str(scene)])
    return view, conn


def _bilingual_catalog(conn, repo, version="test:v1"):
    """Two English cards and one Japanese one, all indexed."""
    repository = CardCatalogRepository(conn)
    repository.replace_for_language(
        [
            CatalogCard(card_id="en-1", name="Charizard", set_id="s", number="4"),
            CatalogCard(card_id="en-2", name="Blastoise", set_id="s", number="2"),
        ],
        "EN",
        "2026-08-05T00:00:00+00:00",
    )
    repository.replace_for_language(
        [
            CatalogCard(
                card_id="jp-1",
                name="Charizard",
                set_id="s",
                number="004/102",
                language="JP",
            )
        ],
        "JP",
        "2026-08-05T00:00:00+00:00",
    )
    for i, cid in enumerate(("en-1", "en-2", "jp-1")):
        vec = np.full(4, i + 1, dtype=np.float32)
        repo.put_embedding(cid, version, vec.size, vec.tobytes())


def test_a_box_ranks_against_its_own_language(app, tmp_path):
    view, conn = _view(tmp_path)
    box = view._add_box(QRectF(10, 10, 60, 90))

    assert view._candidate_pool("EN", "test:v1") == ["en-1", "en-2"]
    assert view._candidate_pool("JP", "test:v1") == ["jp-1"]
    assert view._candidate_pool(None, "test:v1") == ["en-1", "en-2", "jp-1"]
    assert box.language == "EN"  # the common case, and what the default serves
    view.deleteLater()
    conn.close()


def test_two_cards_in_one_photo_can_use_different_catalogs(app, tmp_path):
    """The reason this is per box: a scan is not all one language."""
    view, conn = _view(tmp_path)
    first = view._add_box(QRectF(10, 10, 60, 90))
    second = view._add_box(QRectF(90, 10, 60, 90))

    combo = view._lang_boxes[second.number]
    combo.setCurrentIndex(combo.findData("JP"))

    assert first.language == "EN" and second.language == "JP"
    assert view._candidate_pool(first.language, "test:v1") == ["en-1", "en-2"]
    assert view._candidate_pool(second.language, "test:v1") == ["jp-1"]
    view.deleteLater()
    conn.close()


def test_a_new_box_inherits_the_last_choice(app, tmp_path):
    """A single-language scan should cost one change, not one per card."""
    view, conn = _view(tmp_path)
    first = view._add_box(QRectF(10, 10, 60, 90))
    combo = view._lang_boxes[first.number]
    combo.setCurrentIndex(combo.findData("JP"))

    second = view._add_box(QRectF(90, 10, 60, 90))
    assert second.language == "JP"
    assert view._lang_boxes[second.number].currentData() == "JP"
    view.deleteLater()
    conn.close()


def test_the_two_faces_of_a_card_share_a_language(app, tmp_path):
    """One physical card, one language — the rule the condition picker follows."""
    view, conn = _view(tmp_path)
    box = view._add_box(QRectF(10, 10, 60, 90))
    front = view._sessions[view._current]
    view._include_card_backs()
    back = next(
        s for s in view._sessions if s is not front and s.pair_id == front.pair_id
    )

    combo = view._lang_boxes[box.number]
    combo.setCurrentIndex(combo.findData("JP"))
    assert back.boxes[0].language == "JP"

    # ...and editing the back face carries back to the front.
    view._strip.setCurrentRow(view._sessions.index(back))
    back_combo = view._lang_boxes[box.number]
    assert back_combo.currentData() == "JP"  # the picker shows it too
    back_combo.setCurrentIndex(back_combo.findData("EN"))
    assert front.boxes[0].language == "EN"
    view.deleteLater()
    conn.close()


def test_labelled_cards_stay_in_the_pool_whatever_the_scope(app, tmp_path):
    """A card already identified is known-relevant; a scope must not hide it."""
    view, conn = _view(tmp_path)
    box = view._add_box(QRectF(10, 10, 60, 90))
    from tcginventory.data.vision import CardLabel

    box.card = CardLabel(card_id="jp-1", name="Charizard")

    assert view._candidate_pool("EN", "test:v1") == ["en-1", "en-2", "jp-1"]
    view.deleteLater()
    conn.close()


def test_suggestions_run_against_the_catalog_not_just_labelled_cards(app, tmp_path):
    """The bug this fixes: the matcher could only ever propose a card that had
    already been identified once before."""
    view, conn = _view(tmp_path)
    view._add_box(QRectF(10, 10, 60, 90))

    pool = view._candidate_pool("EN", "test:v1")
    assert pool == ["en-1", "en-2"]  # nothing labelled yet, catalog still offered
    view.deleteLater()
    conn.close()
