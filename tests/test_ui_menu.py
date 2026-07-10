"""Offscreen tests for the File menu: export and import through the UI."""

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
def _silence_messageboxes(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)


def test_file_menu_has_actions(app, seeded_conn):
    from tcginventory.ui.main_window import MainWindow

    window = MainWindow(seeded_conn)
    labels = {a.text() for a in window._file_menu.actions()}
    assert any("Backup database" in label for label in labels)
    assert any("Import items" in label for label in labels)
    window.close()


def test_export_all_via_menu(app, seeded_conn, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from tcginventory.ui.main_window import MainWindow

    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path)
    )
    window = MainWindow(seeded_conn)
    window._export_all()
    assert (tmp_path / "item.csv").exists()
    assert (tmp_path / "inventory_readable.csv").exists()
    window.close()


def test_import_items_via_menu(app, tmp_path, monkeypatch):
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
    window._import_items()
    names = {i.name for i in ItemRepository(conn).list_all()}
    assert "Machamp" in names
    window.close()
    conn.close()
