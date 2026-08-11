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
    """Point the gate at a scratch reviews directory and a fixed HEAD.

    Every one of these takes a `repo` now: which checkout a call is about comes
    from the caller's directory rather than this file's location, so that several
    sessions can hold worktrees of the same repository at once (ticket 008). The
    lease lookups are stubbed too — left live they would read the real registry and
    stamp whichever session happens to be running the suite into the artifact.
    """
    monkeypatch.setattr(gate, "reviews_dir", lambda repo: tmp_path)
    monkeypatch.setattr(gate, "head", lambda repo=None: "cafe123")
    monkeypatch.setattr(gate, "branch_of", lambda repo=None: "ticket/008")
    monkeypatch.setattr(gate, "owning_session", lambda repo=None: "session-a")
    monkeypatch.setattr(gate, "lease_holders", lambda repo=None: ["session-a"])
    # `pr_head` shells out to `gh`, so it must be stubbed or the suite would depend
    # on the network and on whoever is authenticated. `None` is the documented
    # offline fallback (ADR 2026-08-08): bind to local HEAD and name the weakness.
    # Tests that want the strong binding override this explicitly.
    monkeypatch.setattr(gate, "pr_head", lambda repo, ref: None)
    return tmp_path


def _stdin(monkeypatch, command: str) -> None:
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        ),
    )


def _args(**over):
    base = dict(ref="6", verdict=[], findings=[], inherit=None, paths=[])
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
        (gate.reviews_dir(None) / f"{gate.head(None)}.json").read_text(encoding="utf-8")
    )


GREEN = ["invariants=pass", "risk=pass", "test=pass"]


# -- what the gate guards ----------------------------------------------------


@pytest.mark.parametrize(
    "command, expected",
    [
        # The ref is what binds a verdict to the thing being merged, so it has to
        # come out of the right argv position in every shape gh accepts.
        ("gh pr merge 6 --squash", ("land", "6")),
        ("gh pr merge --squash 6", ("land", "6")),
        ("gh.exe pr merge 26 --squash --delete-branch", ("land", "26")),
        ("gh pr merge", ("land", None)),
        (
            "python .claude/skills/sdd-ticket/scripts/ticket.py close 3",
            ("land", "3"),
        ),
        (
            ".venv/Scripts/python.exe .claude/skills/sdd-ticket/scripts/ticket.py"
            " close 12",
            ("land", "12"),
        ),
        # A flag's *value* is not the ref. `--subject "close 9"` used to be the kind
        # of thing a substring search would read as a close of ticket 9.
        ('gh pr merge --subject "close 9" 6', ("land", "6")),
        ("git status", None),
        ("gh pr view 6 --json state", None),
        # Chained commands: the merge is still found after a separator.
        ("git add x && gh pr merge 6", ("land", "6")),
    ],
)
def test_guarded_reads_the_scope_and_ref_from_argv(gate, command, expected):
    assert gate.guarded(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        # THE false positive, from ticket 003: a `gh pr create` whose body prose
        # mentions merging. Nothing is being merged. The old pattern was
        # `\bgh\b[^\n]*\bpr\b[^\n]*\bmerge\b` over the raw string, so this blocked —
        # and the workaround was `--body-file`, i.e. a guard teaching people to
        # route around it.
        'gh pr create --title "x" --body "opens the gate for merging any other PR"',
        'gh pr create --body "this closes the hole where ticket.py close bypassed it"',
        # A merge named inside quoted prose is prose, not an invocation.
        'echo "gh pr merge 6"',
        'git commit -m "gh pr merge 6 was blocked here"',
    ],
)
def test_prose_that_mentions_merging_is_not_a_merge(gate, command):
    assert gate.guarded(command) is None


def test_an_untokenisable_command_is_not_judged(gate):
    """Fail open on our own confusion. An unbalanced quote reaches this hook on
    every shell call; raising here would stop all work in the session."""
    assert gate.guarded('gh pr merge 6 --body "unbalanced') is None


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


