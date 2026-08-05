"""The Operations window: tiles, running jobs, and the trimmed File menu."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tcginventory.data.db import connect  # noqa: E402
from tcginventory.data.migrator import migrate  # noqa: E402
from tcginventory.data.repositories import ItemRepository  # noqa: E402
from tcginventory.data.seed import seed_demo  # noqa: E402
from tcginventory.services import operations as ops  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def seeded_conn():
    conn = connect(":memory:")
    migrate(conn)
    seed_demo(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    """No modal dialogs, and never touch the real data directory."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)


def test_every_registered_operation_gets_a_tile_and_a_runner(app, seeded_conn):
    """The screen is drawn from the registry — a half-added operation (entry with
    no runner, or vice versa) fails here rather than at the click."""
    from tcginventory.ui.views.operations import OperationsView

    view = OperationsView(seeded_conn)
    assert set(view._tiles) == set(ops.BY_ID)
    for op_id in ops.BY_ID:
        assert view._runner(op_id) is not None, f"{op_id} has no runner"
    assert view._runner("models.something") is None  # nothing reserved is wired
    view.deleteLater()


def test_tiles_report_status_and_update_after_a_run(app, seeded_conn, tmp_path):
    from tcginventory.ui.views.operations import OperationsView

    view = OperationsView(seeded_conn)
    tile = view._tiles["export.inventory"]
    assert tile._pill.text() == "Never run"

    ops.record(seeded_conn, "export.inventory", summary="890 rows")
    view.refresh()

    assert tile._pill.text() == "Ready"
    assert "890 rows" in tile._value.text()
    view.deleteLater()


def test_export_runs_the_service_and_logs_it(app, seeded_conn, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from tcginventory.ui.views.operations import OperationsView

    target = tmp_path / "inventory.csv"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
    )
    view = OperationsView(seeded_conn)
    view._run(ops.BY_ID["export.inventory"])

    assert target.exists()
    row = ops.status_for(seeded_conn, ops.BY_ID["export.inventory"])
    assert row.state == ops.READY
    assert view._tiles["export.inventory"]._pill.text() == "Ready"
    # And it shows up in Activity rather than a popup that vanishes.
    assert any(
        "Export inventory" in line for _w, line, _ok in ops.activity(seeded_conn)
    )
    view.deleteLater()


def test_import_posts_rows_and_announces_the_change(app, tmp_path, monkeypatch):
    """An import changes the ledger, so the window tells the shell to refresh."""
    from PySide6.QtWidgets import QFileDialog

    from tcginventory.ui.views.operations import OperationsView

    conn = connect(":memory:")
    migrate(conn)
    csv_path = tmp_path / "items.csv"
    csv_path.write_text("name,set,market value\nMachamp,Base,20.00\n", encoding="utf-8")
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(csv_path), "")),
    )

    view = OperationsView(conn)
    announced: list[int] = []
    view.changed.connect(lambda: announced.append(1))
    view._run(ops.BY_ID["import.items"])

    assert "Machamp" in {i.name for i in ItemRepository(conn).list_all()}
    assert announced == [1]
    assert ops.status_for(conn, ops.BY_ID["import.items"]).state == ops.READY
    view.deleteLater()
    conn.close()


def test_cancelling_a_dialog_records_nothing(app, seeded_conn, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from tcginventory.ui.views.operations import OperationsView

    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", ""))
    )
    view = OperationsView(seeded_conn)
    view._run(ops.BY_ID["export.inventory"])

    assert ops.status_for(seeded_conn, ops.BY_ID["export.inventory"]).state == ops.NEVER
    view.deleteLater()


