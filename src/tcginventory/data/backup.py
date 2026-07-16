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


def backup_price_history(
    history_conn: sqlite3.Connection,
    backups_dir,
    stamp: str,
    months: int = 3,
    keep: int = 3,
) -> Path:
    """Back up the price-history DB, trimmed to the last ``months`` and pruned to
    ``keep`` newest copies. Kept separate (and lighter) than the ledger backup:
    it's re-derivable market data, so we protect only recent history."""
    from datetime import date, timedelta

    backups_dir = Path(backups_dir)
    dest = backups_dir / f"pricehistory-{stamp}.db"
    backup_database(history_conn, dest)
    cutoff = (date.today() - timedelta(days=months * 31)).isoformat()
    trimmed = sqlite3.connect(str(dest))
    try:
        trimmed.execute(
            "DELETE FROM catalog_price_snapshot WHERE as_of < ?", (cutoff,)
        )
        trimmed.commit()
        trimmed.execute("VACUUM")
    finally:
        trimmed.close()
    for old in sorted(backups_dir.glob("pricehistory-*.db"))[:-keep]:
        old.unlink()
    return dest
