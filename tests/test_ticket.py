"""The ticket lives on GitHub, so these tests stand in for GitHub.

`X#` is the specification and `X!` the change that satisfies it — the same object,
because a pull request *is* an issue with a head and base attached. `start` converts
one into the other in place, which is what keeps the number identical.

Everything here goes through `gh`, so `gh` is the seam: `_hub` is a fake GitHub that
holds issues in a dict and answers the handful of calls the tool makes. No test may
reach the network — the suite runs `-n auto` against a private repo and CI has no
auth.

The tests this replaced covered a local ticket file: that the filename, the heading
sigil and the `mr:` field all agreed, because a disagreement meant `push` retitled
one MR from a file describing another. There is one copy of the number now and it
cannot disagree with itself, so those tests describe a hazard that no longer exists.
What replaces them is the sigil/phase check — a pull request must not still call
itself a specification.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".claude/skills/sdd-ticket/scripts/ticket.py"

FRONT = (
    "```\nstatus:   in-progress\nkind:     fix\nsource:   test\n"
    "axis:     correctness\nfound:    2026-08-10\nsaving:   -\n"
    "risk:     low — test\n```\n\n## The finding\n\nsomething\n"
)


@pytest.fixture
def tool(monkeypatch):
    spec = importlib.util.spec_from_file_location("_ticket_tool", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_ticket_tool"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_SLUG", ["o/r"])
    return module


class Hub:
    """A fake GitHub: issues in a dict, where a pull request is one that has a
    `pull_request` key — which is what the real thing is, too."""

    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self.next_number = 46
        self.calls: list[tuple[str, ...]] = []

    def add(self, number: int, title: str, body: str = FRONT, **over) -> dict:
        payload = {"title": title, "body": body, "state": "OPEN"}
        payload.update(over)
        self.issues[number] = payload
        return payload

    def __call__(self, *args: str, check: bool = True) -> str:
        self.calls.append(args)
        if args[:2] == ("issue", "create"):
            number = self.next_number
            self.next_number += 1
            title = args[args.index("--title") + 1]
            body = args[args.index("--body") + 1]
            self.add(number, title, body)
            return f"https://github.com/o/r/issues/{number}"
        if args[0] == "api":
            return self._api(args)
        if args[:2] == ("issue", "edit"):
            number = int(args[2])
            self.issues[number]["title"] = args[args.index("--title") + 1]
            return ""
        if args[:2] == ("issue", "list"):
            return "\n".join(
                f"{n}\t{i['title']}"
                for n, i in sorted(self.issues.items())
                if i["state"] == "OPEN" and "pull_request" not in i
            )
        if args[:2] == ("pr", "list"):
            return "\n".join(
                "\t".join(
                    (
                        str(n),
                        str(i.get("draft", False)).lower(),
                        i.get("head", "b"),
                        i["title"],
                    )
                )
                for n, i in sorted(self.issues.items())
                if i["state"] == "OPEN" and "pull_request" in i
            )
        return ""

    def _api(self, args: tuple[str, ...]) -> str:
        path = (
            args[-1]
            if "-X" not in args
            else next(a for a in args if a.startswith("repos/"))
        )
        fields = {}
        for i, token in enumerate(args):
            if token in ("-f", "-F") and i + 1 < len(args):
                key, _, value = args[i + 1].partition("=")
                fields[key] = value
        if "-X" in args and args[args.index("-X") + 1] == "POST":
            # Converting an issue into a pull request: the same object gains a head.
            number = int(fields["issue"])
            self.issues[number]["pull_request"] = {"merged_at": None}
            self.issues[number]["head"] = fields.get("head", "b")
            self.issues[number]["draft"] = fields.get("draft") == "true"
            return json.dumps({"number": number})
        if "-X" in args and args[args.index("-X") + 1] == "PATCH":
            number = int(path.rsplit("/", 1)[-1])
            self.issues[number].update(
                {k: v for k, v in fields.items() if k in ("title", "body")}
            )
            return json.dumps(self.issues[number])
        number = int(path.rsplit("/", 1)[-1])
        if number not in self.issues:
            raise SystemExit(f"gh api {path}\nNot Found")
        return json.dumps(self.issues[number])


@pytest.fixture
def hub(tool, monkeypatch):
    fake = Hub()
    monkeypatch.setattr(tool, "gh", fake)
    monkeypatch.setattr(
        tool,
        "git",
        lambda *a: "some-work\n" if a[0] == "rev-parse" else "3\n",
    )
    return fake


def _args(**kwargs):
    return type("A", (), kwargs)()


# --- talking to gh at all ----------------------------------------------------


def test_gh_decodes_utf8_rather_than_the_windows_locale(tool, monkeypatch):
    """The bug this pins cost a `close` that had already succeeded.

    `text=True` decodes with the *locale* codec. On Windows that is cp1252, where
    0x9d — the tail of a UTF-8 right curly quote — is undefined, so a ticket body
    containing one blew up in subprocess's reader thread. `stdout` came back `None`
    and the caller died on `None.strip()` **after** the PATCH had landed: the write
    went through, the reply was unreadable, and the tool reported failure.

    Ticket bodies in this repo are full of em dashes and curly quotes, so this is
    the ordinary case rather than an edge one.
    """
    seen: dict[str, object] = {}
    body = "status: done — “quoted”, ~1 GB…"

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=body, stderr="")

    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    assert tool.gh("api", "repos/o/r/issues/46") == body
    assert seen.get("encoding") == "utf-8", "left the decode to the locale codec"
    assert seen.get("errors") == "replace", "an odd byte must not raise mid-read"


def test_gh_survives_output_it_could_not_decode(tool, monkeypatch):
    """`stdout` is `None` whenever the decode fails, so every read must tolerate it
    instead of raising `AttributeError` on `None.strip()`."""

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=None, stderr=None)

    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    assert tool.gh("api", "x") == ""
    assert tool.git("rev-parse", "HEAD") == ""


# --- open: the number arrives with the issue ---------------------------------


def test_open_files_an_issue_and_stamps_the_spec_sigil(tool, hub):
    """No branch, no commit, no worktree — the whole reason to file as an issue is
    that a specification can exist before any code does."""
    assert tool.cmd_open(_args(slug="retire-local-ticket-files")) == 0

    assert 46 in hub.issues
    assert hub.issues[46]["title"] == "46# Retire local ticket files"
    assert "status:   in-progress" in hub.issues[46]["body"]
    # Nothing may touch git: filing predates the branch.
    assert not [c for c in hub.calls if c[:2] == ("pr", "create")]


def test_open_writes_a_skeleton_that_passes_check(tool, hub):
    tool.cmd_open(_args(slug="well-formed"))
    assert tool.cmd_check(_args(number=46)) == 0


@pytest.mark.parametrize("bad", ["Not Kebab", "trailing_underscore", "  -  ", ""])
def test_open_refuses_a_slug_that_is_not_kebab_case(tool, hub, bad):
    """The slug becomes the title and the branch people name after it."""
    with pytest.raises(SystemExit):
        tool.cmd_open(_args(slug=bad))
    assert hub.issues == {}


# --- start: the issue becomes the pull request, keeping its number ------------


def test_start_converts_the_issue_in_place(tool, hub):
    """The mechanism the whole scheme rests on. `POST /pulls` with `issue=N` converts
    rather than creates, so N is preserved — verified live when 46# became 46!."""
    hub.add(46, "46# Retire the local ticket directory")

    assert tool.cmd_start(_args(number=46, draft=False)) == 0

    assert hub.issues[46]["pull_request"] is not None, "never became a pull request"
    assert hub.issues[46]["title"] == "46! Retire the local ticket directory"
    assert list(hub.issues) == [46], "created a second object instead of converting"
    post = next(c for c in hub.calls if "-X" in c and c[c.index("-X") + 1] == "POST")
    assert "issue=46" in post, "did not convert by issue number"


