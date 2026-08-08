"""Ticket tool for TCGinventory — local files are the truth, issues mirror them.

    ticket.py list                    every ticket, its status and its issue
    ticket.py next                    the number a new ticket should take
    ticket.py claim <kebab-title>     reserve the next number by creating the file
    ticket.py check                   non-zero if a ticket is malformed or out of sync
    ticket.py push [N ...]            create/update the GitHub issue for a ticket
    ticket.py close N                 close the issue and remove the ticket file

The local file under ``.claude/context/tickets/`` is canonical *while the work is
outstanding*: it is versioned, it is readable offline, and an agent can act on it
with no network and no auth. The GitHub issue is a mirror for visibility. When
they disagree, the file wins.

**The directory holds live work only.** ``close`` pushes the ticket's final text
to its issue, closes the issue and then deletes the file, so a listing is the
outstanding queue rather than a growing pile of finished ones. The record
survives in the closed issue and in ``git log``; the number is never handed out
again, because ``taken_numbers()`` reads every ref including deleted files.

Needs ``gh`` authenticated for ``push``/``close``; everything else is offline.
``gh`` installs into the *machine* PATH, so a shell opened before the install
will not see it — this resolves the absolute path as a fallback.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
TICKETS = REPO / ".claude" / "context" / "tickets"
README = TICKETS / "README.md"
GH_FALLBACK = Path(r"C:\Program Files\GitHub CLI\gh.exe")

STATUSES = ("open", "in-progress", "deferred", "done", "dropped")
KINDS = ("optimization", "dead-code", "feature", "fix")
REQUIRED = ("status", "kind", "source", "issue", "axis", "found", "saving", "risk")

_NAME = re.compile(r"^(\d{3})-(.+)\.md$")
_FIELD = re.compile(r"^(\w+):\s*(.*?)\s*$", re.MULTILINE)


class Ticket:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.number = int(_NAME.match(path.name).group(1))
        text = path.read_text(encoding="utf-8")
        self.text = text
        heading = text.splitlines()[0].lstrip("# ").strip()
        # "001 — Store card embeddings as float16" -> the part after the number.
        self.title = heading.split("—", 1)[-1].strip() if "—" in heading else heading
        block = text.split("```")[1] if "```" in text else ""
        self.fields = {k: v for k, v in _FIELD.findall(block)}

    @property
    def status(self) -> str:
        return self.fields.get("status", "?")

    @property
    def issue(self) -> int | None:
        raw = self.fields.get("issue", "none")
        return int(raw.lstrip("#")) if raw.lstrip("#").isdigit() else None

    def body(self) -> str:
        """The issue body: everything after the front block, plus a pointer back."""
        parts = self.text.split("```", 2)
        rest = parts[2] if len(parts) > 2 else self.text
        rel = self.path.relative_to(REPO).as_posix()
        meta = "\n".join(
            f"- **{k}**: {self.fields[k]}" for k in REQUIRED if k in self.fields
        )
        return (
            f"{meta}\n\n---\n{rest.strip()}\n\n---\n"
            f"Mirrored from `{rel}`, which is the source of truth. "
            f"Edit the file, then `/sdd-ticket push {self.number}`.\n"
        )

    def set_field(self, key: str, value: str) -> None:
        pattern = re.compile(rf"^({key}:\s*).*$", re.MULTILINE)
        self.text = pattern.sub(rf"\g<1>{value}", self.text, count=1)
        self.path.write_text(self.text, encoding="utf-8")
        self.fields[key] = value


def load() -> list[Ticket]:
    if not TICKETS.is_dir():
        return []
    return sorted(
        (Ticket(p) for p in TICKETS.glob("*.md") if _NAME.match(p.name)),
        key=lambda t: t.number,
    )


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


def cmd_list(args) -> int:
    tickets = load()
    if not tickets:
        print("  no tickets")
        return 0
    for t in tickets:
        issue = f"#{t.issue}" if t.issue else "—"
        print(
            f"  {t.number:03d}  {t.status:<12} {t.fields.get('kind', '?'):<13} "
            f"{issue:<5}  {t.title}"
        )
    return 0


def git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )
    return done.stdout if done.returncode == 0 else ""


def taken_numbers() -> set[int]:
    """Every ticket number ever used, on **any** ref — not just this checkout.

    Two sessions each asked the local directory for the next number, both got
    `007`, and both filed an issue. The local tickets directory only knows about
    tickets that have been merged into the branch you are standing on, so a ticket
    filed an hour ago on someone else's branch is invisible to it.

    `git log --all` covers every local and remote ref, including files since
    deleted: a number that was ever used is never handed out again.
    """
    numbers = {t.number for t in load()}
    listing = git(
        "log",
        "--all",
        "--name-only",
        "--pretty=format:",
        "--",
        TICKETS.relative_to(REPO).as_posix(),
    )
    for line in listing.splitlines():
        match = _NAME.match(Path(line.strip()).name)
        if match:
            numbers.add(int(match.group(1)))
    return numbers


def next_number() -> int:
    taken = taken_numbers()
    return (max(taken) + 1) if taken else 1


def cmd_next(args) -> int:
    print(f"{next_number():03d}")
    return 0


SKELETON = """# {number:03d} — {title}

