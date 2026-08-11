"""Ticket tool for TCGinventory — the ticket lives on GitHub, and nowhere else.

    ticket.py open <kebab-title>   file the specification as an issue; its number is X
    ticket.py list                 the queue: X# not started, X! in flight
    ticket.py show <X>             print the title and body
    ticket.py push <X> <file>      replace the body from a file you wrote
    ticket.py start <X>            convert issue X into pull request X
    ticket.py check <X>            non-zero if the ticket is malformed
    ticket.py close <X>            record the outcome, then merge

**One work item, one number, one copy of the text.** ``X`` is a GitHub number:
``X#`` names the specification and ``X!`` names the change that satisfies it. The
sigil says which phase it is in; the number never changes, because it is the same
object throughout.

That is possible because **a pull request on GitHub *is* an issue** with a head and
a base attached — which is also why issues and pull requests share one per-repo
counter. So ``start`` does not create a second object and copy anything across: it
converts the issue in place, keeping its number, its body and its comments.
``POST /pulls`` takes an ``issue`` parameter for exactly this, in place of
``title``/``body``. The parameter is absent from the current REST documentation but
live; verified on this repo when issue 46 became pull request 46.

Two consequences worth knowing:

* **Filing needs no branch, no commit and no worktree.** ``open`` files an issue, so
  a specification can exist before any code does. ``start`` is the step that needs a
  pushed branch with real commits on it.
* **There is no local copy of anything.** The body is the ticket. ``show`` prints it,
  ``push`` replaces it from a file you wrote — put that file in the session
  scratchpad, not in the repo. An earlier design kept the text in
  ``.claude/context/tickets/``, which meant three copies of the number (filename,
  heading, ``mr:`` field) and a checker to keep them agreeing. One copy cannot
  disagree with itself.

Everything here needs ``gh`` authenticated; there is no offline mode, by design.
``gh`` installs into the *machine* PATH, so a shell opened before the install will
not see it — this resolves the absolute path as a fallback.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
GH_FALLBACK = Path(r"C:\Program Files\GitHub CLI\gh.exe")

STATUSES = ("open", "in-progress", "deferred", "done", "dropped")
KINDS = ("optimization", "dead-code", "feature", "fix")
REQUIRED = ("status", "kind", "source", "axis", "found", "saving", "risk")

_FIELD = re.compile(r"^(\w+):\s*(.*?)\s*$", re.MULTILINE)
# "46# Retire the local ticket directory" while it is a specification, "46! …" once
# it is the change. The sigil is not decoration: `check` uses it to catch a pull
# request still advertising itself as an unstarted ticket.
_TITLE = re.compile(r"^(\d+)([#!])\s+(.+?)\s*$")


def gh(*args: str, check: bool = True) -> str:
    exe = "gh"
    try:
        subprocess.run([exe, "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        if not GH_FALLBACK.is_file():
            raise SystemExit(
                "gh not found — install it, or open a fresh shell"
            ) from None
        exe = str(GH_FALLBACK)
    done = subprocess.run(
        [exe, *args], capture_output=True, text=True, cwd=REPO, check=False
    )
    if check and done.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)}\n{done.stderr.strip()}")
    return done.stdout.strip()


def git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )
    return done.stdout if done.returncode == 0 else ""


_SLUG: list[str] = []


def repo_slug() -> str:
    """`owner/name`, asked once. Every command here makes at least one API call."""
    if not _SLUG:
        _SLUG.append(
            gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        )
    return _SLUG[0]


class Ticket:
    """A ticket as GitHub holds it. Read through the *issues* endpoint deliberately.

    `repos/{slug}/issues/{n}` answers for pull requests too — a pull request is an
    issue — so one call covers both phases and the caller never has to decide which
    endpoint applies. `pull_request` present means it has been started;
    `pull_request.merged_at` means it has landed.
    """

    def __init__(self, number: int, payload: dict) -> None:
        self.number = number
        self.raw = payload
        self.title_full = str(payload.get("title") or "")
        match = _TITLE.match(self.title_full)
        self.title_number = int(match.group(1)) if match else None
        self.sigil = match.group(2) if match else None
        self.title = match.group(3) if match else self.title_full
        self.body = str(payload.get("body") or "")
        block = self.body.split("```")[1] if "```" in self.body else ""
        self.fields = {k: v for k, v in _FIELD.findall(block)}

    @classmethod
    def fetch(cls, number: int) -> Ticket:
        raw = gh("api", f"repos/{repo_slug()}/issues/{number}")
        try:
            return cls(number, json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            raise SystemExit(f"could not read ticket {number} from GitHub") from None

    @property
    def status(self) -> str:
        return self.fields.get("status", "?")

    @property
    def started(self) -> bool:
        return bool(self.raw.get("pull_request"))

    @property
    def merged(self) -> bool:
        return bool((self.raw.get("pull_request") or {}).get("merged_at"))

    @property
    def state(self) -> str:
        return str(self.raw.get("state") or "?").upper()

    def phase_sigil(self) -> str:
        return "!" if self.started else "#"

    def titled(self) -> str:
        return f"{self.number}{self.phase_sigil()} {self.title}"


SKELETON = """```
status:   in-progress
kind:     feature
source:   /task-planning {today}
axis:     correctness
found:    {today}
saving:   -
risk:     low — describe why
```