def test_background_operation_shows_running_then_reports(app, seeded_conn):
    """The tile owns the running state; the result lands in Activity, not a popup.

    Note the price tile's *status* comes from the price snapshots themselves —
    the truth for "when were prices last recorded" — while the run and its
    outcome are what the log carries.
    """
    from tcginventory.services.pricing import RefreshSummary
    from tcginventory.ui.views.operations import OperationsView

    view = OperationsView(seeded_conn)
    tile = view._tiles["prices.refresh"]

    tile.set_running(True)
    assert tile.bar.isVisibleTo(tile)
    assert not tile.button.isEnabled() and tile.button.text() == "Running…"

    tile.set_running(False)
    view._on_prices_refreshed(RefreshSummary(priced=12, unmapped=["x"]))

    assert tile.button.isEnabled() and tile.button.text() == "Refresh"
    assert not tile.bar.isVisibleTo(tile)
    line = ops.activity(seeded_conn)[0][1]
    assert "12 priced" in line and "1 skipped" in line
    view.deleteLater()


def test_a_failed_background_run_is_recorded_as_failed(app, seeded_conn):
    from tcginventory.services.pricing import RefreshSummary
    from tcginventory.ui.views.operations import OperationsView

    view = OperationsView(seeded_conn)
    view._on_prices_refreshed(RefreshSummary(errors=["tcgcsv unreachable"]))

    when, line, ok = ops.activity(seeded_conn)[0]
    assert ok is False and "tcgcsv unreachable" in line
    view.deleteLater()


def test_background_operation_declines_an_in_memory_database(app, seeded_conn):
    """Workers reopen the DB by path, which an in-memory database has none of."""
    from tcginventory.ui.views.operations import OperationsView

    view = OperationsView(seeded_conn)
    view._run(ops.BY_ID["prices.market_sweep"])
    assert view._workers == {}  # refused rather than crashing a thread
    view.deleteLater()


def _section_titles(view):
    from PySide6.QtWidgets import QLabel

    return [
        label.text()
        for label in view.findChildren(QLabel)
        if label.objectName() == "opSectionTitle"
    ]


def test_only_groups_with_operations_get_a_section(app, seeded_conn, monkeypatch):
    """A group renders exactly when it has operations — so a group can be reserved
    in the registry without putting an empty section on screen."""
    from tcginventory.ui.views.operations import OperationsView

    # Every shipped group has entries, so hold the property with a reserved one.
    monkeypatch.setattr(
        ops, "GROUPS", (*ops.GROUPS, ops.Group("reserved", "Nothing here yet"))
    )

    view = OperationsView(seeded_conn)
    titles = _section_titles(view)

    assert "Bring data in" in titles
    assert "Nothing here yet" not in titles  # empty → no section at all
    for group in ops.GROUPS:
        assert (group.label in titles) == bool(ops.in_group(group.id)), group.id
    view.deleteLater()


def test_each_model_gets_its_own_section(app, seeded_conn):
    """The three models are separate concerns with separate data and metrics, and
    the environment that trains them is machine setup rather than a model."""
    from tcginventory.ui.views.operations import OperationsView

    view = OperationsView(seeded_conn)
    titles = _section_titles(view)
    for label in (
        "Model training environment",
        "Region detection model",
        "Condition model",
        "Card identity model",
    ):
        assert label in titles, label

    # And nothing shares a section with an unrelated job any more.
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
    view.deleteLater()


def test_every_section_carries_a_heading_and_a_note(app, seeded_conn):
    """A section says what it is for, rather than leaving the tiles to imply it."""
    from tcginventory.ui.views.operations import _SECTION_NOTES, OperationsView

    view = OperationsView(seeded_conn)
    assert len(view._sections) == len(_section_titles(view))
    for section in view._sections:
        assert _SECTION_NOTES.get(section.group.id), section.group.id
    view.deleteLater()


