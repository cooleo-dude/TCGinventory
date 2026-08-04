"""The operations registry, run log and status providers (headless, Qt-free)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tcginventory.data.repositories import (
    CardCatalogRepository,
    OperationRunRepository,
    PriceSnapshotRepository,
)
from tcginventory.domain.models import CatalogCard, PriceSnapshot
from tcginventory.services import operations as ops


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_registry_is_internally_consistent():
    """Every entry is addressable, grouped and complete — the guard that keeps a
    future operation from being half-added."""
    group_ids = {g.id for g in ops.GROUPS}
    seen: set[str] = set()
    for op in ops.OPERATIONS:
        assert op.id not in seen, f"duplicate operation id {op.id}"
        seen.add(op.id)
        assert op.group in group_ids, f"{op.id} is in unknown group {op.group}"
        assert op.title and op.blurb, f"{op.id} needs a title and a description"
        assert op.kind in (ops.KIND_DIALOG, ops.KIND_BACKGROUND, ops.KIND_TOGGLE)
        if op.kind != ops.KIND_TOGGLE:
            assert op.verb, f"{op.id} needs a button label"
    assert ops.BY_ID.keys() == seen
    # At most one accent-filled button on the screen.
    assert sum(1 for op in ops.OPERATIONS if op.primary) <= 1


def test_models_group_is_reserved_but_empty():
    """Space is held for the coming model metrics/triggers; nothing renders until
    the first entry lands (the view skips empty groups)."""
    assert any(g.id == "models" for g in ops.GROUPS)
    assert ops.in_group("models") == []


def test_run_log_records_last_run_and_prunes(conn):
    repo = OperationRunRepository(conn)
    assert repo.last_run("export.inventory") is None

    repo.record("export.inventory", summary="880 rows")
    repo.record("export.inventory", summary="890 rows")
    repo.record("prices.refresh", ok=False, summary="offline", source="scheduled")

    latest = repo.last_run("export.inventory")
    assert latest["summary"] == "890 rows" and latest["source"] == "ui"
    failed = repo.last_run("prices.refresh")
    assert failed["ok"] == 0 and failed["source"] == "scheduled"
    assert set(repo.last_runs()) == {"export.inventory", "prices.refresh"}
    assert [r["summary"] for r in repo.recent(2)] == ["offline", "890 rows"]

    for i in range(30):
        repo.record("noise", summary=str(i))
    assert repo.prune(keep=5) == 28
    assert len(repo.recent(999)) == 5


def test_status_reads_never_then_the_log(conn):
    op = ops.BY_ID["export.inventory"]
    assert ops.status_for(conn, op).state == ops.NEVER

    ops.record(conn, op.id, summary="890 rows")
    status = ops.status_for(conn, op)
    assert status.state == ops.READY
    assert "890 rows" in status.value

    # A failed run is surfaced, not hidden behind a cheerful "Ready".
    ops.record(conn, op.id, ok=False, summary="disk full")
    assert ops.status_for(conn, op).state == ops.STALE


def test_status_goes_stale_after_its_own_window(conn):
    """export.full is the escape hatch, so a fortnight-old one is flagged."""
    op = ops.BY_ID["export.full"]
    assert op.stale_after_days == 14
    ops.record(conn, op.id, summary="wrote CSVs", started_at=_iso(30))
    assert ops.status_for(conn, op).state == ops.STALE
    ops.record(conn, op.id, summary="wrote CSVs", started_at=_iso(1))
    assert ops.status_for(conn, op).state == ops.READY


def test_price_and_catalog_statuses_come_from_their_own_data(conn):
    from conftest import make_item
    from tcginventory.data.repositories import ItemRepository

    snapshots = PriceSnapshotRepository(conn)
    catalog = CardCatalogRepository(conn)

    assert ops.status_for(conn, ops.BY_ID["prices.refresh"]).state == ops.NEVER
    assert ops.status_for(conn, ops.BY_ID["catalog.update_en"]).value == "not loaded"

    item = ItemRepository(conn).add(make_item("Pikachu"))
    snapshots.add(
        PriceSnapshot(item_id=item.id, source="test", price_cents=100, as_of=_iso(0))
    )
    assert ops.status_for(conn, ops.BY_ID["prices.refresh"]).state == ops.READY

    catalog.replace_for_language(
        [
            CatalogCard(
                card_id="sv9-1",
                name="Pikachu",
                set_id="sv9",
                set_name="Journey Together",
                number="1",
                language="EN",
            )
        ],
        "EN",
        _iso(0),
    )
    english = ops.status_for(conn, ops.BY_ID["catalog.update_en"])
    assert english.state == ops.READY and "1 cards" in english.value
    # The Japanese tile stays honest about its own language.
    assert ops.status_for(conn, ops.BY_ID["catalog.update_jp"]).value == "not loaded"


def test_backup_facts_read_the_files_on_disk(tmp_path):
    assert ops.backup_facts(tmp_path) == (0, None, 0)
    for name in ("inventory-20260101-000000.db", "inventory-20260102-000000.db"):
        (tmp_path / name).write_bytes(b"x" * 2048)
    (tmp_path / "pricehistory-20260101-000000.db").write_bytes(b"y")  # not counted
    count, newest, total = ops.backup_facts(tmp_path)
    assert (count, total) == (2, 4096)
    assert newest is not None


def test_system_facts_describe_the_install(conn):
    facts = {f.caption: f.value for f in ops.system_facts(conn)}
    assert facts["Items"].startswith("0 cards")
    assert facts["Prices updated"] == "never"
    assert facts["Schema"].startswith("v")


def test_activity_names_operations_and_marks_failures(conn):
    ops.record(conn, "export.inventory", summary="890 rows")
    ops.record(
        conn, "shows.refresh", ok=False, summary="tcdb timeout", source="scheduled"
    )

    lines = ops.activity(conn)
    assert lines[0][1].startswith("Refresh card shows — tcdb timeout")
    assert "(scheduled)" in lines[0][1]  # background work is labelled as such
    assert lines[0][2] is False
    assert lines[1][1].startswith("Export inventory to CSV — 890 rows")
    assert lines[1][2] is True


def test_maintenance_and_shows_operations_are_registered():
    """The hub carries more than the old File menu: shows, corpus and DB health."""
    assert {op.id for op in ops.in_group("maintenance")} == {
        "vision.reindex",
        "db.integrity_check",
        "db.vacuum",
    }
    assert "shows.refresh" in {op.id for op in ops.in_group("data_in")}


def test_shows_status_counts_what_is_stored(conn):
    from datetime import date

    from tcginventory.data.repositories import CardShowRepository
    from tcginventory.domain.models import CardShow

    op = ops.BY_ID["shows.refresh"]
    assert ops.status_for(conn, op).state == ops.NEVER

    soon = (date.today() + timedelta(days=20)).isoformat()
    CardShowRepository(conn).upsert(
        CardShow(
            source_id=1,
            name="Fall Expo",
            state="NJ",
            starts_on=soon,
            fetched_at=_iso(0),
        )
    )
    ops.record(conn, op.id, summary="1 show across NJ")

    status = ops.status_for(conn, op)
    assert "1 upcoming" in status.value and status.state == ops.READY


def test_corpus_facts_survive_a_missing_vision_database(monkeypatch, tmp_path):
    """The corpus is an optional side file — no corpus is 'empty', not a crash."""
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    assert ops.corpus_facts() == (0, 0, 0)


def test_corpus_status_flags_labelled_but_unindexed_cards(conn):
    from tcginventory.data.vision import CardLabel, VisionRepository, open_vision

    vision = open_vision(":memory:")
    repo = VisionRepository(vision)
    capture = repo.add_capture(source="transaction")
    image = repo.add_image(capture, "sha", "/tmp/scene.png", width=10, height=10)
    repo.add_region(image, 1, (0, 0, 1, 1), None, CardLabel(card_id="cel-4"))

    captures, labelled, indexed = ops.corpus_facts(vision)
    assert (captures, labelled, indexed) == (1, 1, 0)
    vision.close()


def test_digest_reports_only_unacknowledged_background_work(
    conn, monkeypatch, tmp_path
):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    assert ops.digest(conn) is None

    ops.record(conn, "export.inventory", summary="by hand")  # source='ui'
    assert ops.digest(conn) is None, "the user's own runs aren't news to them"

    ops.record(conn, "prices.refresh", summary="412 priced", source="scheduled")
    ops.record(conn, "shows.refresh", ok=False, summary="timeout", source="scheduled")

    summary = ops.digest(conn)
    assert summary is not None
    assert summary.failures == 1
    assert "2 background runs, 1 failed" in summary.headline
    assert any("412 priced" in line for line in summary.lines)

    ops.acknowledge_digest(summary.newest)
    assert ops.digest(conn) is None  # dismissed stays dismissed

    ops.record(conn, "prices.market_sweep", summary="6,110 changes", source="scheduled")
    later = ops.digest(conn)
    assert later is not None and len(later.lines) == 1


def test_when_renders_recent_stamps_readably():
    assert ops.when(None) == "never"
    assert ops.when(_iso(0)).startswith("today")
    assert ops.when(_iso(1.2)).startswith("yesterday")
    assert ops.when(_iso(40))[:2] == "20"  # falls back to the date