def test_an_unreadable_registry_demands_the_full_panel(gate, reviews):
    """Fail closed: if scope cannot be computed, assume every lane is live.

    The repo is passed rather than patched onto the module — there is no `REPO`
    constant any more, because which checkout a call is about comes from the
    caller's directory (ticket 008).
    """
    assert gate.out_of_scope(["docs/decisions.md"], repo=reviews) == set()


# -- --inherit ---------------------------------------------------------------


def test_inherit_carries_the_lanes_that_were_not_rejudged(gate, reviews, monkeypatch):
    _record(gate, verdict=[*GREEN, "ui=pass"])  # the first, full panel
    monkeypatch.setattr(gate, "head", lambda repo=None: "beef456")  # a new commit

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
    monkeypatch.setattr(gate, "head", lambda repo=None: "beef456")

    record = _record(gate, expect=1, verdict=["risk=block"], inherit="cafe123")
    assert record["verdicts"]["risk"] == "block"  # not the inherited `pass`


def test_inherit_refuses_to_cross_pull_requests(gate, reviews, monkeypatch):
    """The hole this must not open: a green panel for one PR greening another."""
    _record(gate, verdict=GREEN, ref="6")
    monkeypatch.setattr(gate, "head", lambda repo=None: "beef456")

    with pytest.raises(SystemExit, match="only be carried forward within one MR"):
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
    monkeypatch.setattr(gate, "head", lambda repo=None: "beef456")

    with pytest.raises(SystemExit, match="missing verdict"):
        gate.cmd_record(_args(verdict=["risk=pass"], inherit="cafe123"))


# -- which checkout a call is about (ticket 008) ------------------------------


@pytest.fixture()
def tree(tmp_path):
    """A throwaway git repo, so repo resolution has something real to resolve."""
    import subprocess

    root = tmp_path / "tree"
    root.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "seed"], cwd=root, check=True, capture_output=True
    )
    return root


def test_the_repo_comes_from_the_callers_directory(gate, tree):
    """The fix that had to land before worktrees were adopted.

    `settings.json` invokes this hook through `$CLAUDE_PROJECT_DIR`, which stays at
    the launch directory when a session moves into a worktree. Resolving the repo
    from `__file__` therefore read the *main clone's* HEAD while the agent worked
    somewhere else entirely — attesting to a commit nobody was merging.
    """
    assert gate.repo_root(str(tree)).resolve() == tree.resolve()


def test_repo_resolution_falls_back_when_the_directory_is_not_a_repo(gate, tmp_path):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    assert gate.repo_root(str(outside)) == gate.FALLBACK_REPO


def test_head_and_branch_are_read_from_the_given_tree(gate, tree):
    assert gate.head(tree) != "unknown"
    assert gate.branch_of(tree) == "main"


def test_a_verdict_records_whose_panel_it_was(gate, reviews):
    """Attribution, not enforcement.

    The gate stops on a `block` and nothing else, so this does not gate anything —
    it answers "whose review was this?" when two sessions share a checkout and
    somebody reads the artifact months later.
    """
    record = _record(gate, verdict=GREEN)
    assert record["session"] == "session-a"
    assert record["holders"] == ["session-a"]
    assert record["branch"] == "ticket/008"


def test_an_unattributable_verdict_still_opens_the_gate(gate, reviews, monkeypatch):
    """The behaviour that changed when the panel became opt-in.

    Ticket 008 first blocked a verdict that could not be attributed to the calling
    session. Under the opt-in gate a *missing* verdict merges cleanly, so blocking
    on an unattributable `pass` would make a reviewed commit harder to merge than
    an unreviewed one. Only `block` stops the gate.
    """
    monkeypatch.setattr(gate, "owning_session", lambda repo=None: None)
    monkeypatch.setattr(
        gate, "lease_holders", lambda repo=None: ["session-a", "session-b"]
    )
    record = _record(gate, verdict=GREEN)
    assert record["session"] is None
    assert record["holders"] == ["session-a", "session-b"]

    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "tool_name": "Bash",
                    "session_id": "session-c",
                    "tool_input": {"command": "gh pr merge 6"},
                }
            )
        ),
    )
    assert gate.hook() == 0