def test_the_navigator_lists_the_sections_and_jumps_to_them(app, seeded_conn):
    from tcginventory.ui.views.operations import OperationsView

    view = OperationsView(seeded_conn)
    view.resize(1200, 700)
    view.show()
    app.processEvents()

    assert view._nav.count() == len(view._sections)
    assert [view._nav.item(i).text() for i in range(view._nav.count())] == [
        s.group.label for s in view._sections
    ]

    bar = view._scroll.verticalScrollBar()
    middle = len(view._sections) // 2
    view._nav.setCurrentRow(middle)  # a click on the navigator
    app.processEvents()
    assert bar.value() > 0  # it actually moved
    # That section's heading is now at the top of the viewport, near enough.
    assert abs(view._sections[middle].y() - bar.value()) <= 12

    # The final section sits inside the last viewport, so "jump to it" can only
    # scroll to the end — not a failure, just how far there is to go.
    view._nav.setCurrentRow(len(view._sections) - 1)
    app.processEvents()
    assert bar.value() == bar.maximum()
    view.close()
    view.deleteLater()


def test_the_navigator_follows_scrolling(app, seeded_conn):
    """Scrolling down moves the highlight, without that counting as a click."""
    from tcginventory.ui.views.operations import OperationsView

    view = OperationsView(seeded_conn)
    view.resize(1200, 700)
    view.show()
    app.processEvents()
    assert view._nav.currentRow() == 0

    third = view._sections[2]
    view._scroll.verticalScrollBar().setValue(third.y())
    app.processEvents()
    assert view._nav.currentRow() == 2
    view.close()
    view.deleteLater()


def test_maintenance_tasks_run_through_the_shared_worker(app, seeded_conn, tmp_path):
    """Integrity check and compaction are plain functions over a connection, so
    they can be exercised without a worker thread."""
    from tcginventory.ui.views.operations import _integrity_check, _vacuum

    assert "no problems found" in _integrity_check(seeded_conn)
    assert "compact" in _vacuum(seeded_conn) or "reclaimed" in _vacuum(seeded_conn)


