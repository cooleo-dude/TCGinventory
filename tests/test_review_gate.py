"""The review gate must open only for a review of *this* work, by *this* session.

There was no test for `.claude/hooks/review_gate.py` at all until this file: the
gate was verified by hand with piped-JSON cases that lived only in a transcript,
which is how three separate holes in it survived long enough to become ticket 003.
Ticket 003's plan step 1 asks for exactly this file as a regression floor, and
inherits it — the sha/PR-ref binding it specifies is still open work.

What is covered here is ticket 007's half: the gate resolves the repository from
the **caller's directory** rather than from the hook file's location, and a verdict
is bound to the session that recorded it.

Everything runs against a throwaway git repo with the registry redirected through
`TCGI_SESSION_REGISTRY`. The suite runs `-n auto --dist loadfile`, so nothing here
may touch the real `.claude/context/reviews/`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".claude/hooks"
GATE = HOOKS / "review_gate.py"


def _load(name: str):
    sys.path.insert(0, str(HOOKS))
    spec = importlib.util.spec_from_file_location(f"_gate_{name}", HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"_gate_{name}"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load("review_gate")


@pytest.fixture(scope="module")
def session_mod():
    return _load("_session")


@pytest.fixture
def registry(tmp_path, monkeypatch) -> Path:
    target = tmp_path / "registry"
    monkeypatch.setenv("TCGI_SESSION_REGISTRY", str(target))
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def repo(tmp_path) -> Path:
    tree = tmp_path / "tree"
    tree.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", *args], cwd=tree, check=True, capture_output=True)
    (tree / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(
        ["git", "add", "seed.txt"], cwd=tree, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "seed"], cwd=tree, check=True, capture_output=True
    )
    return tree


def _sha(repo: Path) -> str:
    done = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _record(
    repo: Path,
    session: str | None,
    verdicts: dict[str, str],
    holders: list[str] | None = None,
) -> Path:
    """Write a verdict artifact directly, the shape `cmd_record` produces."""
    reviews = repo / ".claude" / "context" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    path = reviews / f"{_sha(repo)}.json"
    path.write_text(
        json.dumps(
            {
                "sha": _sha(repo),
                "scope": "ticket-close",
                "ref": "7",
                "at": "2026-08-07T00:00:00+00:00",
                "verdicts": verdicts,
                "findings": [],
                "session": session,
                "holders": holders
                if holders is not None
                else ([session] if session else []),
                "branch": "main",
                "worktree": str(repo),
            }
        ),
        encoding="utf-8",
    )
    return path


GREEN = {"invariants": "pass", "risk": "pass", "test": "pass"}


def _run_hook(repo: Path, registry: Path, session: str | None, command: str):
    payload = {
        "tool_name": "Bash",
        "cwd": str(repo),
        "tool_input": {"command": command},
    }
    if session:
        payload["session_id"] = session
    return subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "TCGI_SESSION_REGISTRY": str(registry)},
        check=False,
    )


CLOSE = "python .claude/skills/sdd-ticket/scripts/ticket.py close 7"
MERGE = "gh pr merge 12 --squash"


# ---- what the gate guards ---------------------------------------------------


@pytest.mark.parametrize(
    "command, scope",
    [
        (MERGE, "pr-merge"),
        ("gh pr merge 12", "pr-merge"),
        (CLOSE, "ticket-close"),
        ("./.venv/Scripts/python.exe .../ticket.py close 3", "ticket-close"),
    ],
)
def test_guarded_commands_are_recognised(gate, command, scope):
    assert gate.guarded_scope(command) == scope


@pytest.mark.parametrize(
    "command",
    [
        "gh pr list",
        "gh pr view 12",
        "ticket.py list",
        "ticket.py push 7",
        "git status",
        "./.venv/Scripts/python.exe -m pytest -q",
    ],
)
def test_unguarded_commands_pass_straight_through(gate, command):
    assert gate.guarded_scope(command) is None


# ---- repo resolution (ticket 007, hole 3) ----------------------------------


def test_the_repo_comes_from_the_callers_directory(gate, repo):
    """The fix that has to land before worktrees are adopted.

    `settings.json` invokes the hook through `$CLAUDE_PROJECT_DIR`, which stays at
    the launch directory when a session moves into a worktree. Deriving the repo
    from `__file__` read the main clone's HEAD while the agent worked elsewhere.
    """
    assert gate.repo_root(str(repo)).resolve() == repo.resolve()


def test_repo_resolution_falls_back_when_the_directory_is_not_a_repo(gate, tmp_path):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    assert gate.repo_root(str(outside)) == gate.FALLBACK_REPO


def test_head_and_branch_are_read_from_the_given_tree(gate, repo):
    assert gate.head(repo) == _sha(repo)
    assert gate.branch_of(repo) == "main"


# ---- no review on record ----------------------------------------------------


def test_blocks_when_nothing_is_recorded(repo, registry):
    done = _run_hook(repo, registry, "session-a", CLOSE)
    assert done.returncode == 2
    assert "has no review on record" in done.stderr


def test_opens_for_the_session_that_recorded_it(repo, registry):
    _record(repo, "session-a", GREEN)
    done = _run_hook(repo, registry, "session-a", CLOSE)
    assert done.returncode == 0, done.stderr


def test_a_block_verdict_stops_the_gate(repo, registry):
    _record(repo, "session-a", {**GREEN, "risk": "block"})
    done = _run_hook(repo, registry, "session-a", CLOSE)
    assert done.returncode == 2
    assert "returned `block` on risk" in done.stderr


def test_revise_is_a_note_not_a_failure(repo, registry):
    """`revise` is answered, not obeyed — it must not stop the gate."""
    _record(repo, "session-a", {**GREEN, "test": "revise"})
    done = _run_hook(repo, registry, "session-a", CLOSE)
    assert done.returncode == 0, done.stderr


# ---- session binding (ticket 007, hole 2) ----------------------------------


def test_another_sessions_verdict_does_not_open_the_gate(repo, registry):
    """The hole this closes. Two sessions on the same sha — both just branched from
    main, neither has committed — otherwise trade verdicts: A records green for its
    own diff and B's unreviewed work closes a ticket on the strength of it."""
    _record(repo, "session-a", GREEN)
    done = _run_hook(repo, registry, "session-b", CLOSE)
    assert done.returncode == 2
    assert "not by this one" in done.stderr
    assert "session-" in done.stderr


