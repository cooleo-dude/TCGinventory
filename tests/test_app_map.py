"""The `/task-planning` map must keep matching the code it describes.

`.claude/context/app-map.json` is what a fresh session reads instead of guessing
at the architecture. A map that has quietly drifted is worse than no map — it
produces a confident, wrong brief. These tests make drift a red suite rather
than a bad plan, and they reuse the skill's own validator so the check the agent
runs and the check CI runs cannot diverge.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude/skills/task-planning"
SCRIPT = SKILL / "scripts/plan.py"
MAP = ROOT / ".claude/context/app-map.json"


def _load_plan():
    spec = importlib.util.spec_from_file_location("_task_planning_plan", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def plan():
    if not SCRIPT.exists():
        pytest.skip("task-planning skill is not installed in this checkout")
    return _load_plan()


@pytest.fixture(scope="module")
def app_map():
    return json.loads(MAP.read_text(encoding="utf-8"))


def test_map_matches_the_tree(plan, app_map):
    """Every path, the nav order, and every view class the map names."""
    problems = plan.validate(ROOT, app_map)
    assert not problems, "app-map.json has drifted:\n  - " + "\n  - ".join(problems)


def test_every_window_resolves_from_its_own_aliases(plan, app_map):
    """A window nobody can name is a window nobody gets a dossier for."""
    for key, win in app_map["windows"].items():
        for name in [key, win["nav_label"], *win.get("aliases", [])]:
            request = f"{name} — do a thing"
            head, _task, _depth = plan.split_request(request)
            got, ambiguous = plan.resolve_window(head, request, app_map)
            assert got == key, (
                f"{name!r} resolved to {got or ambiguous} instead of {key}"
            )


def test_depth_and_task_are_parsed_off_the_request(plan):
    head, task, depth = plan.split_request(
        "inventory — add a 'days held' column | depth: deep"
    )
    assert (head, task, depth) == ("inventory", "add a 'days held' column", "deep")

    head, task, depth = plan.split_request("operations — prune old backups")
    assert (head, task, depth) == ("operations", "prune old backups", "standard")


def test_brief_renders_for_every_window(plan, app_map):
    """The renderer must survive every entry, including read-only screens with
    no signals and screens with no services."""
    for key, win in app_map["windows"].items():
        out = plan.render(ROOT, app_map, key, "adjust a column", "standard", key)
        assert win["nav_label"] in out
        assert win["file"] in out
        assert "Required reads" in out
        assert "Definition of done" in out


def test_required_reads_are_repo_relative_and_real(plan, app_map):
    """A path an agent cannot Read is worse than no path at all."""
    for key, win in app_map["windows"].items():
        rows = plan.reading_list(ROOT, app_map, win, "deep", "money and schema")
        assert rows, key
        for path, why, ok in rows:
            assert ok, f"{key}: required read does not exist — {path} ({why})"
            assert (ROOT / path).exists(), f"{key}: {path} is not repo-relative"


def test_money_and_schema_flags_fire_on_the_right_words(plan):
    assert plan.MONEY_WORDS.search("recompute the realized profit")
    assert plan.SCHEMA_WORDS.search("add a column for it")
    assert not plan.MONEY_WORDS.search("tint the nav icon on hover")
    assert not plan.SCHEMA_WORDS.search("tint the nav icon on hover")


def test_next_migration_follows_the_directory(plan):
    nxt = plan.next_migration(ROOT)
    existing = sorted((ROOT / "src/tcginventory/data/migrations").glob("*.sql"))
    assert nxt.isdigit() and len(nxt) == 4
    assert not any(f.name.startswith(nxt) for f in existing)