def test_start_refuses_something_already_started(tool, hub):
    hub.add(46, "46! x", pull_request={"merged_at": None})
    with pytest.raises(SystemExit):
        tool.cmd_start(_args(number=46, draft=False))


def test_start_refuses_on_main(tool, hub, monkeypatch):
    """A pull request opens from the branch you are on, and `main` is not one."""
    hub.add(46, "46# x")
    monkeypatch.setattr(tool, "git", lambda *a: "main\n")
    with pytest.raises(SystemExit):
        tool.cmd_start(_args(number=46, draft=False))
    assert "pull_request" not in hub.issues[46]


def test_start_refuses_a_branch_with_nothing_on_it(tool, hub, monkeypatch):
    """GitHub cannot open a pull request against a branch identical to its base. The
    previous design solved this with an empty commit; filing as an issue means the
    number already exists and this can simply refuse."""
    hub.add(46, "46# x")
    monkeypatch.setattr(
        tool, "git", lambda *a: "some-work\n" if a[0] == "rev-parse" else "0\n"
    )
    with pytest.raises(SystemExit):
        tool.cmd_start(_args(number=46, draft=False))
    assert "pull_request" not in hub.issues[46]


# --- check: what can still be wrong with one copy of the number --------------


def test_check_passes_on_a_well_formed_spec(tool, hub):
    hub.add(46, "46# Something specified")
    assert tool.cmd_check(_args(number=46)) == 0


