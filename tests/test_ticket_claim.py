"""A ticket number must never be handed to two sessions.

`ticket.py next` was `max(local ticket numbers) + 1`. The local tickets directory
only holds tickets merged into the branch you are standing on, so a ticket filed an
hour ago on another session's branch is invisible: both sessions read `007`, both
write `007-*.md`, both file a GitHub issue, and `check` only notices once both
files exist on one branch.

Two changes, both tested here: the number comes from **every ref**, and `claim`
turns the read into an atomic create so the race has a loser instead of a
collision.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".claude/skills/sdd-ticket/scripts/ticket.py"


def _git(tree: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=tree, check=True, capture_output=True)


@pytest.fixture
def tool(tmp_path, monkeypatch):
    """The real module, pointed at a throwaway repo.

    `REPO`/`TICKETS`/`README` are module constants resolved from `__file__`, so a
    test has to redirect them — the alternative is writing tickets into the repo
    while the suite runs `-n auto`.
    """
    spec = importlib.util.spec_from_file_location("_ticket_tool", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_ticket_tool"] = module
    spec.loader.exec_module(module)

    tree = tmp_path / "repo"
    tickets = tree / ".claude" / "context" / "tickets"
    tickets.mkdir(parents=True)
    _git(tree, "init", "-q", "-b", "main")
    _git(tree, "config", "user.email", "t@example.com")
    _git(tree, "config", "user.name", "Test")

    monkeypatch.setattr(module, "REPO", tree)
    monkeypatch.setattr(module, "TICKETS", tickets)
    monkeypatch.setattr(module, "README", tickets / "README.md")
    return module, tree, tickets


def _file(tickets: Path, number: int, slug: str) -> Path:
    path = tickets / f"{number:03d}-{slug}.md"
    path.write_text(
        f"# {number:03d} — {slug}\n\n```\nstatus:   open\nkind:     fix\n"
        f"source:   test\nissue:    none\naxis:     correctness\n"
        f"found:    2026-08-07\nsaving:   -\nrisk:     low — test\n```\n",
        encoding="utf-8",
    )
    return path


def test_a_number_used_on_another_branch_is_not_reused(tool):
    """The collision itself. 003 exists only on a branch this checkout is not on."""
    module, tree, tickets = tool
    _file(tickets, 1, "first")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "ticket 001")

    _git(tree, "checkout", "-q", "-b", "other")
    _file(tickets, 3, "filed-elsewhere")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "ticket 003")
    _git(tree, "checkout", "-q", "main")

    # The local directory cannot see it — that is the bug, asserted directly.
    assert {t.number for t in module.load()} == {1}
    # Every ref can.
    assert 3 in module.taken_numbers()
    assert module.next_number() == 4


def test_a_deleted_ticket_number_is_still_taken(tool):
    """A number that was ever used is never handed out again: reusing one silently
    re-points every commit, branch and issue that cited it."""
    module, tree, tickets = tool
    path = _file(tickets, 5, "short-lived")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "ticket 005")
    path.unlink()
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "remove it")

    assert {t.number for t in module.load()} == set()
    assert module.next_number() == 6


def test_next_is_one_on_an_empty_repo(tool):
    module, _, _ = tool
    assert module.next_number() == 1


def test_claim_creates_the_file_and_reserves_the_number(tool):
    module, tree, tickets = tool
    args = type("A", (), {"slug": "isolate-sessions"})()
    assert module.cmd_claim(args) == 0

    created = list(tickets.glob("001-*.md"))
    assert created and created[0].name == "001-isolate-sessions.md"
    body = created[0].read_text(encoding="utf-8")
    assert body.startswith("# 001 — Isolate sessions")
    # The skeleton must satisfy the format checker, or `claim` hands over a file
    # that fails `check` before it has been written.
    assert "status:   open" in body


def test_claim_steps_over_a_number_taken_under_a_different_title(tool, monkeypatch):
    """The race, forced — and the bug this test found.

    `next_number()` has to be pinned to 001 for this to test anything: left alone it
    already sees the file on disk and returns 002, so the contended branch never runs
    and the test passes while proving nothing.

    Pinned, it caught the first implementation reserving `NNN-<slug>.md` — keyed on
    the *filename*. Two sessions claiming 007 with different titles both succeeded,
    which is precisely the collision. The reservation has to key on the number alone.
    """
    module, tree, tickets = tool
    existing = _file(tickets, 1, "already-here")
    before = existing.read_text(encoding="utf-8")
    monkeypatch.setattr(module, "next_number", lambda: 1)

    args = type("A", (), {"slug": "mine"})()
    assert module.cmd_claim(args) == 0

    assert not (tickets / "001-mine.md").exists(), "two tickets share number 001"
    assert (tickets / "002-mine.md").is_file(), "did not step past the taken number"
    assert existing.read_text(encoding="utf-8") == before, "clobbered a live ticket"


def test_a_number_reserved_by_another_session_is_skipped(tool, monkeypatch):
    """The window `next_number()` cannot see: the other session has reserved 001 but
    has not committed — or even written — its file yet."""
    module, tree, tickets = tool
    monkeypatch.setattr(module, "next_number", lambda: 1)
    reserved = module.reservations_dir()
    reserved.mkdir(parents=True, exist_ok=True)
    (reserved / "001").write_text("", encoding="utf-8")

    args = type("A", (), {"slug": "mine"})()
    assert module.cmd_claim(args) == 0
    assert (tickets / "002-mine.md").is_file()


def test_reservations_live_in_the_shared_git_dir(tool):
    """Not under `.claude/context/`: each worktree has its own checkout of that, so
    a marker written there is invisible to the session it is meant to warn."""
    module, tree, _ = tool
    assert module.reservations_dir() == tree / ".git" / "tcgi-ticket-numbers"


def test_claim_gives_up_rather_than_scanning_forever(tool, monkeypatch):
    """The bounded retry has to have a floor: with every candidate taken, `claim`
    must fail loudly instead of returning a number it did not reserve."""
    module, tree, tickets = tool
    for number in range(1, 60):
        _file(tickets, number, "taken")
    monkeypatch.setattr(module, "next_number", lambda: 1)

    args = type("A", (), {"slug": "taken"})()
    with pytest.raises(SystemExit):
        module.cmd_claim(args)


def test_claim_refuses_an_empty_slug(tool):
    module, _, _ = tool
    args = type("A", (), {"slug": "  -  "})()
    with pytest.raises(SystemExit):
        module.cmd_claim(args)
