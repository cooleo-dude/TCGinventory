"""Database backup via SQLite's online-backup API (F-DA-5 / F-DA-6).

Uses ``sqlite3.Connection.backup`` so it works on a live connection (WAL and
in-memory alike) and produces a consistent single-file copy.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def backup_database(conn: sqlite3.Connection, dest_path) -> Path:
    """Write a consistent copy of ``conn``'s database to ``dest_path``."""
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def timestamp_for_filename() -> str:
    """A filesystem-safe UTC timestamp, e.g. ``20260710-153000``."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def auto_backup(
    conn: sqlite3.Connection, backups_dir, stamp: str, keep: int = 10
) -> Path:
    """Timestamped startup backup into ``backups_dir``, pruning to ``keep`` newest."""
    backups_dir = Path(backups_dir)
    dest = backups_dir / f"inventory-{stamp}.db"
    backup_database(conn, dest)
    existing = sorted(backups_dir.glob("inventory-*.db"))
    for old in existing[:-keep]:
        old.unlink()
    return dest
