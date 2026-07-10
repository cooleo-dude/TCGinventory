"""Tests for CSV import/export and full dump/restore round-trip (headless)."""

from __future__ import annotations

from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.data.repositories import ItemRepository, TransactionRepository
from tcginventory.data.seed import seed_demo
from tcginventory.services.importexport import (
    export_all,
    export_inventory_csv,
    export_items_csv,
    export_transactions_csv,
    import_all,
    import_items,
    import_purchases,
    preview_import,
)
from tcginventory.services.inventory import InventoryService
from tcginventory.services.reporting import ReportingService


def _fresh():
    c = connect(":memory:")
    migrate(c)
    return c


def _snapshot(conn):
    inv = sorted(
        (
            r.item.name,
            r.on_hand,
            r.cost_basis_cents,
            r.market_value_cents,
            r.intent,
            r.sold_status,
        )
        for r in InventoryService(conn).inventory_rows()
    )
    rpt = ReportingService(conn)
    return {
        "items": len(ItemRepository(conn).list_all()),
        "txns": len(TransactionRepository(conn).list_all()),
        "realized": rpt.realized_pl_cents(),
        "net_cash": rpt.net_cash_cents(),
        "inventory": inv,
    }


def test_full_dump_restore_round_trip(tmp_path, conn):
    seed_demo(conn)
    before = _snapshot(conn)
    export_all(conn, tmp_path)

    restored = _fresh()
    import_all(restored, tmp_path)
    after = _snapshot(restored)
    restored.close()

    assert after == before


def test_human_exports_write_headers_and_rows(tmp_path, conn):
    seed_demo(conn)
    items = export_items_csv(conn, tmp_path / "items.csv")
    inv = export_inventory_csv(conn, tmp_path / "inv.csv")
    txns = export_transactions_csv(conn, tmp_path / "txns.csv")

    for path, first_header in [(items, "name"), (inv, "name"), (txns, "id")]:
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith(first_header)
        assert len(lines) > 1  # header + at least one row


def test_item_import_creates_then_matches(tmp_path, conn):
    csv_path = tmp_path / "items.csv"
    csv_path.write_text(
        "name,set,number,market value,condition,intent\n"
        "Blastoise,Base,009/102,50.00,NM,resale\n"
        "Venusaur,Base,015/102,45.00,LP,personal\n",
        encoding="utf-8",
    )
    preview = preview_import(csv_path, mode="items")
    assert preview.ok_count == 2
    assert preview.error_count == 0

    result = import_items(conn, csv_path)
    assert result.created == 2
    names = {i.name for i in ItemRepository(conn).list_all()}
    assert {"Blastoise", "Venusaur"} <= names

    # Re-import → identity match, no duplicates.
    again = import_items(conn, csv_path)
    assert again.created == 0
    assert again.matched == 2


def test_item_import_reports_row_errors(tmp_path, conn):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text(
        "name,market value\n"
        ",10.00\n"  # missing name
        "Charizard,notmoney\n",  # bad money
        encoding="utf-8",
    )
    preview = preview_import(csv_path, mode="items")
    assert preview.ok_count == 0
    assert preview.error_count == 2


def test_purchase_import_posts_transactions(tmp_path, conn):
    csv_path = tmp_path / "buys.csv"
    csv_path.write_text(
        "name,set,price,market value,date,intent\n"
        "Mewtwo,Base,30.00,40.00,2026-04-01,resale\n",
        encoding="utf-8",
    )
    result = import_purchases(conn, csv_path)
    assert result.posted == 1

    rows = InventoryService(conn).inventory_rows()
    assert len(rows) == 1
    assert rows[0].item.name == "Mewtwo"
    assert rows[0].cost_basis_cents == 3000  # single card → basis = price paid
    assert ReportingService(conn).realized_pl_cents() == 0


def test_purchase_import_requires_price_and_date(tmp_path, conn):
    csv_path = tmp_path / "buys.csv"
    csv_path.write_text(
        "name,market value\nMewtwo,40.00\n",  # no price, no date
        encoding="utf-8",
    )
    preview = preview_import(csv_path, mode="purchases")
    assert preview.ok_count == 0
    assert preview.error_count >= 1