def test_owning_session_degrades_to_none_without_a_registry(gate, tree, monkeypatch):
    """`owning_session` imports the lease registry lazily and defensively: this runs
    on every guarded call, and an ImportError at module scope would stop all work."""
    monkeypatch.setenv("TCGI_SESSION_REGISTRY", str(tree / "nope"))
    assert gate.owning_session(tree) is None


# -- what `show` claims the gate will do -------------------------------------


def test_show_does_not_promise_a_block_it_will_not_deliver(gate, reviews, capsys):
    """`show` said "no review on record — the gate will block a merge or a close",
    and then the merge went through. The gate was right: `hook()` has admitted a
    missing panel since the critics became opt-in. `show` was never updated with it.

    A gate that overstates its own guarantee is worse than one that says nothing —
    it gets trusted for a promise it does not make (ticket 003, hole four). Asserted
    on the text because the text is the defect.
    """
    assert gate.cmd_show(SimpleNamespace()) == 0, "the normal state is not a failure"
    said = capsys.readouterr().out
    assert "will block" not in said, said
    assert "nothing will be blocked" in said


def test_show_names_the_lanes_that_would_block_a_merge(gate, reviews, capsys):
    """The converse: when a panel *did* block, `show` has to say so plainly. Listing
    lane verdicts without stating the consequence is what let the old wording sit
    unnoticed."""
    _record(gate, expect=1, verdict=["invariants=pass", "risk=block", "test=block"])
    capsys.readouterr()

    assert gate.cmd_show(SimpleNamespace()) == 0
    said = capsys.readouterr().out
    assert "BLOCKED on risk, test" in said, said


def test_show_says_a_clean_panel_proceeds(gate, reviews, capsys):
    _record(gate, verdict=GREEN)
    capsys.readouterr()

    assert gate.cmd_show(SimpleNamespace()) == 0
    said = capsys.readouterr().out
    assert "proceeds" in said
    assert "BLOCKED" not in said


# -- what the verdict is bound to (ticket 003) --------------------------------


