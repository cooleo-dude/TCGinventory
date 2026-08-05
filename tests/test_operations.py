"""The operations registry, run log and status providers (headless, Qt-free)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

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


def test_each_recognition_model_has_its_own_section():
    """One section per concern: the environment that trains the models is machine
    setup, and the three models have separate data, metrics and lifecycles. The
    identifier gets no Train button at all — a labelled crop is its training step.
    """
    assert [op.id for op in ops.in_group("training_env")] == [
        "models.training_env",
        "models.training_env_gpu",
        "models.training_env_remove",
    ]
    assert [op.id for op in ops.in_group("model_regions")] == ["models.detector.train"]
    assert [op.id for op in ops.in_group("model_condition")] == [
        "models.condition.train"
    ]
    assert [op.id for op in ops.in_group("model_identity")] == ["vision.reindex"]
    # Both trainers report their age, so a model left alone reads as stale.
    for op_id in ("models.detector.train", "models.condition.train"):
        op = ops.BY_ID[op_id]
        assert op.caption == "Last trained"
        assert op.stale_after_days == 14


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


def test_sections_do_not_mix_unrelated_jobs():
    """Regrouped 2026-08-05: a card-show scrape is reference data rather than a CSV
    import, prices and the card database are different subjects, and re-indexing the
    corpus is the identity model's upkeep rather than database hygiene."""
    assert {op.id for op in ops.in_group("maintenance")} == {
        "db.integrity_check",
        "db.vacuum",
    }
    assert {op.id for op in ops.in_group("data_in")} == {
        "import.items",
        "import.purchases",
    }
    assert {op.id for op in ops.in_group("prices")} == {
        "prices.refresh",
        "prices.market_sweep",
    }
    assert {op.id for op in ops.in_group("reference")} == {
        "shows.refresh",
        "catalog.update_en",
        "catalog.update_jp",
    }
    # Every operation still lands in a declared section.
    known = {g.id for g in ops.GROUPS}
    assert all(op.group in known for op in ops.OPERATIONS)


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


# -- Models & signals statuses ----------------------------------------------


@pytest.fixture()
def vision_db(tmp_path, monkeypatch):
    """A real vision.db on disk, since the model statuses open it by path."""
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.data.vision import (
        ModelRunRepository,
        VisionRepository,
        open_vision,
    )

    conn = open_vision()
    yield VisionRepository(conn), ModelRunRepository(conn)
    conn.close()


def _labelled_scene(repo, tmp_path, boxes=2, condition=None):
    from PIL import Image

    from tcginventory.data.vision import CardLabel

    scene = tmp_path / f"scene{len(list(repo.conn.execute('SELECT 1 FROM image')))}.png"
    Image.new("RGB", (200, 140), color=(180, 180, 180)).save(scene)
    cap = repo.add_capture(source="manual")
    img = repo.add_image(cap, f"sha{scene.stem}", str(scene), "front")
    for i in range(boxes):
        repo.add_region(
            img,
            i + 1,
            (0.1 * i, 0.1, 0.2, 0.5),
            None,
            CardLabel(card_id=f"c{i}", condition=condition),
        )


def test_detector_status_never_trained_lists_what_is_waiting(conn, vision_db, tmp_path):
    repo, _runs = vision_db
    _labelled_scene(repo, tmp_path, boxes=3)
    status = ops.status_for(conn, ops.BY_ID["models.detector.train"])
    assert status.state == ops.NEVER
    assert "never" in status.value and "3 boxes" in status.value


def test_detector_status_reports_version_age_and_metrics(conn, vision_db, tmp_path):
    from datetime import datetime, timezone

    repo, runs = vision_db
    _labelled_scene(repo, tmp_path, boxes=2)
    from tcginventory.services import training

    snap = training.corpus_snapshot(repo)
    run_id = runs.start(
        corpus_images=snap.images,
        corpus_boxes=snap.boxes,
        corpus_cards=snap.cards,
        corpus_fingerprint=snap.fingerprint,
    )
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    runs.finish(run_id, map50=0.6, map50_95=0.31, now=fresh)
    runs.promote(run_id)

    status = ops.status_for(conn, ops.BY_ID["models.detector.train"])
    assert status.state == ops.READY  # trained on exactly this corpus, and recent
    assert status.value.startswith("v1 · ")
    assert "mAP50-95 0.310" in status.value
    assert "Trained on 1 images / 2 boxes" in (status.detail or "")