```
status:   open
kind:     feature
source:   /task-planning {today}
issue:    none
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


def reservations_dir() -> Path:
    """Where claimed numbers are recorded, shared by every worktree of the repo.

    The git common directory, for the same reason the session registry lives there
    (`.claude/hooks/_session.py`): each worktree has its own checkout of
    `.claude/context/`, so a marker written there would be invisible to the other
    session and prove nothing. This is also never committed — the *tickets* are the
    durable record; these are just high-water marks.
    """
    done = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    common = done.stdout.strip() if done.returncode == 0 else ""
    root = Path(common) if common else REPO / ".git"
    if not root.is_absolute():
        root = REPO / root
    return root / "tcgi-ticket-numbers"


def cmd_claim(args) -> int:
    """Reserve a *number*, so two sessions cannot both file `007`.

    `next` is a read: between one session reading `007` and writing its file,
    another session reads `007` too. The reservation has to key on the **number
    alone** — keying it on the filename lets `007-foo.md` and `007-bar.md` both
    succeed, which is the collision rather than a fix for it (caught by
    `test_claim_steps_over_a_number_taken_between_read_and_write`).

    Two layers: a glob rules out numbers already present in this checkout, and an
    `O_EXCL` marker in the shared git dir closes the window between two concurrent
    claims. Whoever loses gets `FileExistsError` and takes the next number.
    """
    slug = args.slug.strip().strip("-")
    if not slug:
        raise SystemExit("give a kebab-case title, e.g. `claim isolate-sessions`")

    today = datetime.now().strftime("%Y-%m-%d")
    title = slug.replace("-", " ").capitalize()
    markers = reservations_dir()
    markers.mkdir(parents=True, exist_ok=True)

    start = next_number()
    for candidate in range(start, start + 50):
        if any(TICKETS.glob(f"{candidate:03d}-*.md")):
            continue  # taken in this checkout, whatever its title
        try:
            handle = os.open(
                markers / f"{candidate:03d}", os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
        except FileExistsError:
            continue  # another session got this number first
        os.close(handle)

        path = TICKETS / f"{candidate:03d}-{slug}.md"
        path.write_text(
            SKELETON.format(number=candidate, title=title, today=today),
            encoding="utf-8",
        )
        _reindex()
        print(f"  claimed {candidate:03d}  {path.relative_to(REPO).as_posix()}")
        print("  fill it in, then `ticket.py check` and `ticket.py push`")
        return 0
    raise SystemExit(f"could not claim a number near {start:03d}")


def cmd_check(args) -> int:
    problems: list[str] = []
    seen: dict[int, Path] = {}
    for t in load():
        where = t.path.name
        for field in REQUIRED:
            if field not in t.fields:
                problems.append(f"{where}: missing '{field}:'")
        if t.status not in STATUSES:
            problems.append(f"{where}: status {t.status!r} not one of {STATUSES}")
        kind = t.fields.get("kind")
        if kind and kind not in KINDS:
            problems.append(f"{where}: kind {kind!r} not one of {KINDS}")
        if t.number in seen:
            problems.append(f"{where}: number reused from {seen[t.number].name}")
        seen[t.number] = t.path
        # The directory is the outstanding queue. A ticket edited to `done` or
        # `dropped` by hand never went through `close`, so its issue is still open
        # and still describes the work as pending.
        if t.status in ("done", "dropped"):
            problems.append(
                f"{where}: {t.status} but still on disk — "
                f"`close {t.number}` to mirror it and remove the file"
            )
        elif t.status == "in-progress" and not t.issue:
            problems.append(f"{where}: in-progress but never mirrored to an issue")
        if README.is_file() and f"]({t.path.name})" not in README.read_text(
            encoding="utf-8"
        ):
            problems.append(f"{where}: missing from the README index")

    for problem in problems:
        print(f"  FAIL  {problem}")
    if problems:
        return 1
    print(f"  ok — {len(seen)} ticket(s), all well-formed and indexed")
    return 0


def _repo_slug() -> str:
    return gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")


def cmd_push(args) -> int:
    wanted = set(args.numbers or [])
    slug = _repo_slug()
    for t in load():
        if wanted and t.number not in wanted:
            continue
        title = f"[{t.number:03d}] {t.title}"
        labels = [t.fields.get("kind", "")] if t.fields.get("kind") else []
        if t.issue:
            gh(
                "issue",
                "edit",
                str(t.issue),
                "--repo",
                slug,
                "--title",
                title,
                "--body",
                t.body(),
            )
            print(f"  updated #{t.issue}  ({t.path.name})")
        else:
            cmd = [
                "issue",
                "create",
                "--repo",
                slug,
                "--title",
                title,
                "--body",
                t.body(),
            ]
            for label in labels:
                cmd += ["--label", label]
            url = gh(*cmd, check=False)
            if not url.startswith("http"):  # label may not exist yet
                url = gh(
                    "issue",
                    "create",
                    "--repo",
                    slug,
                    "--title",
                    title,
                    "--body",
                    t.body(),
                )
            number = url.rstrip("/").rsplit("/", 1)[-1]
            t.set_field("issue", f"#{number}")
            print(f"  created #{number}  {url}")
            _reindex()
    return 0


def cmd_close(args) -> int:
    """Mirror the finished ticket to its issue, close the issue, delete the file.

    The order matters. Deleting first, or closing the issue without re-pushing the
    body, would drop the outcome the ticket was edited to record — the commit, the
    PR, the measured after — leaving a closed issue that still describes the work
    as pending. The push is what makes the issue a faithful record before the file
    that fed it goes away.
    """
    for t in load():
        if t.number != args.number:
            continue
        if not t.issue:
            raise SystemExit(
                f"{t.path.name}: no mirrored issue, and closing deletes the file — "
                f"the issue would be the only surviving copy. "
                f"Run `push {t.number}` first."
            )
        # A ticket already edited to `dropped` is abandoned, not landed: keep that
        # word, or the record says the work shipped when it never did.
        dropped = t.status == "dropped"
        t.set_field("status", "dropped" if dropped else "done")
        slug = _repo_slug()
        gh(
            "issue",
            "edit",
            str(t.issue),
            "--repo",
            slug,
            "--title",
            f"[{t.number:03d}] {t.title}",
            "--body",
            t.body(),
        )
        tail = (
            "The issue body above is the final record — the ticket file is removed "
            "on close, and its history is in `git log`."
        )
        # `gh issue close` fails on an issue that is already closed, and that is a
        # normal state to be in: the PR body said "closes #N", or a previous `close`
        # died after this point. Failing here would strand the file forever.
        state = gh(
            "issue",
            "view",
            str(t.issue),
            "--repo",
            slug,
            "--json",
            "state",
            "-q",
            ".state",
            check=False,
        )
        if state.strip().upper() == "CLOSED":
            print(f"  #{t.issue} was already closed — updated its body")
        else:
            close_cmd = [
                "issue",
                "close",
                str(t.issue),
                "--repo",
                slug,
                "--comment",
                (f"Dropped. {tail}" if dropped else f"Landed. {tail}"),
            ]
            if dropped:
                close_cmd += ["--reason", "not planned"]
            gh(*close_cmd)
        name = t.path.name
        t.path.unlink()
        _reindex()
        # Staging a path that no longer exists records the deletion; explicit
        # pathspecs, because `git add -A` is denied while a tree is shared.
        gone = t.path.relative_to(REPO).as_posix()
        index = README.relative_to(REPO).as_posix()
        print(f"  closed #{t.issue} and removed {name}")
        print(f"  commit it:  git add {gone} {index}")
        return 0
    raise SystemExit(f"no ticket {args.number:03d}")


def _reindex() -> None:
    """Rewrite the README index table from the ticket files themselves."""
    if not README.is_file():
        return
    rows = [
        f"| [{t.number:03d}]({t.path.name}) | {t.title} "
        f"| {t.fields.get('kind', '?')} | "
        f"{('#' + str(t.issue)) if t.issue else '—'} | `{t.status}` |"
        for t in load()
    ]
    text = README.read_text(encoding="utf-8")
    head, _, _ = text.partition("| # | title |")
    README.write_text(
        head
        + "| # | title | kind | issue | status |\n|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("next").set_defaults(func=cmd_next)
    claimer = sub.add_parser("claim")
    claimer.add_argument("slug", help="kebab-case title, e.g. isolate-sessions")
    claimer.set_defaults(func=cmd_claim)
    sub.add_parser("check").set_defaults(func=cmd_check)
    pusher = sub.add_parser("push")
    pusher.add_argument("numbers", nargs="*", type=int)
    pusher.set_defaults(func=cmd_push)
    closer = sub.add_parser("close")
    closer.add_argument("number", type=int)
    closer.set_defaults(func=cmd_close)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
