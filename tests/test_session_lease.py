"""The branch guard must block the collision and nothing else.

`.claude/hooks/branch_lease.py` runs as a `PreToolUse` hook on **every** shell
call, which puts it in an unusual position for this repo: a false positive does not
produce a wrong number, it stops all work in the session. So the negative cases
here matter at least as much as the positive ones, per `CHECKER-NEEDS-NEGATIVE-TEST`
— every command the guard must leave alone is asserted explicitly.

The registry is redirected to `tmp_path` through `TCGI_SESSION_REGISTRY` in every
test. The suite runs `-n auto --dist loadfile`, so a test writing to the real
`.git/tcgi-sessions/` would fight both the other workers and whichever live session
is running it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".claude/hooks"
GUARD = HOOKS / "branch_lease.py"


def _load(name: str):
    """Import a hook script by path, with its own directory importable.

    The scripts do `import _session`, which works when they run as scripts (their
    directory is `sys.path[0]`) but not under `spec_from_file_location`.
    """
    sys.path.insert(0, str(HOOKS))
    spec = importlib.util.spec_from_file_location(f"_hook_{name}", HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"_hook_{name}"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    return _load("branch_lease")


@pytest.fixture(scope="module")
def session_mod():
    return _load("_session")


@pytest.fixture
def registry(tmp_path, monkeypatch) -> Path:
    target = tmp_path / "registry"
    monkeypatch.setenv("TCGI_SESSION_REGISTRY", str(target))
    return target


@pytest.fixture
def repo(tmp_path) -> Path:
    """A real throwaway git repo — the registry reads `HEAD` through git."""
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


# ---- tokenising -------------------------------------------------------------


def test_a_quoted_separator_is_not_a_separator(guard):
    """The defect ticket 003 recorded, in its other form: a `;` inside a commit
    message must not split the command, or the message tail is read as a command."""
    parts = guard.segments('git add x.py; git commit -m "fix; the thing" -- x.py')
    assert parts == [
        ["git", "add", "x.py"],
        ["git", "commit", "-m", "fix; the thing", "--", "x.py"],
    ]


@pytest.mark.parametrize("joiner", [";", "&&", "||", "|"])
def test_every_shell_separator_splits(guard, joiner):
    assert len(guard.segments(f"git status {joiner} git switch -c x")) == 2


def test_an_unbalanced_quote_yields_nothing_to_judge(guard):
    """Fail open: a command we cannot tokenise is one we must not block on."""
    assert guard.segments('git commit -m "unbalanced') == []
    assert guard.verdict('git commit -m "unbalanced') is None


# ---- what must be blocked ---------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git checkout main",
        "git checkout -b ticket/008",
        "git switch -c ticket/008",
        "git switch main",
        "git reset --hard origin/main",
        "git rebase main",
        "git merge main",
        "git cherry-pick abc1234",
        "git revert abc1234",
        # `pull` is fetch + merge (or rebase): it moves HEAD like the two above it,
        # and was missing from the first cut of the guarded set.
        "git pull",
        "git pull origin main",
        "git pull --rebase",
        "git apply patch.diff",
        "git stash",
        "git restore src/x.py",
        "git clean -fd",
        "git branch -D ticket/008",
        "git branch --force main abc1234",
        "git add -A",
        "git add .",
        "git add --all",
        "git add -u",
        "git add",
        "git commit -m 'sweeps their staged files'",
        "git commit -a -m 'theirs too'",
        "git commit --amend",
        # Buried in a chain, and reached through git's own path options.
        "./.venv/Scripts/ruff.exe check . ; git switch -c ticket/008",
        "git -C . checkout main",
        "git.exe add -A",
    ],
)
def test_blocked_beside_another_session(guard, command):
    assert guard.verdict(command) is not None, command


# ---- what must NOT be blocked ----------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # Reads. A guard that stops these stops the session.
        "git status --short",
        "git log --oneline -5",
        "git diff --name-only",
        "git show HEAD",
        "git rev-parse --short HEAD",
        "git branch -a",
        "git branch ticket/008",  # creating a branch does not move HEAD
        "git fetch origin",
        "git push -u origin ticket/007",
        # The remedy itself must always be reachable.
        "git worktree add --detach ../tcgi-x main",
        "git worktree list",
        # Explicit pathspecs are the documented safe form.
        "git add src/tcginventory/ui/views/history.py",
        "git commit -m 'mine only' -- src/x.py",
        # Reads wearing a destructive subcommand's name. Blocking these is the kind
        # of false positive that gets a guard switched off wholesale.
        "git stash list",
        "git stash show",
        "git bisect log",
        "git clean -n",
        "git clean --dry-run",
        "git apply --check patch.diff",
        "git apply --stat patch.diff",
        # Not git at all.
        "./.venv/Scripts/python.exe -m pytest -q",
        "gh pr create --body 'see the checkout notes and merge later'",
        "gh pr list",
        "echo git switch -c not-a-command",
    ],
)
def test_allowed_even_beside_another_session(guard, command):
    assert guard.verdict(command) is None, command


@pytest.mark.parametrize(
    "command",
    [
        r"cd C:\Users\colel\scratch && git commit -m init",
        "cd /tmp/elsewhere && git reset --hard",
        "pushd ../other-repo; git switch -c whatever",
        "Set-Location ../other; git merge main",
    ],
)
def test_a_chain_that_changes_directory_is_not_judged(guard, command):
    """A real false positive, hit while reviewing this guard: a chain that `cd`s
    into a scratch repo and commits there was blocked with "another session is
    working in this same checkout", which was false for that command and pointed at
    remedies that did not apply.

    Rivals are detected against the directory the tool call *starts* in, and the
    guard cannot tell where a `cd` went — posix tokenising eats Windows backslashes,
    so the target arrives mangled. Failing open is the same direction every other
    ambiguity here resolves.
    """
    assert guard.verdict(command) is None


def test_the_directory_change_bypass_is_known_and_bounded(guard):
    """The accepted cost of the above, asserted so it is a decision rather than a
    surprise: everything *before* the `cd` is still judged, so the escape only
    covers what genuinely might not be this checkout."""
    assert guard.verdict("git switch -c x && cd /tmp") is not None
    assert guard.verdict("cd . && git switch -c x") is None


def test_gh_pr_create_prose_is_not_a_git_command(guard):
    """Ticket 003's false positive, asserted here so this guard cannot repeat it:
    `review_gate.GUARDED` blocked a `gh pr create` because its *body text* said
    "merge". Tokenising makes the prose unreachable as a subcommand."""
    command = (
        'gh pr create --title "x" --body "git checkout main and git add -A are '
        'what this ticket is about; also git commit -a"'
    )
    assert guard.verdict(command) is None


# ---- the lease registry -----------------------------------------------------


def test_claim_is_idempotent_and_keeps_the_start_time(session_mod, repo, registry):
    first = session_mod.claim(repo, "session-a")
    time.sleep(0.01)
    second = session_mod.claim(repo, "session-a")
    assert second["started"] == first["started"]
    assert second["heartbeat"] > first["heartbeat"]
    assert len(session_mod.leases(repo)) == 1
    assert second["branch"] == "main"


def test_a_session_alone_has_no_rivals(session_mod, repo, registry):
    session_mod.claim(repo, "session-a")
    assert session_mod.rivals(repo, "session-a") == []
    assert session_mod.owning_session(repo) == "session-a"


def test_two_sessions_in_one_tree_are_rivals(session_mod, repo, registry):
    session_mod.claim(repo, "session-a")
    session_mod.claim(repo, "session-b")
    rivals = session_mod.rivals(repo, "session-a")
    assert [lease["session"] for lease in rivals] == ["session-b"]
    # Neither owns it, so a verdict recorded here cannot be attributed to either...
    assert session_mod.owning_session(repo) is None
    # ...and the caller must be able to tell that from "nobody holds a lease",
    # which is the difference between an unusable verdict and a legacy one.
    assert sorted(session_mod.holders(repo)) == ["session-a", "session-b"]


def test_holders_is_empty_when_no_lease_exists(session_mod, repo, registry):
    assert session_mod.holders(repo) == []
    assert session_mod.owning_session(repo) is None


def test_a_session_in_another_tree_is_not_a_rival(session_mod, repo, registry):
    """The core of the design: sharing a *repository* is fine, sharing a *checkout*
    is not. Two worktrees have two HEADs, so there is nothing to collide."""
    session_mod.claim(repo, "session-a")
    elsewhere = {
        "session": "session-b",
        "worktree": str(repo / "somewhere-else").lower(),
        "branch": "ticket/008",
        "heartbeat": time.time(),
    }
    (registry / "session-b.json").write_text(json.dumps(elsewhere), encoding="utf-8")

    assert session_mod.rivals(repo, "session-a") == []
    assert session_mod.owning_session(repo) == "session-a"


def test_a_stale_lease_is_reaped(session_mod, repo, registry):
    """A session killed overnight must not wedge the repo forever."""
    session_mod.claim(repo, "session-a")
    session_mod.claim(repo, "ghost")
    ghost = registry / "ghost.json"
    lease = json.loads(ghost.read_text(encoding="utf-8"))
    lease["heartbeat"] = time.time() - session_mod.STALE_AFTER - 1
    ghost.write_text(json.dumps(lease), encoding="utf-8")

    assert session_mod.rivals(repo, "session-a") == []
    assert not ghost.exists(), "a stale lease should be deleted, not just ignored"


def test_a_torn_lease_is_not_evidence_of_a_rival(session_mod, repo, registry):
    session_mod.claim(repo, "session-a")
    (registry / "half-written.json").write_text('{"session": "b"', encoding="utf-8")
    assert session_mod.rivals(repo, "session-a") == []


def test_release_and_release_all(session_mod, repo, registry):
    session_mod.claim(repo, "session-a")
    session_mod.claim(repo, "session-b")
    assert session_mod.release(repo, "session-a") is True
    assert session_mod.release(repo, "session-a") is False
    assert session_mod.release_all(repo) == 1
    assert session_mod.leases(repo) == []


def test_the_registry_lives_in_the_git_common_dir(session_mod, repo, monkeypatch):
    """Not in `.claude/context/`: each worktree has its own checkout of that, so two
    sessions would each read an empty registry and conclude they were alone."""
    monkeypatch.delenv("TCGI_SESSION_REGISTRY", raising=False)
    assert session_mod.registry_dir(repo) == repo / ".git" / "tcgi-sessions"


def test_a_linked_worktree_shares_the_registry(session_mod, repo, monkeypatch):
    """The property the whole design rests on: a session in a linked worktree must
    see the main clone's leases, or isolation is invisible to itself."""
    monkeypatch.delenv("TCGI_SESSION_REGISTRY", raising=False)
    linked = repo.parent / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(linked)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    try:
        assert session_mod.registry_dir(linked) == session_mod.registry_dir(repo)
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(linked)],
            cwd=repo,
            check=False,
            capture_output=True,
        )