def test_a_model_older_than_a_fortnight_reads_as_stale(conn, vision_db, tmp_path):
    """The marker the Regions/Conditions sections are read for: left alone too
    long, a model warns even when the corpus hasn't changed."""
    from datetime import datetime, timedelta, timezone

    repo, runs = vision_db
    _labelled_scene(repo, tmp_path, boxes=2)
    from tcginventory.services import training

    snap = training.corpus_snapshot(repo)
    run_id = runs.start(
        corpus_images=snap.images,
        corpus_boxes=snap.boxes,
        corpus_cards=snap.cards,
        corpus_fingerprint=snap.fingerprint,
    )
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(
        timespec="seconds"
    )
    runs.finish(run_id, map50_95=0.31, now=old)
    runs.promote(run_id)

    status = ops.status_for(conn, ops.BY_ID["models.detector.train"])
    assert status.state == ops.STALE  # 20 days > the 14-day window
    assert ops.BY_ID["models.detector.train"].stale_after_days == 14


def test_new_labels_make_a_current_model_stale(conn, vision_db, tmp_path):
    from datetime import datetime, timezone

    repo, runs = vision_db
    _labelled_scene(repo, tmp_path, boxes=2)
    from tcginventory.services import training

    snap = training.corpus_snapshot(repo)
    run_id = runs.start(
        corpus_images=snap.images,
        corpus_boxes=snap.boxes,
        corpus_cards=snap.cards,
        corpus_fingerprint=snap.fingerprint,
    )
    runs.finish(
        run_id,
        map50_95=0.31,
        now=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    runs.promote(run_id)
    assert ops.status_for(conn, ops.BY_ID["models.detector.train"]).state == ops.READY

    (tmp_path / "second").mkdir()
    _labelled_scene(repo, tmp_path / "second", boxes=4)  # more labels land
    status = ops.status_for(conn, ops.BY_ID["models.detector.train"])
    assert status.state == ops.STALE
    assert "New since" in (status.detail or "")


def test_condition_status_counts_graded_cards_and_says_what_is_missing(
    conn, vision_db, tmp_path
):
    """The grader's unit is a card, not a face — and it reports the gate, so the
    tile can grey its button rather than start a run that can't succeed."""
    repo, _runs = vision_db
    status = ops.status_for(conn, ops.BY_ID["models.condition.train"])
    assert status.state == ops.NEVER
    assert "no graded cards yet" in status.value
    assert status.actionable is False

    _labelled_scene(repo, tmp_path, boxes=2, condition="NM")
    graded = ops.status_for(conn, ops.BY_ID["models.condition.train"])
    assert "2 graded cards" in graded.value
    assert graded.actionable is False  # nowhere near enough of each grade
    assert "Still needed" in (graded.detail or "")


def test_training_env_status_warns_when_no_python_is_found(conn, monkeypatch, tmp_path):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.services import trainingenv

    monkeypatch.setattr(trainingenv, "discover_interpreters", lambda: [])
    status = ops.status_for(conn, ops.BY_ID["models.training_env"])
    assert status.state == ops.OFF
    assert "no suitable Python detected" in status.value
    assert "Install Python 3.10 or newer" in (status.detail or "")


def test_training_env_status_names_the_interpreter_it_would_use(
    conn, monkeypatch, tmp_path
):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.services import trainingenv

    monkeypatch.setattr(
        trainingenv,
        "discover_interpreters",
        lambda: [trainingenv.Interpreter(r"C:\py\python.exe", (3, 12), "registry")],
    )
    status = ops.status_for(conn, ops.BY_ID["models.training_env"])
    assert status.state == ops.NEVER
    assert "Python 3.12" in status.value
    assert "280 MB" in (status.detail or "")


def test_training_env_status_reports_a_recorded_environment(
    conn, monkeypatch, tmp_path
):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.services import training

    fake = tmp_path / "python.exe"
    fake.write_bytes(b"")
    training.remember_training_env(
        training.TrainingEnv(str(fake), True, "8.4.115", "ultralytics 8.4.115")
    )
    status = ops.status_for(conn, ops.BY_ID["models.training_env"])
    assert status.state == ops.READY
    assert "8.4.115" in status.value
    # Already built: nothing for the button to do, and it says why.
    assert status.actionable is False
    assert "remove the environment first" in (status.detail or "").lower()


def _fake_env(tmp_path):
    """A training environment on disk, laid out the way setup builds it."""
    from tcginventory.services import trainingenv

    env = tmp_path / trainingenv.ENV_DIRNAME
    binaries = env / ("Scripts" if os.name == "nt" else "bin")
    binaries.mkdir(parents=True)
    python = trainingenv.env_python(env)
    python.write_bytes(b"x" * 1024)
    (env / "Lib").mkdir()
    (env / "Lib" / "torch.pyd").write_bytes(b"y" * 4096)
    return env, python


def test_setup_stays_clickable_while_the_environment_is_incomplete(
    conn, monkeypatch, tmp_path
):
    """A half-built env is repaired by running setup again — greying that out
    would leave the user with no way forward but deletion."""
    from tcginventory.services import trainingenv

    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        trainingenv,
        "discover_interpreters",
        lambda: [trainingenv.Interpreter(r"C:\py\python.exe", (3, 12), "registry")],
    )
    _fake_env(tmp_path)  # exists, but nothing was ever validated

    status = ops.status_for(conn, ops.BY_ID["models.training_env"])
    assert status.state == ops.STALE
    assert status.actionable is True


