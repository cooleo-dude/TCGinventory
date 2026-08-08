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


def _file(
    tickets: Path,
    number: int,
    slug: str,
    *,
    status: str = "open",
    issue: str = "none",
) -> Path:
    path = tickets / f"{number:03d}-{slug}.md"
    path.write_text(
        f"# {number:03d} — {slug}\n\n```\nstatus:   {status}\nkind:     fix\n"
        f"source:   test\nissue:    {issue}\naxis:     correctness\n"
        f"found:    2026-08-07\nsaving:   -\nrisk:     low — test\n```\n\n"
        f"## The finding\n\nsomething\n",
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


# --- closing: the directory holds live work only -----------------------------


@pytest.fixture
def closer(tool, monkeypatch):
    """`tool`, plus a README to re-index and a `gh` that records instead of calling.

    Every `gh` call in `close` has to be intercepted: the tests must not touch the
    real repo's issues, and CI has no auth.
    """
    module, _tree, tickets = tool
    module.README.write_text(
        "# Tickets\n\n| # | title | kind | issue | status |\n|---|---|---|---|---|\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []
    remote = {"state": "OPEN"}

    def fake_gh(*args: str, check: bool = True) -> str:
        calls.append(args)
        if args[:2] == ("repo", "view"):
            return "owner/repo"
        if args[:2] == ("issue", "view"):
            return remote["state"]
        return ""

    monkeypatch.setattr(module, "gh", fake_gh)
    return module, tickets, calls, remote


def test_close_removes_the_ticket_file(closer):
    """The ask: a closed ticket must not sit in the directory forever."""
    module, tickets, _, _ = closer
    path = _file(tickets, 1, "landed", issue="#42")

    assert module.cmd_close(type("A", (), {"number": 1})()) == 0

    assert not path.exists(), "closed ticket still on disk"
    assert "001-landed.md" not in module.README.read_text(encoding="utf-8")


def test_close_mirrors_the_final_text_before_deleting(closer):
    """Order is the whole safety argument.

    The file is the only copy of the write-up the author just added; the issue body
    is stale until `close` pushes it. Closing (or deleting) first leaves a closed
    issue that still describes the work as pending, and no file to fix it from.
    """
    module, tickets, calls, _ = closer
    path = _file(tickets, 1, "landed", issue="#42")
    path.write_text(
        path.read_text(encoding="utf-8") + "\n## Result\n\nLanded in abc1234.\n",
        encoding="utf-8",
    )

    module.cmd_close(type("A", (), {"number": 1})())

    verbs = [c[1] for c in calls if c[0] == "issue"]
    assert verbs == ["edit", "view", "close"], verbs
    edit = next(c for c in calls if c[:2] == ("issue", "edit"))
    body = edit[edit.index("--body") + 1]
    assert "Landed in abc1234." in body, "pushed a body without the write-up"
    assert "**status**: done" in body, "pushed a body that still says open"


def test_close_refuses_a_ticket_with_no_issue(closer):
    """With the file deleted, the issue is the only surviving copy — so there has to
    be one. Otherwise `close` is a silent `rm`."""
    module, tickets, calls, _ = closer
    path = _file(tickets, 1, "unmirrored")

    with pytest.raises(SystemExit):
        module.cmd_close(type("A", (), {"number": 1})())

    assert path.is_file(), "deleted the only copy of an unmirrored ticket"
    assert calls == []


def test_close_keeps_dropped_from_becoming_done(closer):
    """Abandoned is not landed. Flipping the word would record work as shipped that
    never was, and close the issue as completed."""
    module, tickets, calls, _ = closer
    _file(tickets, 1, "abandoned", status="dropped", issue="#42")

    module.cmd_close(type("A", (), {"number": 1})())

    edit = next(c for c in calls if c[:2] == ("issue", "edit"))
    assert "**status**: dropped" in edit[edit.index("--body") + 1]
    close = next(c for c in calls if c[:2] == ("issue", "close"))
    assert "not planned" in close, close


def test_close_tolerates_an_already_closed_issue(closer):
    """Normal, not exceptional: a PR body saying "closes #N" closes the issue before
    anyone runs `close`. If that made `gh issue close` fatal, the file could never be
    removed — which is the pile this change exists to stop."""
    module, tickets, calls, remote = closer
    path = _file(tickets, 1, "already-closed", issue="#42")
    remote["state"] = "CLOSED"

    assert module.cmd_close(type("A", (), {"number": 1})()) == 0

    assert not path.exists()
    assert not [c for c in calls if c[:2] == ("issue", "close")]
    assert [c for c in calls if c[:2] == ("issue", "edit")], "never updated the body"


def test_check_flags_a_resolved_ticket_still_on_disk(closer):
    """A status hand-edited to `done` skips the mirror entirely: the issue stays open
    describing finished work. `check` is the only place that can still notice."""
    module, tickets, _, _ = closer
    _file(tickets, 1, "hand-edited", status="done", issue="#42")
    module._reindex()

    assert module.cmd_check(type("A", (), {})()) == 1


def test_check_passes_on_an_open_ticket(closer):
    module, tickets, _, _ = closer
    _file(tickets, 1, "outstanding", issue="#42")
    module._reindex()

    assert module.cmd_check(type("A", (), {})()) == 0
