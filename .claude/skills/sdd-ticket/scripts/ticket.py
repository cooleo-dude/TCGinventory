"""Ticket tool for TCGinventory — the merge request *is* the ticket.

    ticket.py open <kebab-title>   branch commit, push, draft MR; X is its number
    ticket.py list                 every open ticket, read from GitHub
    ticket.py check                non-zero if the ticket on this branch is malformed
    ticket.py push                 rewrite the MR title and body from the file
    ticket.py close                record the outcome and remove the file, then merge

**One work item, one number.** ``X`` is the GitHub pull-request number: ``X#``
names the specification and ``X!`` names the change that satisfies it. They are
the same object, so they cannot drift apart.

That is not a convention this script maintains, it is the only arrangement
arithmetic allows. GitHub allocates issues and pull requests from a single
per-repo counter, so a work item that files **both** an issue and a PR can never
have them agree — measured on this repo, issues took 2/7/10/12/13/14/16/17/22/
31/32/36 while PRs took 1/3/4/5/6/8/9/11/15/18…, one interleaved sequence with no
overlap. Retiring the issue is what lets the two numbers become one.

Consequences worth knowing before you use it:

* **GitHub allocates, so nothing needs locking.** The old ``claim`` existed
  because ticket numbers were minted locally and two sessions could both read
  ``007``; it defended that with ``taken_numbers()`` over every ref plus
  ``O_EXCL`` marker files in the git common dir. A number issued by GitHub cannot
  collide, so all of it is gone.
* **``open`` needs network and writes to the branch.** It commits the ticket file
  and pushes, because ``gh pr create`` has nothing to open an MR against
  otherwise. It stages by explicit pathspec — never ``git add -A`` — so it is safe
  in a shared tree.
* **The ticket file lives on its branch and must not reach ``main``.** ``close``
  removes it in a final commit *before* the merge, so the merge leaves nothing to
  clean up. The durable record is the MR body, which ``close`` rewrites from the
  file a moment before deleting it.

Needs ``gh`` authenticated for everything except ``check``. ``gh`` installs into
the *machine* PATH, so a shell opened before the install will not see it — this
resolves the absolute path as a fallback.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
TICKETS = REPO / ".claude" / "context" / "tickets"
GH_FALLBACK = Path(r"C:\Program Files\GitHub CLI\gh.exe")

STATUSES = ("open", "in-progress", "deferred", "done", "dropped")
KINDS = ("optimization", "dead-code", "feature", "fix")
REQUIRED = ("status", "kind", "source", "mr", "axis", "found", "saving", "risk")

# A numbered ticket, and the transient state between writing the file and hearing
# back which number GitHub gave it. Four digits and beyond are allowed: the number
# is GitHub's counter now, and it will pass 999 eventually.
_NAME = re.compile(r"^(\d{3,})-(.+)\.md$")
_DRAFT = re.compile(r"^draft-(.+)\.md$")
_FIELD = re.compile(r"^(\w+):\s*(.*?)\s*$", re.MULTILINE)
# "# 41# — The merge request is the ticket". An en/em dash or a plain hyphen is
# accepted on read; `open` always writes the em dash.
_HEADING = re.compile(r"^#\s+(\d+)#\s+[—–-]\s+(.+?)\s*$")
_MR_TITLE = re.compile(r"^(\d+)!\s+(.+?)\s*$")


class Ticket:
    def __init__(self, path: Path) -> None:
        self.path = path
        match = _NAME.match(path.name)
        self.number = int(match.group(1)) if match else None
        self.slug = match.group(2) if match else _DRAFT.match(path.name).group(1)
        text = path.read_text(encoding="utf-8")
        self.text = text
        first = text.splitlines()[0] if text.strip() else ""
        head = _HEADING.match(first)
        # The sigil in the heading is what `check` compares against the filename
        # and the `mr:` field, so keep them separable rather than parsed away.
        self.heading_number = int(head.group(1)) if head else None
        self.title = head.group(2) if head else first.lstrip("# ").strip()
        block = text.split("```")[1] if "```" in text else ""
        self.fields = {k: v for k, v in _FIELD.findall(block)}

    @property
    def status(self) -> str:
        return self.fields.get("status", "?")

    @property
    def mr(self) -> int | None:
        raw = self.fields.get("mr", "none").lstrip("#")
        return int(raw) if raw.isdigit() else None

    def mr_title(self) -> str:
        return f"{self.number}! {self.title}"

    def body(self) -> str:
        """The MR body: the front block as prose, then everything after it."""
        parts = self.text.split("```", 2)
        rest = parts[2] if len(parts) > 2 else self.text
        rel = self.path.relative_to(REPO).as_posix()
        meta = "\n".join(
            f"- **{k}**: {self.fields[k]}" for k in REQUIRED if k in self.fields
        )
        return (
            f"{meta}\n\n---\n{rest.strip()}\n\n---\n"
            f"Generated from `{rel}` on this branch, which is the source of truth "
            f"while the MR is open. Edit the file, then `/sdd-ticket push`. "
            f"`close` rewrites this body and removes the file, so what you are "
            f"reading is the durable record once the MR is merged.\n"
        )

    def set_field(self, key: str, value: str) -> None:
        pattern = re.compile(rf"^({key}:\s*).*$", re.MULTILINE)
        self.text = pattern.sub(rf"\g<1>{value}", self.text, count=1)
        self.path.write_text(self.text, encoding="utf-8")
        self.fields[key] = value

    def set_heading(self, number: int) -> None:
        lines = self.text.splitlines()
        lines[0] = f"# {number}# — {self.title}"
        self.text = "\n".join(lines) + "\n"
        self.path.write_text(self.text, encoding="utf-8")
        self.heading_number = number


def load() -> list[Ticket]:
    """Every ticket file in this checkout — normally exactly one, yours."""
    if not TICKETS.is_dir():
        return []
    found = [
        Ticket(p)
        for p in sorted(TICKETS.glob("*.md"))
        if _NAME.match(p.name) or _DRAFT.match(p.name)
    ]
    return sorted(found, key=lambda t: (t.number is None, t.number or 0))


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


def git_checked(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}\n{done.stderr.strip()}")
    return done.stdout.strip()


def branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD").strip()


SKELETON = """# {title}

