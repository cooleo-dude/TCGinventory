"""Tests for config path resolution — pure, no Qt required."""

from __future__ import annotations

from pathlib import Path

from tcginventory import config


def test_db_path_uses_data_dir_by_default(monkeypatch):
    monkeypatch.delenv("TCGINVENTORY_DB", raising=False)
    monkeypatch.delenv("TCGINVENTORY_DATA_DIR", raising=False)
    assert config.db_path() == config.data_dir() / config.DB_FILENAME


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TCGINVENTORY_DB", raising=False)
    assert config.data_dir() == tmp_path
    assert config.db_path() == tmp_path / config.DB_FILENAME


def test_db_env_override_wins(monkeypatch, tmp_path):
    db = tmp_path / "custom.db"
    monkeypatch.setenv("TCGINVENTORY_DB", str(db))
    assert config.db_path() == db


def test_ensure_data_dir_creates_it(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "appdata"
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(target))
    created = config.ensure_data_dir()
    assert created == target
    assert Path(target).is_dir()