def test_remove_tile_is_dead_until_an_environment_exists(conn, monkeypatch, tmp_path):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    op = ops.BY_ID["models.training_env_remove"]

    status = ops.status_for(conn, op)
    assert status.state == ops.NEVER and status.actionable is False
    assert "nothing to remove" in status.value

    _fake_env(tmp_path)
    status = ops.status_for(conn, op)
    assert status.actionable is True
    assert "5.0 KB" in status.value  # the 1 KB stub + the 4 KB package
    assert "models" in (status.detail or "").lower()


def test_removing_the_environment_keeps_models_and_forgets_the_interpreter(
    monkeypatch, tmp_path
):
    from tcginventory.services import training, trainingenv

    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    env, python = _fake_env(tmp_path)
    training.remember_training_env(
        training.TrainingEnv(str(python), True, "8.4.115", "ultralytics 8.4.115")
    )
    # The things that must survive: trained models and the labelled corpus.
    models = tmp_path / "vision" / "models"
    models.mkdir(parents=True)
    (models / "card_yolo.onnx").write_bytes(b"model")
    (tmp_path / "vision.db").write_bytes(b"corpus")

    reclaimed = trainingenv.remove_env()

    assert reclaimed == 5120
    assert not env.exists()
    assert (models / "card_yolo.onnx").read_bytes() == b"model"
    assert (tmp_path / "vision.db").exists()
    # The recorded interpreter pointed inside the env, so it is forgotten.
    assert training.training_python() is None
    assert training.training_env_summary().ready is False


def test_removing_the_environment_keeps_a_hand_picked_interpreter(
    monkeypatch, tmp_path
):
    """Someone who pointed the app at their own Python keeps that choice."""
    from tcginventory.services import training, trainingenv

    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    _fake_env(tmp_path)
    outside = tmp_path / "system-python.exe"
    outside.write_bytes(b"")
    training.remember_training_env(
        training.TrainingEnv(str(outside), True, "8.4.115", "ultralytics 8.4.115")
    )

    trainingenv.remove_env()

    assert training.training_python() == str(outside)


def test_remove_env_refuses_anything_that_is_not_the_environment(tmp_path):
    """A guard on the one operation in the app that deletes a directory."""
    from tcginventory.services import trainingenv

    precious = tmp_path / "vision"
    precious.mkdir()
    with pytest.raises(RuntimeError, match="Not a training environment"):
        trainingenv.remove_env(precious)
    assert precious.exists()

    with pytest.raises(RuntimeError, match="Not a training environment"):
        trainingenv.remove_env(tmp_path / trainingenv.ENV_DIRNAME)  # absent


# -- choosing between a CPU and a GPU environment ----------------------------


def _gpu(vram=16376):
    from tcginventory.services.trainingenv import Gpu

    return Gpu("NVIDIA GeForce RTX 4070 Ti SUPER", vram, "591.86", "8.9")


def _installed(monkeypatch, tmp_path, flavour):
    """Pretend an environment of ``flavour`` is built and validated."""
    from tcginventory.services import training

    fake = tmp_path / "python.exe"
    fake.write_bytes(b"")
    training.remember_training_env(
        training.TrainingEnv(str(fake), True, "8.4.115", "ultralytics 8.4.115")
    )
    training.remember_training_flavour(flavour)


def test_gpu_tile_is_dead_without_a_card(conn, monkeypatch, tmp_path):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.services import trainingenv

    monkeypatch.setattr(trainingenv, "detect_gpu", lambda: None)
    status = ops.status_for(conn, ops.BY_ID["models.training_env_gpu"])
    assert status.state == ops.OFF
    assert status.actionable is False  # the tile greys its button
    assert "no suitable GPU detected" in status.value
    assert "NVIDIA" in (status.detail or "")