def _artifact(reviews: Path, **over) -> dict:
    """A verdict on disk, keyed the way `cmd_record` keys one."""
    payload = {
        "sha": "branchtip",
        "scope": "land",
        "ref": "8",
        "binding": "pr-head",
        "head_branch": "ticket/008",
        "verdicts": {"invariants": "block", "risk": "pass", "test": "pass"},
    }
    payload.update(over)
    (reviews / f"{payload['sha']}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return payload


def test_a_block_is_not_bypassed_by_standing_somewhere_else(gate, reviews, monkeypatch):
    """The hole this ticket exists for, and the regression floor for the fix.

    Admission used to be keyed on the local `HEAD` — `verdict_path(repo, head(repo))`.
    A panel that blocked PR #8 on the branch tip was therefore invisible from any
    other directory, most obviously `main`: the lookup missed and the merge went
    through. Measured before the fix, same PR and same recorded block: exit 2 from
    the branch tip, exit 0 from main.
    """
    _artifact(reviews)
    # Standing on main; the PR's branch still points at the reviewed commit.
    monkeypatch.setattr(gate, "head", lambda repo=None: "mainsha")
    monkeypatch.setattr(gate, "branch_of", lambda repo=None: "main")
    monkeypatch.setattr(gate, "rev_parse", lambda repo, rev: "branchtip0000")
    _stdin(monkeypatch, "gh pr merge 8 --squash")
    assert gate.hook() == 2


def test_a_verdict_for_one_pr_does_not_answer_another(gate, reviews, monkeypatch):
    """`cmd_record` stored `ref` and `hook()` never read it, so admission ignored
    which PR a verdict was about. A block on #8 must not stop #9 either."""
    _artifact(reviews, ref="8")
    monkeypatch.setattr(gate, "head", lambda repo=None: "mainsha")
    monkeypatch.setattr(gate, "rev_parse", lambda repo, rev: "branchtip0000")
    _stdin(monkeypatch, "gh pr merge 9 --squash")
    assert gate.hook() == 0, "a verdict for PR #8 spoke for PR #9"


def test_a_new_commit_on_the_pr_supersedes_the_objection(gate, reviews, monkeypatch):
    """A verdict is bound to a commit so that pushing a fix clears the block: the
    panel judged something that is no longer what would merge. That predates this
    ticket, and the ticket's verification list asks for it explicitly."""
    _artifact(reviews)
    monkeypatch.setattr(gate, "head", lambda repo=None: "mainsha")
    monkeypatch.setattr(gate, "rev_parse", lambda repo, rev: "aNewerCommit")
    _stdin(monkeypatch, "gh pr merge 8 --squash")
    assert gate.hook() == 0


def test_an_unresolvable_branch_keeps_a_recorded_block(gate, reviews, monkeypatch):
    """The one place this file fails closed, deliberately.

    Supersession is decided offline, so the branch may not resolve at all — a fresh
    clone, a pruned local ref. Discarding a `block` because a lookup did not work
    would be the gate failing open at exactly the moment it is meant to hold.
    """
    _artifact(reviews)
    monkeypatch.setattr(gate, "head", lambda repo=None: "mainsha")
    monkeypatch.setattr(gate, "rev_parse", lambda repo, rev: None)
    _stdin(monkeypatch, "gh pr merge 8 --squash")
    assert gate.hook() == 2


def test_a_local_head_artifact_speaks_only_for_its_own_commit(
    gate, reviews, monkeypatch
):
    """The weak binding, named and contained (ADR 2026-08-08): it may block the
    commit it was recorded on and must not block any other — the case that passed
    silently before."""
    _artifact(reviews, binding="local-head", head_branch=None, sha="cafe123")
    _stdin(monkeypatch, "gh pr merge 8 --squash")
    assert gate.hook() == 2, "it must speak for the commit it was recorded on"

    monkeypatch.setattr(gate, "head", lambda repo=None: "movedon")
    _stdin(monkeypatch, "gh pr merge 8 --squash")
    assert gate.hook() == 0, "it must not speak for any other commit"


def test_record_binds_a_pr_merge_to_the_pr_head(gate, reviews, monkeypatch):
    monkeypatch.setattr(
        gate, "pr_head", lambda repo, ref: ("abc1234def5678", "ticket/003")
    )
    assert gate.cmd_record(_args(ref="26", verdict=GREEN)) == 0
    record = json.loads((reviews / "abc1234.json").read_text(encoding="utf-8"))
    assert record["binding"] == "pr-head"
    assert record["head_branch"] == "ticket/003"
    assert record["sha"] == "abc1234", "keyed on the PR head, not on local HEAD"
    assert not (reviews / "cafe123.json").exists()


def test_record_falls_back_to_local_head_when_gh_cannot_answer(gate, reviews):
    """Offline it records the weaker binding rather than refusing: the failure would
    land on someone who has just spent ~100k tokens on the panel, and a gate people
    disable is worth less than a weak one they keep."""
    record = _record(gate, verdict=GREEN)
    assert record["binding"] == "local-head"
    assert record["sha"] == "cafe123"


def test_a_ticket_close_binds_to_the_mr_head_like_everything_else(
    gate, reviews, monkeypatch
):
    """The special case that expired.

    `ticket-close` used to bind to local `HEAD`, on the reasoning that closing a
    ticket described work which had already landed. `close` now runs on the ticket's
    own branch in the commit *before* the merge, so the MR head is the right referent
    for both steps — and there is one binding rule instead of a carve-out.
    """
    monkeypatch.setattr(
        gate, "pr_head", lambda repo, ref: ("abc1234def5678", "mr-is-the-ticket")
    )
    assert gate.cmd_record(_args(ref="41", verdict=GREEN)) == 0
    record = json.loads((reviews / "abc1234.json").read_text(encoding="utf-8"))
    assert record["binding"] == "pr-head"
    assert record["sha"] == "abc1234", "still bound to the checkout"


def test_one_verdict_admits_both_the_close_and_the_merge(gate, reviews, monkeypatch):
    """The payoff of collapsing the referent. Landing a change is two commands, and
    a panel recorded once for MR 41 has to answer for both — under the old split
    scopes each needed its own recording, which is paying twice for one judgement."""
    _artifact(reviews, ref="41", sha="branchtip")
    monkeypatch.setattr(gate, "head", lambda repo=None: "mainsha")
    monkeypatch.setattr(gate, "rev_parse", lambda repo, rev: "branchtip0000")
    monkeypatch.setattr(gate, "ticket_mr", lambda repo: "41")

    _stdin(monkeypatch, "gh pr merge 41 --squash")
    assert gate.hook() == 2, "the merge ignored a block recorded for this MR"
    _stdin(monkeypatch, "python .claude/skills/sdd-ticket/scripts/ticket.py close")
    assert gate.hook() == 2, "the close ignored a block recorded for the same MR"


def test_a_close_with_no_number_resolves_the_mr_from_the_ticket_file(
    gate, monkeypatch, tmp_path
):
    """`close` takes no argument now, so the number has to come from the ticket on
    the branch. Reading it wrong is worse than not reading it: the gate would look up
    a verdict for something nobody asked about."""
    tickets = tmp_path / ".claude" / "context" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "041-mr-is-the-ticket.md").write_text(
        "# 41# — The merge request is the ticket\n\n```\nstatus:   in-progress\n"
        "mr:       41\n```\n",
        encoding="utf-8",
    )

    assert gate.ticket_mr(tmp_path) == "41"
    assert gate.guarded("python x/ticket.py close", tmp_path) == ("land", "41")