def test_check_fails_when_the_title_has_no_sigil(tool, hub):
    """The sigil is how a reader tells a specification from a change in flight."""
    hub.add(46, "Retire the local ticket directory")
    assert tool.cmd_check(_args(number=46)) == 1


def test_check_fails_when_the_title_number_is_not_this_ticket(tool, hub):
    hub.add(46, "7# Someone else's work")
    assert tool.cmd_check(_args(number=46)) == 1


def test_check_fails_when_a_pull_request_still_calls_itself_a_spec(tool, hub):
    """The replacement for the old three-copies check. `start` retitles; if it died
    between converting and retitling, the queue shows work as unstarted that is
    already open for review."""
    hub.add(46, "46# Still a spec, apparently", pull_request={"merged_at": None})
    assert tool.cmd_check(_args(number=46)) == 1


def test_check_fails_when_an_issue_claims_to_be_a_pull_request(tool, hub):
    hub.add(46, "46! Not really")
    assert tool.cmd_check(_args(number=46)) == 1


@pytest.mark.parametrize(
    "field", ["status", "kind", "source", "axis", "found", "saving", "risk"]
)
def test_check_fails_on_a_missing_front_block_field(tool, hub, field):
    """Each of these is something the next agent needs and cannot re-derive."""
    body = "\n".join(
        line for line in FRONT.splitlines() if not line.startswith(f"{field}:")
    )
    hub.add(46, "46# x", body=body)
    assert tool.cmd_check(_args(number=46)) == 1


@pytest.mark.parametrize(
    "line, replacement",
    [
        ("status:   in-progress", "status:   nearly"),
        ("kind:     fix", "kind:     vibes"),
    ],
)
def test_check_fails_on_a_value_outside_the_vocabulary(tool, hub, line, replacement):
    hub.add(46, "46# x", body=FRONT.replace(line, replacement))
    assert tool.cmd_check(_args(number=46)) == 1


def test_check_flags_a_resolved_ticket_that_is_still_open(tool, hub):
    """A status hand-edited to `done` skips `close`, so the body still ends at the
    plan and the queue still shows the work as live."""
    hub.add(
        46,
        "46! x",
        body=FRONT.replace("status:   in-progress", "status:   done"),
        pull_request={"merged_at": None},
    )
    assert tool.cmd_check(_args(number=46)) == 1


# --- show / push: the body is the only copy ----------------------------------


