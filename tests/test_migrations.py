"""Tests for the migration runner and initial schema — pure, no Qt."""

from __future__ import annotations

from tcginventory.data.db import connect
from tcginventory.data.migrator import (
    available_migrations,
    current_version,
    migrate,
)

_EXPECTED_TABLES = {
    "item",
    "lot",
    "transaction",
    "transaction_line",
    "lot_consumption",
    "audit_log",
    "schema_version",
}


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r["name"] for r in rows}


def test_available_migrations_include_init():
    migrations = available_migrations()
    assert migrations, "expected at least one bundled migration"
    assert migrations[0].version == 1
    assert migrations[0].name == "0001_init.sql"
    # sorted ascending by version
    versions = [m.version for m in migrations]
    assert versions == sorted(versions)


def test_fresh_db_starts_at_version_zero():
    conn = connect(":memory:")
    assert current_version(conn) == 0


def test_migrate_applies_all_and_creates_tables():
    conn = connect(":memory:")
    applied = migrate(conn)
    latest = available_migrations()[-1].version
    assert applied == [m.version for m in available_migrations()]
    assert current_version(conn) == latest
    assert _EXPECTED_TABLES <= _table_names(conn)


def test_language_column_added_by_0002():
    conn = connect(":memory:")
    migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(item)")}
    assert "language" in cols


def test_migrate_is_idempotent():
    conn = connect(":memory:")
    first = migrate(conn)
    assert first  # applied at least the initial migration
    latest = available_migrations()[-1].version
    # Second run applies nothing and leaves the version untouched.
    assert migrate(conn) == []
    assert current_version(conn) == latest
    rows = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()
    assert rows["n"] == len(available_migrations())


def test_foreign_keys_are_enforced():
    conn = connect(":memory:")
    migrate(conn)
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1


def test_lot_consumption_lot_index_added_by_0018():
    conn = connect(":memory:")
    migrate(conn)
    indexes = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'lot_consumption'"
        )
    }
    assert "idx_consumption_lot" in indexes