def test_gpu_tile_is_dead_when_the_card_is_too_small(conn, monkeypatch, tmp_path):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.services import trainingenv

    monkeypatch.setattr(trainingenv, "detect_gpu", lambda: _gpu(vram=2048))
    status = ops.status_for(conn, ops.BY_ID["models.training_env_gpu"])
    assert status.state == ops.OFF and status.actionable is False
    assert "too little memory" in status.value


def test_gpu_tile_offers_a_fresh_build_when_nothing_is_installed(
    conn, monkeypatch, tmp_path
):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.services import trainingenv

    monkeypatch.setattr(trainingenv, "detect_gpu", lambda: _gpu())
    status = ops.status_for(conn, ops.BY_ID["models.training_env_gpu"])
    assert status.state == ops.NEVER and status.actionable
    assert "ready to build" in status.value
    assert status.verb is None  # the registry's "Set up with GPU" stands


def test_each_tile_offers_the_other_flavour(conn, monkeypatch, tmp_path):
    """The pair describe one environment from opposite sides: whichever is
    installed has nothing to do, and the other offers to switch."""
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.services import trainingenv

    monkeypatch.setattr(trainingenv, "detect_gpu", lambda: _gpu())

    _installed(monkeypatch, tmp_path, trainingenv.FLAVOUR_CPU)
    cpu = ops.status_for(conn, ops.BY_ID["models.training_env"])
    gpu = ops.status_for(conn, ops.BY_ID["models.training_env_gpu"])
    assert cpu.actionable is False  # already the installed flavour
    assert gpu.actionable and gpu.verb == "Switch to GPU environment"

    _installed(monkeypatch, tmp_path, trainingenv.FLAVOUR_GPU)
    cpu = ops.status_for(conn, ops.BY_ID["models.training_env"])
    gpu = ops.status_for(conn, ops.BY_ID["models.training_env_gpu"])
    assert cpu.actionable and cpu.verb == "Switch to CPU environment"
    assert gpu.actionable is False
    assert "in use" in gpu.value


def test_an_environment_from_before_gpu_support_reads_as_cpu(
    conn, monkeypatch, tmp_path
):
    """Regression: a recorded environment carries no flavour until it is rebuilt,
    and treating that as "nothing installed" would tell users to set up again."""
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    from tcginventory.services import training, trainingenv

    monkeypatch.setattr(trainingenv, "detect_gpu", lambda: _gpu())
    _installed(monkeypatch, tmp_path, trainingenv.FLAVOUR_CPU)
    training.remember_training_flavour(None)  # as an older install would be

    assert training.training_env_flavour() is None
    cpu = ops.status_for(conn, ops.BY_ID["models.training_env"])
    assert cpu.state == ops.READY and cpu.actionable is False
    assert ops.status_for(conn, ops.BY_ID["models.training_env_gpu"]).verb == (
        "Switch to GPU environment"
    )


def test_the_gpu_build_installs_cuda_torch_before_ultralytics():
    """Order matters: ultralytics first would pull the CPU wheels and then find its
    dependency satisfied, leaving a CPU environment wearing a GPU label."""
    from tcginventory.services import trainingenv

    steps = trainingenv.bootstrap_steps("py.exe", flavour=trainingenv.FLAVOUR_GPU)
    commands = [" ".join(argv) for _label, argv in steps]
    assert "venv" in commands[0]
    assert trainingenv.CUDA_INDEX in commands[1] and "torch" in commands[1]
    assert "ultralytics" in commands[2]
    assert "ultralytics" not in commands[1]  # nothing pulls torch early

    cpu = trainingenv.bootstrap_steps("py.exe", flavour=trainingenv.FLAVOUR_CPU)
    assert len(cpu) == 2  # no CUDA step at all
    assert trainingenv.CUDA_INDEX not in " ".join(cpu[1][1])


def test_detect_gpu_reads_nvidia_smi(monkeypatch):
    import subprocess

    from tcginventory.services import trainingenv

    class Fake:
        returncode = 0
        stdout = (
            "NVIDIA GeForce GTX 1650, 4096, 555.00, 7.5\n"
            "NVIDIA GeForce RTX 4070 Ti SUPER, 16376, 591.86, 8.9\n"
        )

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Fake())
    gpu = trainingenv.detect_gpu()
    assert gpu is not None
    assert gpu.vram_mb == 16376  # the biggest card wins
    assert gpu.usable and "4070" in gpu.label


def test_detect_gpu_is_none_without_the_driver(monkeypatch):
    import subprocess

    from tcginventory.services import trainingenv

    def boom(*a, **k):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(subprocess, "run", boom)
    assert trainingenv.detect_gpu() is None
