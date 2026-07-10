"""Tests for database backup (F-DA-5 / F-DA-6), headless."""

from __future__ import annotations

import sqlite3

from tcginventory.data.backup import auto_backup, backup_database
from tcginventory.data.repositories import ItemRepository
from tcginventory.data.seed import seed_demo


def test_backup_database_produces_a_readable_copy(tmp_path, conn):
    seed_demo(conn)
    expected = len(ItemRepository(conn).list_all())

    dest = backup_database(conn, tmp_path / "backup.db")
    assert dest.exists()

    copy = sqlite3.connect(str(dest))
    count = copy.execute("SELECT COUNT(*) FROM item").fetchone()[0]
    copy.close()
    assert count == expected


def test_auto_backup_prunes_to_keep_newest(tmp_path, conn):
    seed_demo(conn)
    backups = tmp_path / "backups"
    for stamp in ["20260101-000000", "20260102-000000", "20260103-000000"]:
        auto_backup(conn, backups, stamp=stamp, keep=2)

    remaining = sorted(p.name for p in backups.glob("inventory-*.db"))
    assert remaining == [
        "inventory-20260102-000000.db",
        "inventory-20260103-000000.db",
    ]
