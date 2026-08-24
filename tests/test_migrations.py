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


def _migration_sql(version: int) -> str:
    (found,) = [m for m in available_migrations() if m.version == version]
    return found.sql


def _add_item(conn, name, set_name, card_number, language="EN"):
    cur = conn.execute(
        "INSERT INTO item (name, game, set_name, card_number, category, "
        "language, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (name, "pokemon", set_name, card_number, "single", language, "t", "t"),
    )
    return cur.lastrowid


def _add_catalog(conn, card_id, set_id, set_name, number, language="EN"):
    conn.execute(
        "INSERT INTO card_catalog (card_id, name, set_id, set_name, number, "
        "language, updated_at) VALUES (?,?,?,?,?,?,?)",
        (card_id, "x", set_id, set_name, number, language, "t"),
    )


def test_0021_pads_only_the_sets_that_print_padded_numbers():
    """Padding is per-set, so the migration must not apply a blanket rule.

    151 prints "006/165" and the catalog stored "6"; Base Set prints "4/102" and
    the catalog stored "4". A migration that padded everything to three digits
    would corrupt the second while fixing the first (ticket 49).
    """
    conn = connect(":memory:")
    migrate(conn)

    # The catalog as it stands *before* an English re-pull: number stripped,
    # card_id still carrying TCGdex's localId.
    _add_catalog(conn, "sv03.5-006", "sv03.5", "151", "6")
    _add_catalog(conn, "sv03.5-001", "sv03.5", "151", "1")
    _add_catalog(conn, "sv03.5-183", "sv03.5", "151", "183")
    _add_catalog(conn, "base1-4", "base1", "Base Set", "4")

    padded = _add_item(conn, "Charizard ex", "151", "6/165")
    bare = _add_item(conn, "Bulbasaur", "151", "1")
    already = _add_item(conn, "Mewtwo", "151", "183/165")
    vintage = _add_item(conn, "Charizard", "Base Set", "4/102")
    unknown = _add_item(conn, "Pikachu", "Some Promo Box", "7/12")
    japanese = _add_item(conn, "Charizard ex", "151", "6/165", language="JP")

    conn.executescript(_migration_sql(21))

    def number(item_id):
        return conn.execute(
            "SELECT card_number FROM item WHERE id = ?", (item_id,)
        ).fetchone()["card_number"]

    assert number(padded) == "006/165"  # the reported bug
    assert number(bare) == "001"  # no denominator to preserve
    assert number(already) == "183/165"  # already three digits, untouched
    assert number(vintage) == "4/102"  # Base Set really does print "4/102"
    assert number(unknown) == "7/12"  # set not in the catalog — left alone
    assert number(japanese) == "6/165"  # no EN row may match a JP item


def test_0021_leaves_the_ledger_alone():
    """The migration is metadata-only: it must not touch lots or transactions.

    card_number is an identity column, so re-keying it is safe only because
    nothing points at the *string* — lots point at item_id. A migration that
    reached further would be moving cost basis around inside a .sql file.
    """
    statements = "\n".join(
        line.split("--", 1)[0]
        for line in _migration_sql(21).splitlines()
        if line.split("--", 1)[0].strip()
    ).upper()
    for forbidden in ("DELETE", "INSERT", "DROP", "LOT", "TRANSACTION", "AUDIT"):
        assert forbidden not in statements, forbidden
    assert statements.count("UPDATE") == 1
    assert "UPDATE ITEM" in statements


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
