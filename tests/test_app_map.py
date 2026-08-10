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
import re
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


def test_the_money_flag_does_not_fire_on_plan_shaped_words(plan):
    """`p/?l` used to collapse to a bare `pl`, and with the trailing `\\w*` it
    matched `plan`, `planning`, `please` and `platform` — putting
    specs/02-data-model.md at the top of the reading list for any task with the
    word "planning" in it. P/L itself must still match."""
    for word in ("planning", "plan the work", "please do it", "platform"):
        assert not plan.MONEY_WORDS.search(word), word
    for word in ("the p/l chart", "P/L", "p&l"):
        assert plan.MONEY_WORDS.search(word), word


def test_next_migration_follows_the_directory(plan):
    nxt = plan.next_migration(ROOT)
    existing = sorted((ROOT / "src/tcginventory/data/migrations").glob("*.sql"))
    assert nxt.isdigit() and len(nxt) == 4
    assert not any(f.name.startswith(nxt) for f in existing)


# -- the dossier reads the rule registry, not the decision log (ticket 010) --


def test_the_decision_log_is_never_read_whole(plan, app_map):
    """It is 89 entries and ~30k tokens, it is append-only so that cost only
    rises, and it answers the wrong question. What binds comes from the registry.

    Four windows do have their design in the log rather than in a spec file —
    those stay, but as bounded section reads with a line range, never the file.
    """
    for key, win in app_map["windows"].items():
        rows = plan.reading_list(ROOT, app_map, win, "deep", "money and schema")
        for path, why, _ok in rows:
            if path == "docs/decisions.md":
                assert re.search(r"lines \d+-\d+", why), f"{key}: whole-file read"
        out = plan.render(ROOT, app_map, key, "adjust a column", "standard", key)

    # Writing one is still expected — only the whole-file *read* is gone.
    assert "recorded in docs/decisions.md" in out


def test_a_window_whose_design_is_an_adr_gets_the_section_not_the_file(plan, app_map):
    """price_lookup and auctions have no spec of their own; the map names which
    ADR entries carry their design, by date."""
    win = app_map["windows"]["price_lookup"]
    rows = plan.reading_list(ROOT, app_map, win, "standard", "market chart")
    adr = [(p, why) for p, why, _ok in rows if p == "docs/decisions.md"]
    assert adr, "price_lookup's design ADRs vanished from the reading list"
    for _path, why in adr:
        start, end = map(int, re.search(r"lines (\d+)-(\d+)", why).groups())
        assert 0 < start < end
    log = (ROOT / "docs/decisions.md").read_text(encoding="utf-8").splitlines()
    spans = sum(
        int(re.search(r"lines (\d+)-(\d+)", why).group(2))
        - int(re.search(r"lines (\d+)-(\d+)", why).group(1))
        for _p, why in adr
    )
    assert spans < len(log) / 4, "the 'sections' cover most of the file"


def test_validate_rejects_an_unanchored_or_moved_adr_reference(plan, app_map):
    """The negative case for the anchor guard.

    Same discipline the rule registry already applies to this file: cite a
    heading, never the whole thing. Without the check, renaming an ADR heading
    silently drops a window's design note instead of failing.
    """
    import copy

    bad = copy.deepcopy(app_map)
    bad["windows"]["operations"]["specs"] = ["docs/decisions.md (M-Ops1)"]
    problems = plan.validate(ROOT, bad)
    assert any("no '§' anchor" in p for p in problems), problems

    moved = copy.deepcopy(app_map)
    moved["windows"]["operations"]["specs"] = [
        "docs/decisions.md § ### 2026-08-04 — A heading nobody ever wrote"
    ]
    problems = plan.validate(ROOT, moved)
    assert any("ADR heading not found" in p for p in problems), problems

    # And the real map is clean.
    assert not [p for p in plan.validate(ROOT, app_map) if "ADR" in p or "anchor" in p]


def test_scoped_rules_keeps_what_applies_and_drops_what_does_not(plan):
    """The whole value is the scoping. A rules block that returns everything is
    the log's problem wearing a different hat."""
    ui_model = ["src/tcginventory/ui/models/inventory_model.py"]
    service = ["src/tcginventory/services/recognizer.py"]

    in_ui, _ = plan.scoped_rules(ROOT, ui_model)
    in_svc, _ = plan.scoped_rules(ROOT, service)
    ids = lambda by_lane: {r["id"] for rules in by_lane.values() for r in rules}  # noqa: E731

    # QT-MODEL-SORT-ROLE is scoped src/tcginventory/ui/models/**/*.py
    assert "QT-MODEL-SORT-ROLE" in ids(in_ui)
    assert "QT-MODEL-SORT-ROLE" not in ids(in_svc)
    # QT-FREE-CORE binds the service layer and must survive the narrowing
    assert "QT-FREE-CORE" in ids(in_svc)


def test_scoped_rules_never_offers_the_registrys_own_escape_hatch(plan):
    """UNENUMERATED is scoped `**` and would match every path. It is a licence
    for a critic to raise something novel, not a rule anyone can violate."""
    by_lane, _ = plan.scoped_rules(ROOT, ["src/tcginventory/services/pricing.py"])
    assert "UNENUMERATED" not in {r["id"] for rs in by_lane.values() for r in rs}


def test_a_path_no_rule_claims_is_reported_not_passed_over(plan):
    """An empty rules block and 'the registry does not cover this' look identical
    to a reader and mean opposite things."""
    _by_lane, unclaimed = plan.scoped_rules(ROOT, ["docs/user-guide.md"])
    assert unclaimed == ["docs/user-guide.md"]


def test_a_missing_registry_stops_the_run_rather_than_printing_no_rules(plan, tmp_path):
    """The one way this change can be dangerously wrong: a dossier that silently
    prints no rules reads as 'nothing binds here'."""
    with pytest.raises(SystemExit, match="rule registry is missing"):
        plan.load_rules(tmp_path)

    (tmp_path / ".claude" / "context").mkdir(parents=True)
    (tmp_path / plan.RULES_REL).write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit, match="not valid JSON"):
        plan.load_rules(tmp_path)


def test_the_dossier_names_the_rules_and_how_to_read_their_reasoning(plan, app_map):
    out = plan.render(ROOT, app_map, "inventory", "add a column", "standard", "inv")
    assert "## Rules in scope" in out
    assert "MONEY-CENTS" in out  # named by id, so it can be looked up
    assert "rules.py show" in out  # and the log stays reachable by anchor