def test_integrity_check_reports_a_broken_foreign_key(app, tmp_path):
    """A real orphan is raised, not swallowed into a cheerful summary."""
    from tcginventory.ui.views.operations import _integrity_check

    conn = connect(":memory:")
    migrate(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO lot (item_id, acquired_at, quantity, remaining_qty, "
        "unit_cost_cents, acquired_market_value_cents, intent, created_at) "
        "VALUES (9999, '2026-01-01', 1, 1, 100, 100, 'resale', '2026-01-01T00:00:00Z')"
    )
    with pytest.raises(RuntimeError, match="foreign-key orphan"):
        _integrity_check(conn)
    conn.close()


def test_refreshing_shows_without_states_says_so(app, seeded_conn, monkeypatch):
    from tcginventory.ui.views.operations import _refresh_shows

    monkeypatch.setattr(
        "tcginventory.config.get_pref",
        lambda key, default=None: [] if key == "show_states" else default,
    )
    with pytest.raises(RuntimeError, match="No states selected"):
        _refresh_shows(seeded_conn)


def test_task_failures_are_logged_as_failures(app, seeded_conn):
    from tcginventory.ui.views.operations import OperationsView

    view = OperationsView(seeded_conn)
    view._on_task_done("db.vacuum", (False, "database is locked"))

    when, line, ok = ops.activity(seeded_conn)[0]
    assert ok is False and "database is locked" in line
    view.deleteLater()


def test_digest_banner_appears_for_background_work_and_dismisses(
    app, seeded_conn, tmp_path
):
    from tcginventory.ui.views.operations import OperationsView

    view = OperationsView(seeded_conn)
    assert not view._digest.isVisibleTo(view)  # nothing happened while away

    ops.record(seeded_conn, "prices.refresh", summary="412 priced", source="scheduled")
    ops.record(
        seeded_conn, "shows.refresh", ok=False, summary="timeout", source="scheduled"
    )
    view.refresh()

    assert view._digest.isVisibleTo(view)
    assert view._digest.property("bad") is True  # a failure tints the rail amber

    summary = ops.digest(seeded_conn)
    view._dismiss_digest(summary.newest)

    assert not view._digest.isVisibleTo(view)
    view.refresh()
    assert not view._digest.isVisibleTo(view)  # and it stays dismissed
    view.deleteLater()


def _install_fake_env(tmp_path):
    """An environment on disk plus the recorded interpreter, as setup leaves it."""
    from tcginventory.services import training, trainingenv

    env = tmp_path / trainingenv.ENV_DIRNAME
    (env / ("Scripts" if os.name == "nt" else "bin")).mkdir(parents=True)
    python = trainingenv.env_python(env)
    python.write_bytes(b"x" * 2048)
    training.remember_training_env(
        training.TrainingEnv(str(python), True, "8.4.115", "ultralytics 8.4.115")
    )
    return env


def test_setup_button_greys_out_once_the_environment_exists(app, seeded_conn, tmp_path):
    """Regression: 'Set up model training' stayed clickable after setup, so it
    would happily rebuild an environment that was already there."""
    from tcginventory.ui.views.operations import OperationsView

    view = OperationsView(seeded_conn)
    setup = view._tiles["models.training_env"]
    remove = view._tiles["models.training_env_remove"]
    assert not remove.button.isEnabled()  # nothing installed, nothing to remove

    _install_fake_env(tmp_path)
    view.refresh()

    assert not setup.button.isEnabled()
    assert "remove the environment first" in setup.button.toolTip().lower()
    assert remove.button.isEnabled()  # …and its opposite comes alive
    view.deleteLater()


def test_removing_the_environment_revives_the_setup_button(app, seeded_conn, tmp_path):
    """The pair flips together — finishing one refreshes its whole group."""
    from tcginventory.services import trainingenv
    from tcginventory.ui.views.operations import OperationsView, _remove_training_env

    env = _install_fake_env(tmp_path)
    view = OperationsView(seeded_conn)
    assert not view._tiles["models.training_env"].button.isEnabled()

    summary = _remove_training_env(seeded_conn)
    view._log("models.training_env_remove", True, summary)

    assert not env.exists()
    assert "models kept" in summary
    assert view._tiles["models.training_env"].button.isEnabled()
    assert not view._tiles["models.training_env_remove"].button.isEnabled()
    assert not trainingenv.is_bootstrapped()
    view.deleteLater()


def test_removing_the_environment_is_confirmed_first(
    app, seeded_conn, tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QMessageBox

    from tcginventory.ui.views.operations import OperationsView

    env = _install_fake_env(tmp_path)
    asked: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: (asked.append(a[2]), QMessageBox.No)[1]),
    )
    view = OperationsView(seeded_conn)
    view._run(ops.BY_ID["models.training_env_remove"])

    assert env.exists()  # declined → nothing deleted
    assert "models" in asked[0].lower() and "corpus" in asked[0].lower()
    view.deleteLater()


def test_file_menu_is_trimmed_to_operations_and_exit(app, seeded_conn):
    from tcginventory.ui.main_window import MainWindow

    window = MainWindow(seeded_conn)
    labels = [a.text() for a in window._file_menu.actions() if a.text()]
    assert labels == ["Operations", "Exit"]
    # The verbs really did move — no stray import/export handlers left behind.
    for gone in ("_import_items", "_export_all", "_backup_database", "_run_import"):
        assert not hasattr(window, gone), f"{gone} should live on the Operations view"
    window.close()


def test_menu_and_nav_both_reach_the_operations_screen(app, seeded_conn):
    from tcginventory.ui.main_window import _OPERATIONS_INDEX, NAV_ITEMS, MainWindow

    window = MainWindow(seeded_conn)
    assert NAV_ITEMS[-1] == "Operations"
    assert window._stack.count() == len(NAV_ITEMS)

    window._operations_action.trigger()

    assert window._nav.currentRow() == _OPERATIONS_INDEX
    assert window._stack.currentWidget() is window._operations
    window.close()
