"""Offscreen tests for the export/import paths, driven from the Operations screen.

These verbs used to live in the File menu; the menu is now just a way into
Operations (see tests/test_ui_operations.py), so the behaviour is exercised
through the screen that owns it.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tcginventory.data.db import connect  # noqa: E402
from tcginventory.data.migrator import migrate  # noqa: E402
from tcginventory.data.repositories import ItemRepository  # noqa: E402
from tcginventory.data.seed import seed_demo  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def seeded_conn():
    conn = connect(":memory:")
    migrate(conn)
    seed_demo(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _silence_messageboxes(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)


def test_file_menu_opens_operations(app, seeded_conn):
    from tcginventory.ui.main_window import MainWindow

    window = MainWindow(seeded_conn)
    labels = {a.text() for a in window._file_menu.actions()}
    assert "Operations" in labels and "Exit" in labels
    window.close()


def test_export_all_writes_every_table(app, seeded_conn, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from tcginventory.ui.main_window import MainWindow

    target = tmp_path / "dump"
    target.mkdir()
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: str(target)
    )
    window = MainWindow(seeded_conn)
    window._operations._export_all()
    assert (target / "item.csv").exists()
    assert (target / "inventory_readable.csv").exists()
    window.close()


def test_import_items_creates_them(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from tcginventory.ui.main_window import MainWindow

    conn = connect(":memory:")
    migrate(conn)

    csv_path = tmp_path / "items.csv"
    csv_path.write_text("name,set,market value\nMachamp,Base,20.00\n", encoding="utf-8")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *a, **k: (str(csv_path), "")
    )

    window = MainWindow(conn)
    window._operations._import_items()
    names = {i.name for i in ItemRepository(conn).list_all()}
    assert "Machamp" in names
    window.close()
    conn.close()