```
status:   in-progress
kind:     feature
source:   /task-planning {today}
mr:       none
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


def cmd_open(args) -> int:
    """Write the ticket, push it, and let GitHub name it.

    The number cannot be known before the MR exists, so this is ordered: file,
    commit, push, create, *then* rename to the number that came back. The window
    where the file is called `draft-<slug>.md` is why `_DRAFT` exists.
    """
    slug = args.slug.strip().strip("-")
    if not slug:
        raise SystemExit("give a kebab-case title, e.g. `open isolate-sessions`")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise SystemExit(f"slug {slug!r} must be kebab-case: [a-z0-9-]")

    here = branch()
    if here in ("main", "master"):
        raise SystemExit(
            f"on {here} — a ticket is a branch with an MR. Take a worktree first "
            f"(EnterWorktree), then run this again."
        )
    existing = load()
    if existing:
        raise SystemExit(
            f"{existing[0].path.name} is already on this branch — one ticket per "
            f"MR. Finish or `close` it before opening another."
        )

    today = datetime.now().strftime("%Y-%m-%d")
    title = slug.replace("-", " ").capitalize()
    draft = TICKETS / f"draft-{slug}.md"
    draft.write_text(SKELETON.format(title=title, today=today), encoding="utf-8")

    rel = draft.relative_to(REPO).as_posix()
    git_checked("add", "--", rel)
    git_checked("commit", "-m", f"Tickets: file the brief for {slug}", "--", rel)
    git_checked("push", "-u", "origin", here)

    url = gh("pr", "create", "--draft", "--title", slug, "--body-file", str(draft))
    number = int(url.rstrip("/").rsplit("/", 1)[-1])

    final = TICKETS / f"{number:03d}-{slug}.md"
    git_checked("mv", rel, final.relative_to(REPO).as_posix())
    ticket = Ticket(final)
    ticket.set_heading(number)
    ticket.set_field("mr", str(number))
    gh("pr", "edit", str(number), "--title", ticket.mr_title())

    # `git mv` has already staged both sides of the rename, and `git add` on the
    # old path fails outright ("pathspec did not match any files") now that it is
    # gone from the working tree. Commit the pair by pathspec instead — never
    # `git add -A`, which is denied while a tree is shared.
    paths = [rel, final.relative_to(REPO).as_posix()]
    git_checked("commit", "-m", f"Tickets: number the brief {number}#", "--", *paths)
    git_checked("push")

    print(f"  opened {number}#  {final.relative_to(REPO).as_posix()}")
    print(f"  draft MR {number}!  {url}")
    print("  fill it in, then `ticket.py push` to mirror it")
    return 0


def cmd_list(args) -> int:
    """The queue is GitHub's open MRs — `main` carries no ticket files.

    Reading the local directory would only ever show the ticket on the branch you
    are standing on, which is not a queue.
    """
    raw = gh(
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
    rows = [line.split("\t", 3) for line in raw.splitlines() if line.strip()]
    if not rows:
        print("  no open tickets")
        return 0
    for number, is_draft, head, title in sorted(rows, key=lambda r: int(r[0])):
        sigil = _MR_TITLE.match(title)
        state = "draft" if is_draft == "true" else "ready"
        name = sigil.group(2) if sigil else f"{title}   (no sigil)"
        print(f"  {int(number):>4}!  {state:<6} {head:<28}  {name}")
    return 0


def cmd_check(args) -> int:
    problems: list[str] = []
    tickets = load()
    for t in tickets:
        where = t.path.name
        for field in REQUIRED:
            if field not in t.fields:
                problems.append(f"{where}: missing '{field}:'")
        if t.status not in STATUSES:
            problems.append(f"{where}: status {t.status!r} not one of {STATUSES}")
        kind = t.fields.get("kind")
        if kind and kind not in KINDS:
            problems.append(f"{where}: kind {kind!r} not one of {KINDS}")

        if t.number is None:
            # Only legitimate between `open`'s push and its rename. Committed, it
            # means an `open` died halfway and the MR is unnumbered.
            problems.append(
                f"{where}: still a draft — no number. `open` assigns one from the "
                f"MR it creates; if that MR exists, rename the file to it."
            )
        else:
            # The three places the number appears must agree, or `push` retitles
            # one MR from a file describing another.
            if t.heading_number is None:
                problems.append(
                    f"{where}: heading must carry the sigil, '# {t.number}# — Title'"
                )
            elif t.heading_number != t.number:
                problems.append(
                    f"{where}: heading says {t.heading_number}# but the filename "
                    f"says {t.number}"
                )
            if t.mr is None:
                problems.append(
                    f"{where}: 'mr:' is not a number — it is the MR that lands this"
                )
            elif t.mr != t.number:
                problems.append(
                    f"{where}: mr: {t.mr} disagrees with the filename ({t.number}) "
                    f"— X# and X! are the same number"
                )

        # The file is removed by `close`, in the commit before the merge. Edited to
        # a terminal status by hand, it would ride into `main` and the MR body
        # would still describe the work as pending.
        if t.status in ("done", "dropped"):
            problems.append(
                f"{where}: {t.status} but still on disk — `close` mirrors it to the "
                f"MR and removes it, and must run before the merge"
            )

    for problem in problems:
        print(f"  FAIL  {problem}")
    if problems:
        return 1
    print(f"  ok — {len(tickets)} ticket(s) on this branch, well-formed")
    return 0


def _only_ticket() -> Ticket:
    tickets = load()
    if not tickets:
        raise SystemExit(
            "no ticket file on this branch — `open <slug>` files one, or switch to "
            "the branch whose ticket you meant"
        )
    if len(tickets) > 1:
        names = ", ".join(t.path.name for t in tickets)
        raise SystemExit(f"more than one ticket on this branch: {names}")
    return tickets[0]


def cmd_push(args) -> int:
    t = _only_ticket()
    if t.number is None or t.mr is None:
        raise SystemExit(f"{t.path.name}: no MR number yet — see `check`")
    gh(
        "pr",
        "edit",
        str(t.mr),
        "--title",
        t.mr_title(),
        "--body",
        t.body(),
    )
    print(f"  updated {t.mr}!  ({t.path.name})")
    return 0


def cmd_close(args) -> int:
    """Record the outcome in the MR, then remove the file — before the merge.

    Order and timing both matter. The body is rewritten first so the MR carries
    the finished text rather than the brief; the file is then deleted in the same
    branch, so merging leaves `main` with no ticket files to sweep up later. Run
    after the merge and both guarantees are gone: the file is already on `main`,
    and removing it needs a second PR — which is the overhead this replaced.
    """
    t = _only_ticket()
    if t.number is None or t.mr is None:
        raise SystemExit(f"{t.path.name}: no MR number yet — see `check`")

    state = (
        gh("pr", "view", str(t.mr), "--json", "state", "-q", ".state", check=False)
        .strip()
        .upper()
    )
    dropped = t.status == "dropped"
    if state == "MERGED":
        raise SystemExit(
            f"{t.mr}! is already merged, so {t.path.name} is on main and this is "
            f"too late: the MR body still describes the brief, and deleting the "
            f"file now needs its own PR. Record the outcome in the MR by hand, "
            f"then remove the file in your next change."
        )
    if state == "CLOSED" and not dropped:
        raise SystemExit(
            f"{t.mr}! is closed without merging, but the ticket says "
            f"{t.status!r}. If the work was abandoned set `status: dropped` and "
            f"say why; otherwise reopen the MR."
        )

    # A ticket already edited to `dropped` is abandoned, not landed: keep that
    # word, or the record says the work shipped when it never did.
    t.set_field("status", "dropped" if dropped else "done")
    gh("pr", "edit", str(t.mr), "--title", t.mr_title(), "--body", t.body())

    rel = t.path.relative_to(REPO).as_posix()
    # `-f` because `set_field` above rewrote `status:` in the working tree, and git
    # refuses to remove a file with unstaged modifications. Discarding them is the
    # intent: the edit existed only to reach the MR body, which is already pushed.
    git_checked("rm", "--quiet", "-f", "--", rel)
    verb = "Dropped" if dropped else "Landed"
    git_checked(
        "commit",
        "-m",
        f"Tickets: close {t.number}#, recorded in {t.mr}! ({verb.lower()})",
        "--",
        rel,
    )
    git_checked("push")

    print(f"  {verb} {t.number}# — MR body rewritten, {t.path.name} removed")
    if dropped:
        print(f"  now:  gh pr close {t.mr} --comment 'Dropped — see the body'")
    else:
        print(f"  now:  gh pr merge {t.mr} --squash --delete-branch")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    opener = sub.add_parser("open")
    opener.add_argument("slug", help="kebab-case title, e.g. isolate-sessions")
    opener.set_defaults(func=cmd_open)
    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("push").set_defaults(func=cmd_push)
    sub.add_parser("close").set_defaults(func=cmd_close)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