# ---- hook mode, end to end --------------------------------------------------


def _run_hook(repo: Path, registry: Path, session: str, command: str):
    payload = {
        "tool_name": "Bash",
        "cwd": str(repo),
        "session_id": session,
        "tool_input": {"command": command},
    }
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "TCGI_SESSION_REGISTRY": str(registry)},
        check=False,
    )


def test_hook_allows_a_branch_switch_when_alone(repo, registry):
    registry.mkdir(parents=True, exist_ok=True)
    done = _run_hook(repo, registry, "session-a", "git switch -c ticket/007")
    assert done.returncode == 0, done.stderr


def test_hook_blocks_a_branch_switch_beside_a_rival(repo, registry, session_mod):
    registry.mkdir(parents=True, exist_ok=True)
    # A first call from session A takes the lease...
    _run_hook(repo, registry, "session-a", "git status")
    # ...then session B tries to move HEAD in the same tree.
    done = _run_hook(repo, registry, "session-b", "git switch -c ticket/008")
    assert done.returncode == 2, done.stdout
    assert "BLOCKED" in done.stderr
    assert "EnterWorktree" in done.stderr, "the remedy has to be in the message"
    assert "session-a"[:8] in done.stderr


def test_hook_still_allows_reads_beside_a_rival(repo, registry):
    registry.mkdir(parents=True, exist_ok=True)
    _run_hook(repo, registry, "session-a", "git status")
    done = _run_hook(repo, registry, "session-b", "git log --oneline -3")
    assert done.returncode == 0, done.stderr


def test_hook_ignores_tools_that_are_not_shells(repo, registry):
    payload = {
        "tool_name": "Read",
        "cwd": str(repo),
        "session_id": "session-b",
        "tool_input": {"command": "git switch -c x"},
    }
    done = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0


def test_hook_fails_open_without_a_session_id(repo, registry):
    """No identity means no basis to tell sessions apart, so no basis to deny."""
    registry.mkdir(parents=True, exist_ok=True)
    _run_hook(repo, registry, "session-a", "git status")
    payload = {
        "tool_name": "Bash",
        "cwd": str(repo),
        "tool_input": {"command": "git switch -c ticket/008"},
    }
    done = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0


def test_hook_fails_open_on_unparseable_input():
    done = subprocess.run(
        [sys.executable, str(GUARD)],
        input="not json at all",
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
