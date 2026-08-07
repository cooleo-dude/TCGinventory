"""Ticket tool for TCGinventory — local files are the truth, issues mirror them.

    ticket.py list                    every ticket, its status and its issue
    ticket.py next                    the number a new ticket should take
    ticket.py check                   non-zero if a ticket is malformed or out of sync
    ticket.py push [N ...]            create/update the GitHub issue for a ticket
    ticket.py close N                 mark done and close the mirrored issue

The local file under ``.claude/context/tickets/`` is canonical: it is versioned,
it is readable offline, and an agent can act on it with no network and no auth.
The GitHub issue is a mirror for visibility. When they disagree, the file wins.

Needs ``gh`` authenticated for ``push``/``close``; everything else is offline.
``gh`` installs into the *machine* PATH, so a shell opened before the install
will not see it — this resolves the absolute path as a fallback.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
TICKETS = REPO / ".claude" / "context" / "tickets"
README = TICKETS / "README.md"
GH_FALLBACK = Path(r"C:\Program Files\GitHub CLI\gh.exe")

STATUSES = ("open", "in-progress", "done", "dropped")
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


def cmd_next(args) -> int:
    tickets = load()
    print(f"{(tickets[-1].number + 1) if tickets else 1:03d}")
    return 0


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
        if t.status == "done" and not t.issue:
            problems.append(f"{where}: done but never mirrored to an issue")
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
    for t in load():
        if t.number != args.number:
            continue
        t.set_field("status", "done")
        _reindex()
        if t.issue:
            gh(
                "issue",
                "close",
                str(t.issue),
                "--repo",
                _repo_slug(),
                "--comment",
                "Landed. See the ticket file for the measured result.",
            )
            print(f"  closed #{t.issue} and marked {t.path.name} done")
        else:
            print(f"  marked {t.path.name} done (no mirrored issue)")
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