def test_an_unattributed_verdict_is_still_admissible(repo, registry):
    """A verdict recorded before the lease registry existed carries no session. The
    guarantee is weaker and `session: null` says so in the artifact — but refusing
    it would brick every gate call on a machine where the registry is not running."""
    _record(repo, None, GREEN)
    done = _run_hook(repo, registry, "session-b", CLOSE)
    assert done.returncode == 0, done.stderr


def test_a_verdict_recorded_under_shared_ownership_opens_for_nobody(repo, registry):
    """The hole the risk critic found, and the reason `holders` exists.

    With two sessions on one worktree no single one owns it, so `owning_session`
    returns `None` and the artifact records `session: null`. Read as the legacy
    "no registry" null, that null is admissible — by *either* session — which
    reinstates exactly the verdict-trading this binding was added to stop. It has
    to fail closed, and be distinguishable from a genuinely unattributed verdict.
    """
    _record(repo, None, GREEN, holders=["session-a", "session-b"])
    for caller in ("session-a", "session-b", "session-c"):
        done = _run_hook(repo, registry, caller, CLOSE)
        assert done.returncode == 2, f"{caller} was let through: {done.stdout}"
        assert "cannot be attributed" in done.stderr


def test_record_marks_a_verdict_unusable_when_the_tree_is_shared(
    repo, registry, session_mod
):
    """End to end: recording while sharing a tree produces an artifact that names
    both holders and opens for neither."""
    session_mod.claim(repo, "session-a")
    session_mod.claim(repo, "session-b")
    done = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "record",
            "--scope",
            "ticket-close",
            "--ref",
            "7",
            "--verdict",
            "invariants=pass",
            "--verdict",
            "risk=pass",
            "--verdict",
            "test=pass",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "TCGI_SESSION_REGISTRY": str(registry)},
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "UNUSABLE" in done.stdout, done.stdout

    record = json.loads(
        (repo / ".claude" / "context" / "reviews" / f"{_sha(repo)}.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["session"] is None
    assert sorted(record["holders"]) == ["session-a", "session-b"]
    assert _run_hook(repo, registry, "session-a", CLOSE).returncode == 2


def test_a_caller_without_a_session_id_is_not_second_guessed(repo, registry):
    _record(repo, "session-a", GREEN)
    done = _run_hook(repo, registry, None, CLOSE)
    assert done.returncode == 0, done.stderr


def test_an_unguarded_command_is_never_session_checked(repo, registry):
    _record(repo, "session-a", GREEN)
    done = _run_hook(repo, registry, "session-b", "git status --short")
    assert done.returncode == 0, done.stderr


# ---- failing open on confusion ---------------------------------------------


def test_fails_open_on_unparseable_input():
    done = subprocess.run(
        [sys.executable, str(GATE)],
        input="not json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0


def test_owning_session_degrades_to_none_without_a_registry(gate, repo, monkeypatch):
    """`owning_session` imports the registry lazily and defensively: this runs on
    every guarded call, and an ImportError at module scope would stop all work."""
    monkeypatch.setenv("TCGI_SESSION_REGISTRY", str(repo / "nope"))
    assert gate.owning_session(repo) is None


# ---- record round-trip ------------------------------------------------------


def test_record_stamps_the_session_that_owns_the_worktree(
    repo, registry, session_mod, monkeypatch
):
    session_mod.claim(repo, "session-a")
    monkeypatch.chdir(repo)
    done = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "record",
            "--scope",
            "ticket-close",
            "--ref",
            "7",
            "--verdict",
            "invariants=pass",
            "--verdict",
            "risk=pass",
            "--verdict",
            "test=pass",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "TCGI_SESSION_REGISTRY": str(registry)},
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr

    artifact = repo / ".claude" / "context" / "reviews" / f"{_sha(repo)}.json"
    record = json.loads(artifact.read_text(encoding="utf-8"))
    assert record["session"] == "session-a"
    assert record["branch"] == "main"
    assert record["verdicts"] == GREEN

    # ...and that recorded verdict opens the gate for its own session only.
    assert _run_hook(repo, registry, "session-a", CLOSE).returncode == 0
    assert _run_hook(repo, registry, "session-b", CLOSE).returncode == 2


def test_record_refuses_an_incomplete_panel(repo, registry):
    done = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "record",
            "--scope",
            "ticket-close",
            "--ref",
            "7",
            "--verdict",
            "invariants=pass",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "TCGI_SESSION_REGISTRY": str(registry)},
        check=False,
    )
    assert done.returncode != 0
    assert "missing verdict for" in done.stderr


def test_record_rejects_an_invalid_verdict_value(repo, registry):
    done = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "record",
            "--scope",
            "ticket-close",
            "--ref",
            "7",
            "--verdict",
            "invariants=looks-fine",
            "--verdict",
            "risk=pass",
            "--verdict",
            "test=pass",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "TCGI_SESSION_REGISTRY": str(registry)},
        check=False,
    )
    assert done.returncode != 0
    assert "must be one of" in done.stderr
