"""SQLite connection helpers.

Owns connection setup (WAL, foreign-key enforcement, row factory) and a small
time helper. No business logic and no Qt — importable and testable headless.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a SQLite connection with the project's standard pragmas.

    - ``row_factory`` is :class:`sqlite3.Row` so repositories can read by column
      name.
    - **WAL** journal mode for durability/concurrency on the local file (a no-op
      for ``:memory:`` databases, which report ``memory``).
    - **Foreign keys ON** — enforced per-connection in SQLite, so every
      connection must set it.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def open_database(db_path: str | Path) -> sqlite3.Connection:
    """Connect and bring the schema up to date, returning the live connection."""
    from tcginventory.data.migrator import migrate

    conn = connect(db_path)
    migrate(conn)
    return conn