def test_push_uploads_the_file_and_strips_a_show_heading(tool, hub, tmp_path):
    """`show` prints the title as a heading so it reads as a document. Pushing that
    back must not leave the heading in the body, or every edit adds another copy of
    the title."""
    hub.add(46, "46! Retire it", pull_request={"merged_at": None})
    edited = tmp_path / "t.md"
    edited.write_text(
        f"# 46! Retire it\n\n{FRONT}\n## Result\n\nLanded.\n", encoding="utf-8"
    )

    assert tool.cmd_push(_args(number=46, file=str(edited))) == 0

    body = hub.issues[46]["body"]
    assert body.startswith("```"), body[:40]
    assert "## Result" in body and "Landed." in body
    assert "46! Retire it" not in body, "left the show heading in the body"
    assert hub.issues[46]["title"] == "46! Retire it"


def test_push_refuses_a_file_that_is_not_there(tool, hub, tmp_path):
    hub.add(46, "46! x", pull_request={"merged_at": None})
    with pytest.raises(SystemExit):
        tool.cmd_push(_args(number=46, file=str(tmp_path / "nope.md")))


def test_show_prints_the_title_and_the_body(tool, hub, capsys):
    hub.add(46, "46! Retire it", pull_request={"merged_at": None})
    assert tool.cmd_show(_args(number=46)) == 0
    out = capsys.readouterr().out
    assert "46! Retire it" in out and "## The finding" in out


def test_show_renders_the_sigil_of_the_real_phase(tool, hub, capsys):
    """The stored title can be stale — `start` retitles as a second step, so it can be
    interrupted. Everything that prints or writes a title derives the sigil from the
    object instead of trusting the string, which is why `push` repairs it in passing.
    """
    hub.add(46, "46! Claims to be started")

    assert tool.cmd_show(_args(number=46)) == 0

    assert "46# Claims to be started" in capsys.readouterr().out


# --- close: record the outcome, then merge -----------------------------------


def test_close_writes_done_into_the_body(tool, hub):
    hub.add(46, "46! Retire it", pull_request={"merged_at": None})
    assert tool.cmd_close(_args(number=46)) == 0
    assert "status:   done" in hub.issues[46]["body"]


def test_close_keeps_dropped_from_becoming_done(tool, hub):
    """Abandoned is not landed."""
    hub.add(
        46,
        "46! x",
        body=FRONT.replace("status:   in-progress", "status:   dropped"),
        pull_request={"merged_at": None},
    )
    assert tool.cmd_close(_args(number=46)) == 0
    assert "status:   dropped" in hub.issues[46]["body"]


def test_close_refuses_something_never_started(tool, hub):
    """A specification that was never converted describes work that never landed."""
    hub.add(46, "46# x")
    with pytest.raises(SystemExit):
        tool.cmd_close(_args(number=46))
    assert "status:   in-progress" in hub.issues[46]["body"]


def test_close_refuses_once_it_is_merged(tool, hub):
    """The one-way door, and the reason `close` runs before the merge: afterwards the
    body is a plan nobody goes back to correct, and there is no local copy to rewrite
    it from."""
    hub.add(46, "46! x", pull_request={"merged_at": "2026-08-11T01:59:27Z"})
    with pytest.raises(SystemExit):
        tool.cmd_close(_args(number=46))
    assert "status:   in-progress" in hub.issues[46]["body"], "rewrote a merged record"


# --- list: the queue is GitHub, in two phases --------------------------------


def test_list_separates_specifications_from_work_in_flight(tool, hub, capsys):
    hub.add(46, "46# Not started yet")
    hub.add(
        42, "42! In flight", pull_request={"merged_at": None}, draft=True, head="wip"
    )

    assert tool.cmd_list(_args()) == 0

    out = capsys.readouterr().out
    assert "46#" in out and "spec" in out
    assert "42!" in out and "draft" in out and "wip" in out
    assert out.index("42!") < out.index("46#"), "not ordered by number"
    assert "46# Not started" not in out, "printed the sigil twice"


def test_list_keeps_a_title_that_lost_its_sigil(tool, hub, capsys):
    """Legacy issues and hand-opened pull requests have no sigil. Dropping them would
    hide real open work behind a convention it predates."""
    hub.add(22, "[009] Rewrite the README")

    assert tool.cmd_list(_args()) == 0

    out = capsys.readouterr().out
    assert "Rewrite the README" in out and "no sigil" in out