@pytest.mark.parametrize(
    "body, why",
    [
        ("# 41# — x\n\n```\nmr:       none\n```\n", "an unnumbered draft"),
        ("# 41# — x\n\n```\nstatus:   open\n```\n", "no mr field at all"),
        ("", "an empty file"),
    ],
)
def test_ticket_mr_returns_none_rather_than_guessing(gate, tmp_path, body, why):
    """Every failure path has to fall back to the weaker lookups instead of inventing
    a ref, and none of them may raise — this runs in a `PreToolUse` hook, where an
    exception stops every shell call in the session."""
    tickets = tmp_path / ".claude" / "context" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "041-thing.md").write_text(body, encoding="utf-8")

    assert gate.ticket_mr(tmp_path) is None, why


def test_ticket_mr_survives_a_missing_tickets_directory(gate, tmp_path):
    """`main` carries no ticket files at all now, so this is the ordinary case."""
    assert gate.ticket_mr(tmp_path) is None


def test_show_names_the_binding(gate, reviews, capsys):
    _record(gate, verdict=GREEN)
    capsys.readouterr()
    assert gate.cmd_show(SimpleNamespace()) == 0
    said = capsys.readouterr().out
    assert "local-head" in said and "no other" in said


def test_show_reports_a_verdict_that_is_not_on_local_head(gate, reviews, capsys):
    """`show` must not go quiet about the verdict that is about to block a merge:
    reporting only the local-HEAD artifact would reopen the drift step 0 closed.

    The first version of this function did list it and then printed "nothing will be
    blocked" underneath, which is why the consequence is asserted here too.
    """
    _artifact(reviews)
    capsys.readouterr()
    assert gate.cmd_show(SimpleNamespace()) == 0
    said = capsys.readouterr().out
    assert "8!" in said and "invariants   block" in said, said
    assert "a merge of 8! is BLOCKED on invariants" in said, said
    assert "nothing blocking" not in said, said


def test_same_commit_compares_by_prefix(gate):
    """`git rev-parse --short` abbreviates and `gh` returns the full oid. Comparing
    them for equality would mark every verdict superseded — the gate failing open
    while looking like it works."""
    assert gate.same_commit("abc1234", "abc1234def5678901234")
    assert gate.same_commit("abc1234def5678901234", "abc1234")
    assert not gate.same_commit("abc1234", "abd1234")
    assert not gate.same_commit("", "abc1234")
    assert not gate.same_commit("abc1234", None)
