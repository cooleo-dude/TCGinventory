"""The review gate has to fail closed, and `--inherit` must not become a loophole.

There were no tests for `review_gate.py` at all until this file: the gate was
verified by hand with piped JSON that lived only in a transcript. It is the thing
standing between an unreviewed diff and `main`, so it gets the same treatment as
the rule registry — including the negative cases, per `CHECKER-NEEDS-NEGATIVE-TEST`.

`--inherit` exists to stop the panel being re-run over lanes whose files did not
change. That saves most of the cost of a second round, and it would be worth
nothing if it let a verdict leak across pull requests.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".claude/hooks/review_gate.py"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("_review_gate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_review_gate"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def reviews(gate, tmp_path, monkeypatch):
    """Point the gate at a scratch reviews directory and a fixed HEAD."""
    monkeypatch.setattr(gate, "REVIEWS", tmp_path)
    monkeypatch.setattr(gate, "head", lambda: "cafe123")
    return tmp_path


def _args(**over):
    base = dict(
        scope="pr-merge", ref="6", verdict=[], findings=[], inherit=None, paths=[]
    )
    base.update(over)
    return SimpleNamespace(**base)


def _record(gate, expect: int = 0, **over) -> dict:
    """Record, then read back the artifact for whatever HEAD is now.

    `cmd_record` returns 1 when a lane blocks, so the expected code is explicit —
    and the artifact is keyed on the *current* head, not the one an earlier call
    happened to use.
    """
    assert gate.cmd_record(_args(**over)) == expect
    return json.loads(
        (gate.REVIEWS / f"{gate.head()}.json").read_text(encoding="utf-8")
    )


GREEN = ["invariants=pass", "risk=pass", "test=pass"]


# -- what the gate guards ----------------------------------------------------


@pytest.mark.parametrize(
    "command, scope",
    [
        ("gh pr merge 6 --squash", "pr-merge"),
        ("python .claude/skills/sdd-ticket/scripts/ticket.py close 3", "ticket-close"),
        ("git status", None),
        ("gh pr view 6 --json state", None),
    ],
)
def test_guarded_scope_matches_what_it_should(gate, command, scope):
    assert gate.guarded_scope(command) == scope


def test_no_panel_on_record_does_not_block(gate, reviews, monkeypatch):
    """The critics are opt-in — they cost ~26k tokens a spawn, so a merge with no
    panel is a normal merge, not a violation."""
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "gh pr merge 6"},
                }
            )
        ),
    )
    assert gate.hook() == 0


def test_a_panel_you_ran_cannot_be_ignored(gate, reviews, monkeypatch):
    """The half the gate still enforces: critics that were actually run and said
    `block` stop the merge. Opt-in to run them, not opt-in to obey them."""
    _record(gate, expect=1, verdict=["invariants=pass", "risk=block", "test=pass"])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "gh pr merge 6"},
                }
            )
        ),
    )
    assert gate.hook() == 2


def test_hook_opens_on_a_clear_verdict(gate, reviews, monkeypatch):
    _record(gate, verdict=GREEN)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "gh pr merge 6"},
                }
            )
        ),
    )
    assert gate.hook() == 0


def test_a_revise_does_not_block(gate, reviews):
    """`revise` is a note the author answers, not a failure."""
    record = _record(gate, verdict=["invariants=pass", "risk=revise", "test=revise"])
    assert record["verdicts"]["risk"] == "revise"


def test_every_required_lane_must_be_present(gate, reviews):
    with pytest.raises(SystemExit):
        gate.cmd_record(_args(verdict=["invariants=pass", "risk=pass"]))


# -- lanes the registry puts out of scope ------------------------------------


def test_a_docs_only_diff_needs_no_critic_at_all(gate, reviews):
    """The whole point: nothing in `.claude/` or `docs/` can violate a rule in
    any required lane, so the panel is `n/a` across the board and costs nothing.
    """
    record = _record(gate, paths=["docs/decisions.md", "CLAUDE.md"])
    assert record["verdicts"] == {
        "invariants": "n/a",
        "risk": "n/a",
        "test": "n/a",
    }


def test_a_lane_in_scope_still_demands_a_real_verdict(gate, reviews):
    """The negative case — `--paths` must not become a way to skip a live lane.

    `tests/**/*.py` is in scope for the test lane, so a diff touching it cannot
    be recorded without one.
    """
    with pytest.raises(SystemExit, match="missing verdict"):
        gate.cmd_record(_args(paths=["tests/test_ui_views.py"]))


def test_out_of_scope_never_overwrites_a_real_verdict(gate, reviews):
    record = _record(
        gate, expect=1, paths=["docs/decisions.md"], verdict=["risk=block"]
    )
    assert record["verdicts"]["risk"] == "block"  # judged, not n/a


def test_no_paths_means_the_full_panel_as_before(gate, reviews):
    """Omitting `--paths` must not weaken the gate."""
    with pytest.raises(SystemExit, match="missing verdict"):
        gate.cmd_record(_args(verdict=["invariants=pass"]))


def test_an_unreadable_registry_demands_the_full_panel(gate, reviews, monkeypatch):
    """Fail closed: if scope cannot be computed, assume every lane is live."""
    monkeypatch.setattr(gate, "REPO", reviews)  # no rules.py under here
    assert gate.out_of_scope(["docs/decisions.md"]) == set()


# -- --inherit ---------------------------------------------------------------


def test_inherit_carries_the_lanes_that_were_not_rejudged(gate, reviews, monkeypatch):
    _record(gate, verdict=[*GREEN, "ui=pass"])  # the first, full panel
    monkeypatch.setattr(gate, "head", lambda: "beef456")  # a new commit

    record = _record(gate, verdict=["risk=pass"], inherit="cafe123")
    assert record["verdicts"] == {
        "invariants": "pass",
        "risk": "pass",
        "test": "pass",
        "ui": "pass",
    }
    assert record["inherited"] == {
        "from": "cafe123",
        "lanes": ["invariants", "test", "ui"],
    }


def test_a_lane_judged_this_round_wins_over_the_inherited_one(
    gate, reviews, monkeypatch
):
    _record(gate, verdict=["invariants=pass", "risk=pass", "test=pass"])
    monkeypatch.setattr(gate, "head", lambda: "beef456")

    record = _record(gate, expect=1, verdict=["risk=block"], inherit="cafe123")
    assert record["verdicts"]["risk"] == "block"  # not the inherited `pass`


def test_inherit_refuses_to_cross_pull_requests(gate, reviews, monkeypatch):
    """The hole this must not open: a green panel for one PR greening another."""
    _record(gate, verdict=GREEN, ref="6")
    monkeypatch.setattr(gate, "head", lambda: "beef456")

    with pytest.raises(SystemExit, match="only be carried forward within one PR"):
        gate.cmd_record(_args(ref="9", verdict=["risk=pass"], inherit="cafe123"))


def test_inherit_refuses_a_sha_with_no_verdict(gate, reviews):
    with pytest.raises(SystemExit, match="no verdict on record"):
        gate.cmd_record(_args(verdict=["risk=pass"], inherit="0000000"))


def test_inherit_cannot_conjure_a_missing_lane(gate, reviews, monkeypatch):
    """Carrying forward a partial panel still has to satisfy every lane."""
    _record(gate, verdict=GREEN)
    (reviews / "cafe123.json").write_text(
        json.dumps({"ref": "6", "verdicts": {"invariants": "pass"}}), encoding="utf-8"
    )
    monkeypatch.setattr(gate, "head", lambda: "beef456")

    with pytest.raises(SystemExit, match="missing verdict"):
        gate.cmd_record(_args(verdict=["risk=pass"], inherit="cafe123"))