## The finding

TODO — the finding and its evidence.

## Area of impact

| File | Line | What changes |
|---|---|---|
| TODO | — | — |

## Approach

**Chosen:** TODO.

### Rejected alternatives

- TODO — and why not.

## Plan

1. TODO

## Verification

- TODO
"""


def _patch(number: int, **fields: str) -> None:
    args = ["api", "-X", "PATCH", f"repos/{repo_slug()}/issues/{number}"]
    for key, value in fields.items():
        args += ["-f", f"{key}={value}"]
    gh(*args)


def cmd_open(args) -> int:
    """File the specification. No branch, no commit, no worktree.

    The number arrives with the issue, so the title carries its sigil immediately:
    `X#`, a specification. `start` changes that to `X!` when the same object becomes
    the change.
    """
    slug = args.slug.strip().strip("-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug or ""):
        raise SystemExit(
            f"slug {args.slug!r} must be kebab-case: [a-z0-9-], e.g. isolate-sessions"
        )

    today = datetime.now().strftime("%Y-%m-%d")
    title = slug.replace("-", " ").capitalize()
    url = gh(
        "issue", "create", "--title", title, "--body", SKELETON.format(today=today)
    )
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    _patch(number, title=f"{number}# {title}")
    print(f"  opened {number}#  {url}")
    print(f"  fill it in:  ticket.py show {number} > <scratch>/t.md")
    print(f"               ticket.py push {number} <scratch>/t.md")
    return 0


def cmd_list(args) -> int:
    """The queue, in two phases: `X#` specified but not started, `X!` in flight.

    Both halves come from GitHub because there is nowhere else they could come from.
    The two `gh` subcommands are used for their filtering, not because the objects
    differ — the issues endpoint would return the pull requests as well.
    """
    rows: list[tuple[int, str, str, str]] = []
    specs = gh(
        "issue",
        "list",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,title",
        "-q",
        '.[] | "\\(.number)\\t\\(.title)"',
    )
    for line in specs.splitlines():
        number, _, title = line.partition("\t")
        if number.strip():
            rows.append((int(number), "#", "spec", title))
    flight = gh(
        "pr",
        "list",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,title,isDraft,headRefName",
        "-q",
        '.[] | "\\(.number)\\t\\(.isDraft)\\t\\(.headRefName)\\t\\(.title)"',
    )
    for line in flight.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            number, is_draft, head, title = parts
            phase = "draft" if is_draft == "true" else "ready"
            rows.append((int(number), "!", phase, f"{title}   [{head}]"))
    if not rows:
        print("  no open tickets")
        return 0
    for number, sigil, phase, title in sorted(rows):
        shown = _TITLE.match(title)
        name = title if not shown else f"{shown.group(3)}{title[shown.end() :]}"
        if not shown:
            name = f"{title}   (no sigil)"
        print(f"  {number:>4}{sigil}  {phase:<6} {name}")
    return 0


def cmd_show(args) -> int:
    t = Ticket.fetch(args.number)
    print(f"# {t.titled()}\n")
    print(t.body)
    return 0


def cmd_push(args) -> int:
    """Replace the body from a file you wrote, and re-derive the title's sigil.

    There is no merge of local and remote: the file is a working copy the author has
    just produced, and the body becomes whatever it says. `show` is how you get the
    current text to start from.
    """
    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"{path}: no such file")
    t = Ticket.fetch(args.number)
    body = path.read_text(encoding="utf-8")
    # A `show` round-trip prepends the title as a heading; strip it back off, or the
    # body accumulates one copy of the title per edit.
    lines = body.splitlines()
    if lines and _TITLE.match(lines[0].lstrip("# ").strip()):
        body = "\n".join(lines[1:]).lstrip("\n")
    _patch(t.number, title=t.titled(), body=body)
    print(f"  updated {t.number}{t.phase_sigil()}  ({len(body)} bytes)")
    return 0


def cmd_start(args) -> int:
    """Convert the issue into a pull request — the same object, so the same number.

    `POST /pulls` with an `issue` parameter is what makes `X#` and `X!` one
    identifier rather than two numbers kept in step by hand. It needs a pushed branch
    with at least one commit `main` does not have; GitHub has nothing to open a pull
    request against otherwise.
    """
    t = Ticket.fetch(args.number)
    if t.started:
        raise SystemExit(
            f"{t.number}! is already a pull request — `start` converts an issue, and "
            f"this one has been converted"
        )
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch in ("main", "master", ""):
        raise SystemExit(
            f"on {branch or 'no branch'} — `start` opens a pull request from the "
            f"branch you are on, so take a worktree and commit your work first"
        )
    ahead = git("rev-list", "--count", f"origin/main..{branch}").strip()
    if ahead in ("", "0"):
        raise SystemExit(
            f"{branch} has no commits origin/main does not, so GitHub has nothing to "
            f"open a pull request against. Commit and push, then start."
        )
    gh(
        "api",
        "-X",
        "POST",
        f"repos/{repo_slug()}/pulls",
        "-F",
        f"issue={args.number}",
        "-f",
        f"head={branch}",
        "-f",
        "base=main",
        "-F",
        f"draft={'true' if args.draft else 'false'}",
    )
    _patch(args.number, title=f"{args.number}! {t.title}")
    print(f"  {args.number}# became {args.number}!  from {branch}")
    return 0


def cmd_check(args) -> int:
    """Validate the ticket GitHub holds. There is nothing local to check.

    Three things can be wrong, where the file-backed version had to police one number
    appearing in three places: the title can lack its sigil, the sigil can contradict
    the phase the object is actually in, and the front block can be incomplete.
    """
    t = Ticket.fetch(args.number)
    problems: list[str] = []
    where = str(t.number)

    if t.title_number is None:
        problems.append(
            f"{where}: title {t.title_full!r} carries no sigil — it should read "
            f"'{t.number}{t.phase_sigil()} <Title>'"
        )
    else:
        if t.title_number != t.number:
            problems.append(
                f"{where}: title says {t.title_number} but this is ticket {t.number}"
            )
        if t.sigil != t.phase_sigil():
            claimed, actually = (
                ("#", "a pull request") if t.started else ("!", "still an issue")
            )
            problems.append(
                f"{where}: titled with {claimed!r} but it is {actually} — "
                f"{'start' if not t.started else 'push'} fixes the sigil"
            )

    for field in REQUIRED:
        if field not in t.fields:
            problems.append(f"{where}: body is missing '{field}:'")
    if t.fields and t.status not in STATUSES:
        problems.append(f"{where}: status {t.status!r} not one of {STATUSES}")
    kind = t.fields.get("kind")
    if kind and kind not in KINDS:
        problems.append(f"{where}: kind {kind!r} not one of {KINDS}")
    if t.status in ("done", "dropped") and t.state == "OPEN" and not t.merged:
        problems.append(
            f"{where}: {t.status} but still open — `close {t.number}` records the "
            f"outcome, and then it is merged or closed"
        )

    for problem in problems:
        print(f"  FAIL  {problem}")
    if problems:
        return 1
    print(f"  ok — {t.number}{t.sigil} is well-formed")
    return 0


def cmd_close(args) -> int:
    """Record the outcome in the body, then merge.

    The body is the durable record, so this is the last chance to make it say what
    happened rather than what was planned. It refuses once the thing is merged: the
    record would then be a plan nobody goes back to correct, and no local copy
    remains to rewrite it from.
    """
    t = Ticket.fetch(args.number)
    if not t.started:
        raise SystemExit(
            f"{t.number}# was never started, so nothing landed. Either `start "
            f"{t.number}` and land it, or set `status: dropped` and close the issue."
        )
    if t.merged:
        raise SystemExit(
            f"{t.number}! is already merged, so this is too late — the body still "
            f"describes the plan and no local copy remains to rewrite it from. Edit "
            f"the body on GitHub by hand."
        )

    dropped = t.status == "dropped"
    body = re.sub(
        r"^status:(\s*).*$",
        lambda m: f"status:{m.group(1)}{'dropped' if dropped else 'done'}",
        t.body,
        count=1,
        flags=re.MULTILINE,
    )
    _patch(t.number, title=t.titled(), body=body)
    verb = "Dropped" if dropped else "Landed"
    print(f"  {verb} {t.number}! — body rewritten as the final record")
    if dropped:
        print(f"  now:  gh pr close {t.number} --comment 'Dropped — see the body'")
    else:
        print(f"  now:  gh pr merge {t.number} --squash --delete-branch")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    opener = sub.add_parser("open")
    opener.add_argument("slug", help="kebab-case title, e.g. isolate-sessions")
    opener.set_defaults(func=cmd_open)

    sub.add_parser("list").set_defaults(func=cmd_list)

    shower = sub.add_parser("show")
    shower.add_argument("number", type=int)
    shower.set_defaults(func=cmd_show)

    pusher = sub.add_parser("push")
    pusher.add_argument("number", type=int)
    pusher.add_argument("file", help="the body to upload; keep it in the scratchpad")
    pusher.set_defaults(func=cmd_push)

    starter = sub.add_parser("start")
    starter.add_argument("number", type=int)
    starter.add_argument("--draft", action="store_true")
    starter.set_defaults(func=cmd_start)

    checker = sub.add_parser("check")
    checker.add_argument("number", type=int)
    checker.set_defaults(func=cmd_check)

    closer = sub.add_parser("close")
    closer.add_argument("number", type=int)
    closer.set_defaults(func=cmd_close)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
