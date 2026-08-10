"""The critics' rule registry must keep matching the documents it cites.

`.claude/context/rules.json` is the closed set of rules the review panel may
judge against — a finding that cites anything else is inadmissible. That is only
worth something if the registry cannot quietly drift from the specs it points at:
a rule whose source section has been rewritten is a rule the panel would enforce
from memory.

These tests make that drift a red suite, the same way `tests/test_app_map.py`
does for the window map, and they reuse the registry's own validator so the check
an agent runs and the check the suite runs cannot diverge.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".claude/hooks/rules.py"
RULES = ROOT / ".claude/context/rules.json"
SCHEMA = ROOT / ".claude/context/verdict.schema.json"


@pytest.fixture(scope="module")
def rules_module():
    spec = importlib.util.spec_from_file_location("_rules_tool", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_rules_tool"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def registry() -> dict:
    return json.loads(RULES.read_text(encoding="utf-8"))


def test_registry_and_schema_are_valid_json(registry):
    assert registry["rules"], "the registry is empty"
    json.loads(SCHEMA.read_text(encoding="utf-8"))


def test_every_source_anchor_and_seal_still_resolves(rules_module, capsys):
    """The whole point: a spec edit that changes what a rule means fails here.

    When this goes red, read the cited section and decide whether the rule still
    holds — then reseal deliberately with `rules.py seal --id <ID>`. Resealing to
    silence the failure defeats the mechanism.
    """
    code = rules_module.cmd_check(None)
    assert code == 0, capsys.readouterr().out


def test_rule_ids_are_unique_and_well_formed(registry):
    ids = [rule["id"] for rule in registry["rules"]]
    assert len(ids) == len(set(ids)), "duplicate rule id"
    for rule_id in ids:
        assert rule_id.replace("-", "").isalnum() and rule_id.isupper(), rule_id


def test_every_lane_maps_to_an_agent_that_exists(registry):
    for lane, agent in registry["lanes"].items():
        assert (ROOT / ".claude/agents" / f"{agent}.md").is_file(), lane


def test_every_rule_lane_is_declared(registry):
    known = set(registry["lanes"]) | {"any"}
    for rule in registry["rules"]:
        assert rule["lane"] in known, rule["id"]


def test_unenumerated_can_never_block(registry):
    """The escape hatch stays an escape hatch. A critic may raise something the
    registry does not cover, but it must not be able to stop a merge on it."""
    rule = next(r for r in registry["rules"] if r["id"] == "UNENUMERATED")
    assert rule["severity_max"] == "revise"


def test_unenumerated_does_not_cite_the_registry_itself(registry):
    """Sealing a rule that cites rules.json rewrites the file it just hashed, so
    the seal can never settle. Caught the first time this was built."""
    rule = next(r for r in registry["rules"] if r["id"] == "UNENUMERATED")
    assert rule["source"]["file"] != ".claude/context/rules.json"


@pytest.mark.parametrize(
    "path, pattern, expected",
    [
        # `**` must span ZERO directories as well as many. Both of these were
        # False under plain fnmatch, so the rules citing them never dispatched.
        ("src/tcginventory/__init__.py", "src/tcginventory/**/*.py", True),
        ("src/tcginventory/ui/theme.py", "src/tcginventory/ui/**/*.py", True),
        ("src/tcginventory/config.py", "src/tcginventory/**/*.py", True),
        # ... and many.
        ("src/tcginventory/services/inventory.py", "src/tcginventory/**/*.py", True),
        ("src/tcginventory/ui/models/x.py", "src/tcginventory/ui/**/*.py", True),
        # `*` must NOT cross a directory boundary.
        ("src/tcginventory/ui/theme.py", "src/tcginventory/*.py", False),
        # Wrong subtree, wrong extension, wrong root.
        ("src/tcginventory/services/x.py", "src/tcginventory/ui/**/*.py", False),
        ("src/tcginventory/ui/theme.qss", "src/tcginventory/ui/**/*.py", False),
        ("docs/decisions.md", "src/tcginventory/**/*.py", False),
        # Exact paths and directory scopes still work.
        (
            "src/tcginventory/services/allocation.py",
            "src/tcginventory/services/allocation.py",
            True,
        ),
        (
            "src/tcginventory/data/migrations/0020_x.sql",
            "src/tcginventory/data/migrations/**",
            True,
        ),
        ("anything/at/all.py", "**", True),
    ],
)
def test_scope_globbing(rules_module, path, pattern, expected):
    """A rule's scope decides whether its critic is dispatched at all, so a
    matching bug fails open and fails silently."""
    assert rules_module.path_matches(path, pattern) is expected


def test_repeated_globstars_do_not_blow_up(rules_module):
    """Consecutive `**` used to branch combinatorially — 7.86s at eight in a row.

    Every critic shells out to `rules.py scope` before judging a diff, so a
    doubled `**` in a rule's scope would hang the whole review gate with no error
    saying why. Folding runs of `**` into one makes that impossible.
    """
    path = "/".join(f"d{i}" for i in range(25)) + "/x.py"
    pathological = "/".join(["**"] * 12) + "/*.md"  # deliberately non-matching

    start = time.perf_counter()
    assert rules_module.path_matches(path, pathological) is False
    assert time.perf_counter() - start < 1.0, "repeated ** is branching again"

    # Folding must not change what matches.
    assert rules_module.path_matches("a/b/c.py", "a/**/**/c.py") is True
    assert rules_module.path_matches("a/c.py", "a/**/**/c.py") is True
    assert rules_module._collapse(["a", "**", "**", "**", "b"]) == ["a", "**", "b"]


def test_every_rule_scope_matches_its_own_source(rules_module, registry):
    """A rule whose scope cannot match the file it cites would never fire on a
    change to that file — exactly how THEME-TOKENS was broken."""
    for rule in registry["rules"]:
        source = rule["source"]["file"]
        if not source.startswith("src/"):
            continue  # rules sourced from docs or specs govern code elsewhere
        assert any(
            rules_module.path_matches(source, pattern) for pattern in rule["scope"]
        ), f"{rule['id']}: scope {rule['scope']} cannot match its source {source}"


def test_append_only_sources_are_anchored_at_a_heading(rules_module, registry):
    """A rule sourced from `docs/decisions.md` must cite a heading, not the file.

    `anchor: null` seals everything, so every unrelated ADR breaks the seal and
    gets resealed — which is how "never reseal to make a failure go away" decays
    into reflex. Two rules were shaped that way and both fired on ADRs that had
    nothing to do with them before this was enforced.
    """
    assert rules_module.anchor_problems(registry) == []


def test_an_unanchored_append_only_source_is_rejected(rules_module):
    """The negative case for the guard above — proving it can actually fail."""
    bad = {
        "rules": [
            {
                "id": "SOME-RULE",
                "source": {"file": "docs/decisions.md", "anchor": None},
            }
        ]
    }
    problems = rules_module.anchor_problems(bad)
    assert len(problems) == 1 and "SOME-RULE" in problems[0]

    # A heading anchor on the same file is fine, and so is a null anchor on a
    # file that is not append-only — `conftest.py` embodies its rule wholesale.
    bad["rules"][0]["source"]["anchor"] = "### 2026-08-07 — Something"
    assert rules_module.anchor_problems(bad) == []
    assert (
        rules_module.anchor_problems(
            {
                "rules": [
                    {"id": "X", "source": {"file": "tests/conftest.py", "anchor": None}}
                ]
            }
        )
        == []
    )


def test_every_numbered_invariant_has_its_own_rule(rules_module, registry):
    """The registry must name the invariant that broke, not gesture at the section.

    `INVARIANTS-DOC` was one catch-all over nine distinct invariants: a finding
    citing it said almost nothing about which one broke. If the spec grows a tenth
    invariant, this goes red rather than letting it sit unenumerated.
    """
    spec = (ROOT / "specs/02-data-model.md").read_text(encoding="utf-8")
    section = spec.split("## Invariants (must always hold)")[1].split("\n## ")[0]
    numbered = [
        line for line in section.splitlines() if re.match(r"^\d+\. ", line.strip())
    ]
    inv_rules = [r for r in registry["rules"] if r["id"].startswith("INV-")]
    assert len(inv_rules) == len(numbered), (
        f"{len(numbered)} numbered invariants in the spec but {len(inv_rules)} "
        f"INV-* rules in the registry"
    )
    assert not any(r["id"] == "INVARIANTS-DOC" for r in registry["rules"])


def _write(tmp_path: Path, payload: dict) -> str:
    target = tmp_path / "verdict.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return str(target)


def test_validator_accepts_a_well_formed_verdict(rules_module, tmp_path):
    args = type(
        "A",
        (),
        {
            "file": _write(
                tmp_path,
                {
                    "lane": "invariants",
                    "verdict": "block",
                    "findings": [
                        {
                            "rule": "MONEY-CENTS",
                            "file": "src/tcginventory/services/x.py",
                            "line": 12,
                            "evidence": "basis divided by 100.0, making a float",
                            "severity": "block",
                        }
                    ],
                },
            )
        },
    )()
    assert rules_module.cmd_validate(args) == 0


@pytest.mark.parametrize(
    "payload, why",
    [
        (
            {
                "lane": "invariants",
                "verdict": "block",
                "findings": [
                    {
                        "rule": "NOT-A-REAL-RULE",
                        "file": "a.py",
                        "line": 1,
                        "evidence": "x",
                        "severity": "block",
                    }
                ],
            },
            "an invented rule id must be rejected",
        ),
        (
            {
                "lane": "ui",
                "verdict": "block",
                "findings": [
                    {
                        "rule": "PREVIEW-HOVER",
                        "file": "a.py",
                        "line": 1,
                        "evidence": "x",
                        "severity": "block",
                    }
                ],
            },
            "a revise-capped rule must not be able to block",
        ),
        (
            {
                "lane": "risk",
                "verdict": "revise",
                "findings": [
                    {
                        "rule": "UNENUMERATED",
                        "file": "a.py",
                        "line": 1,
                        "evidence": "x",
                        "severity": "revise",
                    }
                ],
            },
            "UNENUMERATED without suggested_rule must be rejected",
        ),
        (
            {
                "lane": "test",
                "verdict": "block",
                "findings": [
                    {
                        "rule": "TESTS-HEADLESS",
                        "file": "a.py",
                        "line": 1,
                        "evidence": "x",
                        "severity": "revise",
                    }
                ],
            },
            "a block verdict needs a blocking finding",
        ),
        (
            {
                "lane": "test",
                "verdict": "pass",
                "findings": [
                    {
                        "rule": "TESTS-HEADLESS",
                        "file": "a.py",
                        "line": 1,
                        "evidence": "x",
                        "severity": "revise",
                    }
                ],
            },
            "a pass verdict must carry no findings",
        ),
        (
            {
                "lane": "test",
                "verdict": "revise",
                "findings": [
                    {
                        "rule": "TESTS-HEADLESS",
                        "file": "a.py",
                        "line": 1,
                        "evidence": "   ",
                        "severity": "revise",
                    }
                ],
            },
            "empty evidence must be rejected",
        ),
        (
            {
                "lane": "test",
                "verdict": "revise",
                "findings": [
                    {
                        "rule": "TESTS-HEADLESS",
                        "file": "a.py",
                        "evidence": "x",
                        "severity": "revise",
                    }
                ],
            },
            "a missing required field must be rejected",
        ),
        (
            {"lane": "vibes", "verdict": "pass", "findings": []},
            "an unknown lane must be rejected",
        ),
    ],
)
def test_validator_rejects_bad_verdicts(rules_module, tmp_path, payload, why):
    args = type("A", (), {"file": _write(tmp_path, payload)})()
    assert rules_module.cmd_validate(args) == 1, why
