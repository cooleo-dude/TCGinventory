"""One work item, one number — and the checks that keep it that way.

`X` is the GitHub pull-request number: `X#` is the specification, `X!` the change
that satisfies it. The number therefore appears in three places on disk — the
filename, the heading sigil and the `mr:` field — and every one of these tests
exists because a disagreement between them silently retitles or rewrites the wrong
MR.

The tests that used to live here covered `claim`'s number reservation: a local
counter could hand `007` to two sessions, so it was defended with `taken_numbers()`
over every ref and `O_EXCL` markers in the git common dir. GitHub allocates now and
cannot issue one number twice, so both the machinery and its tests are gone.
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

    `REPO`/`TICKETS` are module constants resolved from `__file__`, so a test has to
    redirect them — the alternative is writing tickets into the repo while the suite
    runs `-n auto`.
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
    (tree / "README.md").write_text("repo\n", encoding="utf-8")
    _git(tree, "add", "README.md")
    _git(tree, "commit", "-qm", "initial")

    monkeypatch.setattr(module, "REPO", tree)
    monkeypatch.setattr(module, "TICKETS", tickets)
    return module, tree, tickets


@pytest.fixture
def fakes(tool, monkeypatch):
    """`tool`, plus a `gh` that records instead of calling and a `git` kept local.

    Every `gh` call has to be intercepted: the tests must not touch the real repo's
    pull requests, and CI has no auth. `git` is intercepted only for `push` — the
    rest runs for real, because `cmd_open` depends on `git mv` actually moving the
    file it then reads back.
    """
    module, tree, tickets = tool
    calls: list[tuple[str, ...]] = []
    remote = {"state": "OPEN", "number": 41, "list": ""}

    def fake_gh(*args: str, check: bool = True) -> str:
        calls.append(args)
        if args[:2] == ("pr", "create"):
            return f"https://github.com/o/r/pull/{remote['number']}"
        if args[:2] == ("pr", "view"):
            return remote["state"]
        if args[:2] == ("pr", "list"):
            return remote["list"]
        return ""

    real = module.git_checked

    def fake_git(*args: str) -> str:
        calls.append(("git", *args))
        if args[0] == "push":
            return ""
        return real(*args)

    monkeypatch.setattr(module, "gh", fake_gh)
    monkeypatch.setattr(module, "git_checked", fake_git)
    return module, tree, tickets, calls, remote


def _file(
    tickets: Path,
    number: int,
    slug: str,
    *,
    status: str = "in-progress",
    mr: str | int | None = None,
    heading: str | None = None,
    name: str | None = None,
) -> Path:
    """A ticket on disk. Every parameter that can disagree with another is settable,
    because the disagreements are what `check` exists to catch."""
    mr = number if mr is None else mr
    head = f"# {number}# — {slug}" if heading is None else heading
    path = tickets / (name or f"{number:03d}-{slug}.md")
    path.write_text(
        f"{head}\n\n```\nstatus:   {status}\nkind:     fix\n"
        f"source:   test\nmr:       {mr}\naxis:     correctness\n"
        f"found:    2026-08-10\nsaving:   -\nrisk:     low — test\n```\n\n"
        f"## The finding\n\nsomething\n",
        encoding="utf-8",
    )
    return path


def _tracked(tree: Path, path: Path) -> Path:
    """Commit a ticket file. `close` removes it with `git rm`, which needs it tracked
    — as it always is in real use, because `open` commits it."""
    rel = path.relative_to(tree).as_posix()
    _git(tree, "add", "--", rel)
    _git(tree, "commit", "-qm", f"ticket {path.name}", "--", rel)
    return path


def _args(**kwargs):
    return type("A", (), kwargs)()


# --- opening: GitHub names the ticket ----------------------------------------


def test_open_names_the_ticket_after_the_mr_it_created(fakes):
    """The whole point. The number is not ours to pick, so `open` files first and
    renames to whatever came back."""
    module, tree, tickets, calls, remote = fakes
    _git(tree, "checkout", "-q", "-b", "some-work")
    remote["number"] = 41

    assert module.cmd_open(_args(slug="mr-is-the-ticket")) == 0

    assert not (tickets / "draft-mr-is-the-ticket.md").exists(), "draft left behind"
    final = tickets / "041-mr-is-the-ticket.md"
    assert final.is_file(), sorted(p.name for p in tickets.glob("*"))
    body = final.read_text(encoding="utf-8")
    assert body.startswith("# 41# — Mr is the ticket"), body.splitlines()[0]
    assert "mr:       41" in body
    # The MR is retitled with the sigil, so the queue reads the same as the file.
    edit = next(c for c in calls if c[:2] == ("pr", "edit"))
    assert edit[edit.index("--title") + 1] == "41! Mr is the ticket"


def test_open_writes_a_skeleton_that_passes_check(fakes):
    """`open` handing over a file that fails `check` would make the first thing every
    author does a repair."""
    module, tree, _tickets, _calls, _remote = fakes
    _git(tree, "checkout", "-q", "-b", "some-work")

    module.cmd_open(_args(slug="well-formed"))

    assert module.cmd_check(_args()) == 0


def test_open_refuses_on_main(fakes):
    """A ticket is a branch with an MR. On `main` there is nothing to open one
    against, and the commit would land on the trunk."""
    module, _tree, tickets, calls, _remote = fakes

    with pytest.raises(SystemExit):
        module.cmd_open(_args(slug="on-the-trunk"))

    assert list(tickets.glob("*.md")) == []
    assert calls == []


def test_open_refuses_a_second_ticket_on_one_branch(fakes):
    """One ticket per MR, or `push` and `close` cannot tell which one they mean."""
    module, tree, tickets, calls, _remote = fakes
    _git(tree, "checkout", "-q", "-b", "some-work")
    _file(tickets, 41, "already-here")

    with pytest.raises(SystemExit):
        module.cmd_open(_args(slug="second"))

    assert not list(tickets.glob("*second*")), "filed a second ticket anyway"
    assert calls == []


def test_open_refuses_a_slug_that_is_not_kebab_case(fakes):
    """The slug becomes a filename and part of the MR title; spaces and capitals in
    it break the `_NAME` match that finds the ticket again."""
    module, tree, _tickets, _calls, _remote = fakes
    _git(tree, "checkout", "-q", "-b", "some-work")

    for bad in ("Not Kebab", "trailing_underscore", "  -  "):
        with pytest.raises(SystemExit):
            module.cmd_open(_args(slug=bad))


# --- check: the three copies of the number must agree ------------------------


def test_check_passes_on_a_well_formed_ticket(fakes):
    module, _tree, tickets, _calls, _remote = fakes
    _file(tickets, 41, "consistent")

    assert module.cmd_check(_args()) == 0


def test_check_fails_when_mr_disagrees_with_the_filename(fakes):
    """The failure this convention exists to make impossible: `push` would retitle
    MR 7 from a file describing ticket 41."""
    module, _tree, tickets, _calls, _remote = fakes
    _file(tickets, 41, "crossed-wires", mr=7)

    assert module.cmd_check(_args()) == 1


def test_check_fails_when_the_heading_sigil_disagrees(fakes):
    module, _tree, tickets, _calls, _remote = fakes
    _file(tickets, 41, "crossed-wires", heading="# 7# — crossed wires")

    assert module.cmd_check(_args()) == 1


def test_check_fails_when_the_heading_has_no_sigil(fakes):
    """The sigil is the human-readable half of the convention. Without it the file
    reads like the old scheme, where the heading number meant something else."""
    module, _tree, tickets, _calls, _remote = fakes
    _file(tickets, 41, "unsigiled", heading="# Unsigiled thing")

    assert module.cmd_check(_args()) == 1


def test_check_fails_on_an_unnumbered_draft(fakes):
    """`draft-*.md` is legitimate only between `open`'s push and its rename. On disk
    afterwards it means `open` died halfway and the MR is unnumbered."""
    module, _tree, tickets, _calls, _remote = fakes
    _file(tickets, 41, "half-open", mr="none", name="draft-half-open.md")

    assert module.cmd_check(_args()) == 1


def test_check_fails_on_a_mr_field_that_is_not_a_number(fakes):
    module, _tree, tickets, _calls, _remote = fakes
    _file(tickets, 41, "unmirrored", mr="none")

    assert module.cmd_check(_args()) == 1


def test_check_flags_a_resolved_ticket_still_on_disk(fakes):
    """A status hand-edited to `done` skips `close` entirely, so the MR body still
    describes the brief and the file rides into `main`."""
    module, _tree, tickets, _calls, _remote = fakes
    _file(tickets, 41, "hand-edited", status="done")

    assert module.cmd_check(_args()) == 1


# --- closing: record, then remove, then merge --------------------------------


def test_close_rewrites_the_body_before_removing_the_file(fakes):
    """Order is the whole safety argument. The file is the only copy of the write-up
    the author just added; the MR body is stale until `close` pushes it. Removing
    first leaves an MR describing the brief and no file to fix it from."""
    module, tree, tickets, calls, _remote = fakes
    path = _tracked(tree, _file(tickets, 41, "landed"))
    path.write_text(
        path.read_text(encoding="utf-8") + "\n## Result\n\nLanded in abc1234.\n",
        encoding="utf-8",
    )

    assert module.cmd_close(_args()) == 0

    edit = next(c for c in calls if c[:2] == ("pr", "edit"))
    body = edit[edit.index("--body") + 1]
    assert "Landed in abc1234." in body, "pushed a body without the write-up"
    assert "**status**: done" in body, "pushed a body that still says in-progress"
    assert not path.exists(), "ticket file survived close"
    # And the removal is committed, or the merge carries the file into `main`.
    removed = [i for i, c in enumerate(calls) if c[:2] == ("git", "rm")]
    assert removed, calls
    assert calls.index(edit) < removed[0], "removed the file before recording it"
    assert any(c[:2] == ("git", "commit") for c in calls), "removal never committed"


def test_close_refuses_once_the_mr_is_merged(fakes):
    """The one-way door. After the merge the file is already on `main` and removing
    it needs its own PR — which is the overhead this scheme removed."""
    module, _tree, tickets, calls, remote = fakes
    path = _file(tickets, 41, "too-late")
    remote["state"] = "MERGED"

    with pytest.raises(SystemExit):
        module.cmd_close(_args())

    assert path.is_file(), "removed the file after the merge had taken it to main"
    assert not [c for c in calls if c[:2] == ("pr", "edit")], "rewrote a merged MR"


def test_close_refuses_a_closed_mr_unless_the_ticket_says_dropped(fakes):
    """An MR closed without merging means the work did not land. Recording `done`
    against it would claim a change shipped that is not in `main`."""
    module, _tree, tickets, _calls, remote = fakes
    path = _file(tickets, 41, "abandoned-but-claimed", status="in-progress")
    remote["state"] = "CLOSED"

    with pytest.raises(SystemExit):
        module.cmd_close(_args())

    assert path.is_file()


def test_close_keeps_dropped_from_becoming_done(fakes):
    """Abandoned is not landed. Flipping the word records work as shipped that never
    was."""
    module, tree, tickets, calls, remote = fakes
    _tracked(tree, _file(tickets, 41, "abandoned", status="dropped"))
    remote["state"] = "CLOSED"

    assert module.cmd_close(_args()) == 0

    edit = next(c for c in calls if c[:2] == ("pr", "edit"))
    assert "**status**: dropped" in edit[edit.index("--body") + 1]


def test_close_refuses_when_there_is_no_ticket_on_the_branch(fakes):
    module, _tree, _tickets, calls, _remote = fakes

    with pytest.raises(SystemExit):
        module.cmd_close(_args())

    assert calls == []


# --- listing: the queue is GitHub, not the directory -------------------------


def test_list_reads_open_mrs_and_shows_the_sigil(fakes, capsys):
    """`main` carries no ticket files, so the local directory can only ever show the
    branch you are standing on — which is not a queue."""
    module, _tree, _tickets, _calls, remote = fakes
    remote["list"] = (
        "41\ttrue\tmr-is-the-ticket\t41! The merge request is the ticket\n"
        "12\tfalse\tprewarm\t12! Recognition prewarm holds 112 MB\n"
    )

    assert module.cmd_list(_args()) == 0

    out = capsys.readouterr().out
    assert "41!" in out and "The merge request is the ticket" in out
    assert "draft" in out and "ready" in out, "never distinguished draft from ready"
    assert "mr-is-the-ticket" in out, "never named the branch"
    # Sorted by number, so the queue reads in filing order rather than gh's.
    assert out.index("12!") < out.index("41!")
    # The sigil is stripped from the displayed title — printing "41!  41! ..." twice
    # is what the first cut did.
    assert "41! The merge request" not in out


def test_list_keeps_an_mr_whose_title_lost_its_sigil(fakes, capsys):
    """A PR opened by hand has no sigil. It still belongs in the listing — silently
    dropping it would hide real open work behind a convention it predates."""
    module, _tree, _tickets, _calls, remote = fakes
    remote["list"] = "9\tfalse\tsome-branch\tOpened by hand\n"

    assert module.cmd_list(_args()) == 0

    out = capsys.readouterr().out
    assert "Opened by hand" in out, "dropped an open MR for lacking a sigil"
    assert "no sigil" in out, "listed it as if it were a well-formed ticket"
