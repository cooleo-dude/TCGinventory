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
import sys
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
