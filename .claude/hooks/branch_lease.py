"""Branch guard — one session, one worktree, one branch.

Two jobs in one file, the same shape as `review_gate.py`:

* **hook mode** (no arguments, JSON on stdin): a `PreToolUse` hook. It refreshes
  this session's lease, and when *another* live session is working in the same
  checkout it denies the commands that would move that session's `HEAD` or sweep
  its files into this session's commit.
* **subcommands**: `show`, `claim`, `release [--all]` for looking at and unsticking
  the registry by hand.

    branch_lease.py show
    branch_lease.py release --all

## What it is defending against

A checkout has exactly one `HEAD`. Session A runs `git switch -c ticket/007`,
session B runs `git switch -c ticket/008`, and A's next commit lands on B's branch
and into B's pull request. Observed on 2026-08-03: an 831-line feature went in
under the commit message "Transaction: lock Reference/Counterparty width…" because
the other session's `git add -A && git commit` loop staged this session's files
between its own `add` and its `commit`. Undoing that needs a history rewrite that
races the other session's live commits.

## The one condition

**Another live lease on this same worktree.** That is the whole test. A session
alone in its own worktree may switch branches and `git add -A` as it likes —
nobody else's `HEAD` is here to move and nobody else's files are here to sweep. So
the remedy this prints is never "be careful", it is "take your own worktree".

## Why it tokenises instead of pattern-matching

Ticket 003 recorded that `review_gate.GUARDED` is "simultaneously too broad and too
narrow": a substring search over the command line blocked a `gh pr create` whose
*body prose* contained the word "merge", while missing a real merge invoked through
a shell variable. This guard fires on **every** shell call, so the same defect
would be worse here. Commands are tokenised with `shlex` — which respects quoting,
so a `;` inside `-m "fix; the thing"` is not a separator — then split on the
separator tokens and classified by `argv[0]` and the subcommand that follows it.

## Failure direction

Fail **open** on internal confusion (unparseable stdin, unbalanced quotes, no git,
a corrupt registry) and **closed** only on the condition actually detected. A
`PreToolUse` hook that raises when it is confused stops every shell call in the
session, which is a worse outcome than the collision it was added to prevent.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _session  # noqa: E402  (same directory; the path insert above is required)
from _shell import SEPARATORS, segments  # noqa: E402  (same)

FALLBACK_REPO = Path(__file__).resolve().parents[2]

__all__ = ["SEPARATORS", "segments"]

# Subcommands that move `HEAD` or rewrite the working tree under another session.
# `branch` is not here: creating a branch does not move `HEAD` — only the
# destructive flags below do.
TREE_MOVING = {
    "checkout",
    "switch",
    "reset",
    "rebase",
    "merge",
    # `pull` is fetch + merge (or rebase, under pull.rebase): it moves HEAD exactly
    # like the two subcommands above it. It was missed in the first cut of this set,
    # which is worth recording — it is a far more ordinary command than several that
    # were remembered.
    "pull",
    "cherry-pick",
    "revert",
    "stash",
    "restore",
    "apply",
    "am",
    "bisect",
    "clean",
}
BRANCH_DESTRUCTIVE = {"-d", "-D", "-m", "-M", "-f", "--delete", "--move", "--force"}

# Read-only modes of otherwise-destructive subcommands. Blocking `git stash list`
# would be the kind of false positive that gets a guard switched off entirely, and
# a guard nobody runs protects nothing.
READ_ONLY_MODES = {
    "stash": {"list", "show"},
    "bisect": {"log", "view", "visualize"},
    "clean": set(),  # handled by the dry-run flags below
    "apply": set(),
}
DRY_RUN = {"-n", "--dry-run", "--check", "--stat", "--summary"}

# Commands that change where the rest of a chain runs. A git command after one of
# these is not necessarily against the checkout we detected rivals in.
DIRECTORY_CHANGING = {"cd", "chdir", "pushd", "popd", "set-location", "sl"}

# Staging that cannot tell one session's files from another's.
STAGE_EVERYTHING = {"-A", "--all", "-u", "--update", ".", ":/", "*"}


def repo_root(cwd: str | None = None) -> Path:
    """The checkout this call is about — the caller's directory, not this file's."""
    for candidate in (cwd, os.environ.get("CLAUDE_PROJECT_DIR")):
        if not candidate or not Path(candidate).is_dir():
            continue
        done = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=candidate,
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode == 0 and done.stdout.strip():
            return Path(done.stdout.strip())
    return FALLBACK_REPO


# ---- command classification -------------------------------------------------


def _is_git(argv0: str) -> bool:
    name = Path(argv0).name.lower()
    return name in ("git", "git.exe")


def _subcommand(argv: list[str]) -> tuple[str | None, list[str]]:
    """The git subcommand and its arguments, skipping `git -C x`-style options."""
    index = 1
    while index < len(argv):
        token = argv[index]
        if token.startswith("-"):
            # `-C <path>` and `-c <k=v>` take a value; the rest are standalone.
            if token in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                index += 2
                continue
            index += 1
            continue
        return token, argv[index + 1 :]
    return None, []


def offence(argv: list[str]) -> str | None:
    """Why this one command is not safe beside another session, or `None`."""
    if not argv or not _is_git(argv[0]):
        return None
    subcommand, rest = _subcommand(argv)
    if subcommand is None:
        return None

    if subcommand in TREE_MOVING:
        modes = READ_ONLY_MODES.get(subcommand)
        first = next((token for token in rest if not token.startswith("-")), None)
        if modes is not None and (first in modes or any(f in DRY_RUN for f in rest)):
            return None  # a read dressed as a write: `git stash list`, `clean -n`
        return f"`git {subcommand}` moves HEAD or rewrites the working tree"

    if subcommand == "branch" and any(flag in BRANCH_DESTRUCTIVE for flag in rest):
        return "`git branch` with a delete/move/force flag rewrites shared refs"

    if subcommand == "add":
        if any(token in STAGE_EVERYTHING for token in rest):
            return "`git add` stages everything, including the other session's files"
        if not [token for token in rest if not token.startswith("-")]:
            return "`git add` with no pathspec cannot tell whose files it is staging"

    if subcommand == "commit":
        if any(flag in ("-a", "--all") for flag in rest):
            return "`git commit -a` commits the other session's edits too"
        if "--" not in rest:
            return (
                "`git commit` with no pathspec sweeps in whatever the other "
                "session has staged"
            )

    return None


def verdict(command: str) -> str | None:
    """The first offence in a chained command, or `None` if all of it is safe.

    **Stops judging at a directory change.** Rivals are detected against the
    directory the tool call starts in, but a chain can move: `cd <scratch> && git
    commit -m init` runs against a completely unrelated repository, and blocking it
    with "another session is working in this same checkout" is both false and
    unactionable — the remedies printed do not apply to a tree nobody holds a lease
    on. That was a real false positive, hit while reviewing this guard.

    Deliberately it does **not** try to work out where `cd` went. Tokenising is
    posix-mode, which eats Windows backslashes, so the target arrives as
    `C:UserscolelTemp...` — the reason this file reads subcommands and flags but
    never a path *value*. Comparing that against the worktree would be guesswork
    dressed as a check.

    The cost is a bypass: `cd . && git switch -c x` is not judged. That is an
    accepted trade, not an oversight — this guard exists to stop an unthinking
    `git switch`, and someone writing `cd .` first is not doing it unthinkingly.
    Failing open on a command we cannot locate is the same direction every other
    ambiguity in this file resolves.
    """
    for argv in segments(command):
        if argv and Path(argv[0]).name.lower().removesuffix(".exe") in (
            DIRECTORY_CHANGING
        ):
            return None
        reason = offence(argv)
        if reason:
            return reason
    return None


# ---- hook mode --------------------------------------------------------------


def hook() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never block on our own confusion

    repo = repo_root(event.get("cwd"))
    session = event.get("session_id")
    phase = event.get("hook_event_name", "PreToolUse")

    # A session ending gives its lease up, so the next one is not told it has a
    # rival that went home hours ago. Reaping covers a crash; this covers the
    # ordinary case immediately.
    if phase == "SessionEnd":
        if session:
            try:
                _session.release(repo, session)
            except Exception:
                pass
        return 0

    if not session:
        # No identity means no way to tell sessions apart, so no basis to deny.
        # The lease would be worth nothing — say nothing and get out of the way.
        return 0

    if phase == "SessionStart":
        try:
            _session.claim(repo, session)
        except Exception:
            pass
        return 0

    if event.get("tool_name") not in ("Bash", "PowerShell"):
        return 0

    try:
        _session.claim(repo, session)
        others = _session.rivals(repo, session)
    except Exception:
        return 0  # a registry we cannot read is not evidence of a rival

    if not others:
        return 0

    command = str(event.get("tool_input", {}).get("command", ""))
    reason = verdict(command)
    if reason is None:
        return 0

    who = ", ".join(
        f"{lease.get('session', '?')[:8]} on {lease.get('branch', '?')}"
        for lease in others
    )
    print(
        f"BLOCKED: {reason}.\n"
        f"Another session is working in this same checkout ({who}), and it has "
        f"exactly one HEAD — this command would move theirs or commit their files "
        f"under your message.\n\n"
        f"Take your own checkout instead: call EnterWorktree, which CLAUDE.md's "
        f"`## Working in parallel sessions` section authorises. Then everything "
        f"here is allowed again, because nothing you do reaches their tree.\n"
        f"Staging only your own files by explicit path (`git add <path>`, "
        f"`git commit -m ... -- <path>`) is also allowed right now.\n"
        f"If this is wrong — the other session is long gone — "
        f"`python .claude/hooks/branch_lease.py release --all`.",
        file=sys.stderr,
    )
    return 2


# ---- subcommands ------------------------------------------------------------


def cmd_show(args) -> int:
    repo = repo_root(os.getcwd())
    print(f"  registry  {_session.registry_dir(repo)}")
    print(f"  here      {_session.worktree_root(repo)}")
    live = _session.leases(repo)
    if not live:
        print("  no live leases")
        return 0
    for lease in live:
        mine = lease.get("worktree") == _session.worktree_root(repo)
        print(
            f"    {lease.get('session', '?')[:8]}  "
            f"{lease.get('branch', '?'):<28} "
            f"{'THIS TREE' if mine else lease.get('worktree', '?')}"
        )
    return 0


def cmd_claim(args) -> int:
    repo = repo_root(os.getcwd())
    lease = _session.claim(repo, args.session)
    print(f"  claimed {lease['session'][:8]} on {lease['branch']}")
    return 0


def cmd_release(args) -> int:
    repo = repo_root(os.getcwd())
    if args.all:
        print(f"  released {_session.release_all(repo)} lease(s)")
        return 0
    if not args.session:
        raise SystemExit("give --session <id>, or --all")
    print(
        f"  {'released' if _session.release(repo, args.session) else 'no lease for'} "
        f"{args.session[:8]}"
    )
    return 0


def main() -> None:
    if len(sys.argv) == 1:  # hook mode: the harness pipes JSON in
        raise SystemExit(hook())

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show").set_defaults(func=cmd_show)
    claimer = sub.add_parser("claim")
    claimer.add_argument("--session", required=True)
    claimer.set_defaults(func=cmd_claim)
    releaser = sub.add_parser("release")
    releaser.add_argument("--session")
    releaser.add_argument("--all", action="store_true")
    releaser.set_defaults(func=cmd_release)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
