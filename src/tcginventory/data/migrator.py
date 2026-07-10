"""Forward-only SQL migration runner, tracked by the ``schema_version`` table.

Migration files live in ``tcginventory/data/migrations/`` and are named
``NNNN_description.sql`` (e.g. ``0001_init.sql``). Each is applied at most once,
in ascending numeric order, inside a single transaction that also records the
version — so a failed migration leaves the database untouched.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from importlib import resources

from tcginventory.data.db import now_iso

_MIGRATIONS_PACKAGE = "tcginventory.data"
_MIGRATIONS_DIRNAME = "migrations"


@dataclass(frozen=True)
class Migration:
    """A single numbered migration script."""

    version: int
    name: str
    sql: str


def available_migrations() -> list[Migration]:
    """Return all bundled migrations, sorted ascending by version."""
    root = resources.files(_MIGRATIONS_PACKAGE) / _MIGRATIONS_DIRNAME
    migrations: list[Migration] = []
    for entry in root.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        prefix = entry.name.split("_", 1)[0]
        migrations.append(
            Migration(
                version=int(prefix),
                name=entry.name,
                sql=entry.read_text(encoding="utf-8"),
            )
        )
    migrations.sort(key=lambda m: m.version)
    return migrations


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version (0 on a fresh database)."""
    if not _table_exists(conn, "schema_version"):
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row is not None and row["v"] is not None else 0


def apply_migration(conn: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration atomically, recording its version on success.

    The version-recording INSERT runs in the same transaction as the DDL. The
    ``applied_at`` value is generated here (never user input), so embedding it in
    the script is safe.
    """
    script = (
        "BEGIN;\n"
        f"{migration.sql}\n"
        "INSERT INTO schema_version(version, applied_at) VALUES "
        f"({migration.version}, '{now_iso()}');\n"
        "COMMIT;"
    )
    try:
        conn.executescript(script)
    except Exception:
        conn.rollback()
        raise


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every pending migration in order. Returns the versions applied."""
    version = current_version(conn)
    applied: list[int] = []
    for migration in available_migrations():
        if migration.version <= version:
            continue
        apply_migration(conn, migration)
        applied.append(migration.version)
    return applied
