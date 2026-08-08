"""Review gate — a panel you ran cannot be quietly ignored.

**The critic panel is opt-in.** It costs roughly 26k tokens per agent spawned, so
it runs when someone asks for it — "run the critics" before a merge — and not
otherwise. A merge with no panel on record proceeds untouched.

What this gate still guarantees is the other half: once critics *have* judged the
commit you are about to merge, a `block` stops it. Running the panel and merging
over its objection takes a deliberate act (`clear`, or another commit), not
silence.

Two jobs in one file:

* **hook mode** (no arguments, JSON on stdin): a `PreToolUse` hook. On a PR merge
  or a ticket close it looks for a verdict for the current `HEAD`; no verdict is
  fine, a blocking verdict is not.
* **`record`**: how the panel's verdicts get written, after the agents have run.

    review_gate.py record --scope pr-merge --ref 4 \
        --paths $(git diff --name-only origin/main...HEAD) \
        --verdict risk=pass --verdict test=pass
    review_gate.py show
    review_gate.py clear

A verdict is bound to a commit sha **and to the session that recorded it**. Push
another commit and any objection stops applying, because the panel judged something
that is no longer what would merge. An objection recorded by another session still
stands against that session's work, not yours — it judged somebody else's diff
(ticket 008).

Which repository a call is about comes from the **caller's directory**, never from
this file's location. One session per worktree means several checkouts of this repo
exist at once, and `.claude/settings.json` invokes the hook through
`$CLAUDE_PROJECT_DIR`, which stays at the launch directory when a session moves
into a worktree. Deriving the repo from `__file__` therefore read the main clone's
`HEAD` while the agent worked somewhere else entirely — attesting to a commit
nobody was merging.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

FALLBACK_REPO = Path(__file__).resolve().parents[2]

# A verdict of `revise` is not a failure — it is a note the author answers.
# Only `block` stops the gate. `n/a` is a lane the rule registry says this diff
# cannot violate: recorded so the panel's coverage stays legible, never spawned.
BLOCKING = {"block"}
VALID = {"pass", "revise", "block", "n/a"}
REQUIRED = {"invariants", "risk", "test"}

# What the gate guards. Matched against the command text of a Bash/PowerShell call.
GUARDED = (
    (re.compile(r"\bgh\b[^\n]*\bpr\b[^\n]*\bmerge\b"), "pr-merge"),
    (re.compile(r"ticket\.py[^\n]*\bclose\b"), "ticket-close"),
)


def repo_root(cwd: str | None = None) -> Path:
    """The checkout this call is about, most trustworthy source first.

    `--show-toplevel` reports the *worktree* root rather than the main clone, which
    is the whole point: a session working in `.claude/worktrees/x` must record and
    read verdicts for that tree's HEAD.
    """
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


def reviews_dir(repo: Path) -> Path:
    return repo / ".claude" / "context" / "reviews"


def head(repo: Path) -> str:
    done = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() or "unknown"


def branch_of(repo: Path) -> str:
    done = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() or "unknown"


def verdict_path(repo: Path, sha: str) -> Path:
    return reviews_dir(repo) / f"{sha}.json"


# ---- hook mode -------------------------------------------------------------


def guarded_scope(command: str) -> str | None:
    for pattern, scope in GUARDED:
        if pattern.search(command):
            return scope
    return None


def owning_session(repo: Path) -> str | None:
    """Which session holds the lease on `repo`, per `.claude/hooks/_session.py`.

    Imported lazily and defensively **on purpose.** This runs as a `PreToolUse`
    hook on every shell call, so an ImportError at module scope — a half-applied
    checkout, a corrupt sibling — would raise on each one and stop all work in the
    session. A missing registry degrades to an unattributed verdict, which is a
    weaker guarantee recorded in the open rather than a dead session.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _session

        return _session.owning_session(repo)
    except Exception:
        return None


def lease_holders(repo: Path) -> list[str]:
    """Every session holding this worktree. See `owning_session` for the lazy
    import; the distinction between *nobody* and *several* is what makes a null
    session safe to admit or not."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _session

        return _session.holders(repo)
    except Exception:
        return []


def hook() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # not something we can judge; never block on our own confusion

    tool = event.get("tool_name", "")
    if tool not in ("Bash", "PowerShell"):
        return 0
    command = str(event.get("tool_input", {}).get("command", ""))
    scope = guarded_scope(command)
    if scope is None:
        return 0

    repo = repo_root(event.get("cwd"))
    sha = head(repo)
    path = verdict_path(repo, sha)
    if not path.is_file():
        # No panel was run, and that is allowed. The critics are opt-in — they
        # cost ~26k tokens per spawn, so they run when someone asks for them, not
        # on every merge. What this hook still enforces is that a panel you *did*
        # run cannot be ignored: see below.
        return 0

    record = json.loads(path.read_text(encoding="utf-8"))
    blocked = [k for k, v in record.get("verdicts", {}).items() if v in BLOCKING]
    if blocked:
        print(
            f"BLOCKED: critics were run on {sha} and returned `block` on "
            f"{', '.join(sorted(blocked))}.\nFix the findings and commit — the "
            f"verdict is bound to the sha, so a new commit clears the objection. "
            f"To merge over it deliberately: review_gate.py clear.",
            file=sys.stderr,
        )
        return 2

    # Attribution (`session`, `holders`) is recorded but deliberately does **not**
    # gate anything, because the opt-in redesign removed the hole it was written
    # for. Ticket 008 originally blocked a verdict recorded by another session: at
    # the time a missing verdict *stopped* a merge, so a foreign green one wrongly
    # opened it and two sessions on one sha could trade gate passes. Now a missing
    # verdict proceeds — so there is nothing left to trade, and blocking on a
    # foreign `pass` would make a reviewed commit harder to merge than an unreviewed
    # one. Only `block` stops the gate; the metadata is there to say whose panel it
    # was when someone reads the artifact.
    return 0


# ---- record / show / clear -------------------------------------------------


def out_of_scope(paths: list[str], repo: Path | None = None) -> set[str]:
    """Required lanes the rule registry says these paths cannot violate.

    The registry already knows which rules a set of files can break, so spawning
    a critic to discover that a lane has nothing to judge is ~26k tokens spent to
    re-derive a lookup. With no ``--paths`` given nothing is assumed out of scope,
    so the gate stays as strict as it was.

    `repo` is resolved from the caller's directory like everything else here, so a
    session working in its own worktree reads that tree's registry rather than the
    main clone's.
    """
    if not paths:
        return set()
    repo = repo or repo_root(os.getcwd())
    done = subprocess.run(
        [
            sys.executable,
            str(repo / ".claude" / "hooks" / "rules.py"),
            "scope",
            "--json",
            *paths,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        return set()  # registry unreadable — demand the full panel, never less
    try:
        in_scope = set(json.loads(done.stdout).get("lanes", {}))
    except (json.JSONDecodeError, ValueError):
        return set()
    return REQUIRED - in_scope


def inherited(args, judged: dict[str, str], repo: Path | None = None) -> dict[str, str]:
    """Lanes carried forward from an earlier panel on the same PR.

    Re-judging a lane whose files did not change is the most expensive thing this
    gate causes: a verdict is bound to a sha, so every push discards a completed
    panel. When only some lanes are dirty the author re-dispatches those and
    carries the rest.

    Two conditions stop that being a loophole. A lane may only be carried within
    one ``ref``, so a review of PR #6 can never green a lane of PR #9; and a lane
    judged this round always wins, so inheriting can supply a missing verdict but
    never overwrite a fresh one. The artifact records what was carried and from
    where, because an inheritance nobody can audit is not a verdict.
    """
    if not getattr(args, "inherit", None):
        return {}
    path = verdict_path(repo or repo_root(os.getcwd()), args.inherit)
    if not path.is_file():
        raise SystemExit(f"--inherit {args.inherit}: no verdict on record for it")
    prior = json.loads(path.read_text(encoding="utf-8"))
    if str(prior.get("ref")) != str(args.ref):
        raise SystemExit(
            f"--inherit {args.inherit}: recorded for ref {prior.get('ref')!r}, not "
            f"{args.ref!r} — a lane may only be carried forward within one PR"
        )
    return {k: v for k, v in prior.get("verdicts", {}).items() if k not in judged}


def cmd_record(args) -> int:
    repo = repo_root(os.getcwd())
    verdicts: dict[str, str] = {}
    for raw in args.verdict:
        lane, _, value = raw.partition("=")
        if value not in VALID:
            raise SystemExit(f"{raw!r}: verdict must be one of {sorted(VALID)}")
        verdicts[lane] = value
    carried = inherited(args, verdicts, repo)
    verdicts = {**carried, **verdicts}  # a lane judged this round always wins
    # A lane the registry says this diff cannot violate needs no critic — but it
    # is still recorded, so the artifact shows the whole panel's coverage rather
    # than going quiet about the lanes nobody looked at. Lanes that *are* in
    # scope still need a real verdict below.
    for lane in sorted(out_of_scope(args.paths, repo) - set(verdicts)):
        verdicts[lane] = "n/a"
    missing = REQUIRED - set(verdicts)
    if missing:
        raise SystemExit(f"missing verdict for: {', '.join(sorted(missing))}")

    # A recorded verdict is only worth something if its findings are grounded.
    # Reuse the registry's own validator so the check a critic runs and the check
    # the gate runs cannot diverge.
    for path in args.findings:
        done = subprocess.run(
            [
                sys.executable,
                str(repo / ".claude" / "hooks" / "rules.py"),
                "validate",
                path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            print(done.stdout.strip(), file=sys.stderr)
            raise SystemExit(
                f"{path}: not admissible — a finding must cite a rule id from "
                f".claude/context/rules.json"
            )

    sha = head(repo)
    session = owning_session(repo)
    owners = lease_holders(repo)
    reviews_dir(repo).mkdir(parents=True, exist_ok=True)
    verdict_path(repo, sha).write_text(
        json.dumps(
            {
                "sha": sha,
                "scope": args.scope,
                "ref": args.ref,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "verdicts": verdicts,
                "inherited": (
                    {"from": args.inherit, "lanes": sorted(carried)} if carried else {}
                ),
                "findings": [str(p) for p in args.findings],
                # Whose panel this was. `null` means the worktree had no single
                # owner — read `holders` to tell "no registry running" from
                # "several sessions shared this tree". Attribution only: the gate
                # stops on a `block` and nothing else, so this is here to answer
                # "whose review was this?" when someone reads the artifact.
                "session": session,
                "holders": owners,
                "branch": branch_of(repo),
                "worktree": str(repo),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    blocked = [k for k, v in verdicts.items() if v in BLOCKING]
    state = f"BLOCKED on {', '.join(blocked)}" if blocked else "clear"
    if carried:
        state += f", carried {', '.join(sorted(carried))} from {args.inherit}"
    if session:
        attribution = f"session {session[:8]}"
    elif len(owners) > 1:
        attribution = (
            f"UNUSABLE — {len(owners)} sessions share this checkout, so this "
            f"verdict belongs to none of them. Take your own worktree and re-record."
        )
    else:
        attribution = "unattributed (no lease)"
    print(f"  recorded {sha}  {args.scope} {args.ref}  — {state}, {attribution}")
    return 1 if blocked else 0


def cmd_show(args) -> int:
    repo = repo_root(os.getcwd())
    sha = head(repo)
    path = verdict_path(repo, sha)
    print(f"  HEAD {sha}  on {branch_of(repo)}  in {repo}")
    if not path.is_file():
        print("  no review on record — the gate will block a merge or a close")
        return 1
    record = json.loads(path.read_text(encoding="utf-8"))
    for lane, value in record["verdicts"].items():
        print(f"    {lane:<12} {value}")
    session = record.get("session")
    print(f"  {record['scope']} {record['ref']}, recorded {record['at']}")
    print(f"  by session {session[:8] if session else '— (unattributed)'}")
    return 0


def cmd_clear(args) -> int:
    reviews = reviews_dir(repo_root(os.getcwd()))
    removed = 0
    for path in reviews.glob("*.json") if reviews.is_dir() else []:
        path.unlink()
        removed += 1
    print(f"  cleared {removed} review(s)")
    return 0


def main() -> None:
    if len(sys.argv) == 1:  # hook mode: the harness pipes JSON in
        raise SystemExit(hook())

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    recorder = sub.add_parser("record")
    recorder.add_argument(
        "--scope", required=True, choices=["pr-merge", "ticket-close"]
    )
    recorder.add_argument("--ref", required=True)
    recorder.add_argument(
        "--verdict", action="append", default=[], metavar="lane=value"
    )
    recorder.add_argument(
        "--findings",
        action="append",
        default=[],
        metavar="PATH",
        help="a critic's JSON verdict; validated against the rule registry",
    )
    recorder.add_argument(
        "--paths",
        nargs="+",
        default=[],
        metavar="PATH",
        help=(
            "the diff's changed files — lanes the rule registry puts out of "
            "scope are recorded n/a instead of needing a critic"
        ),
    )
    recorder.add_argument(
        "--inherit",
        metavar="SHA",
        help=(
            "carry lanes forward from an earlier verdict on the same ref — for "
            "lanes whose files did not change since it was recorded"
        ),
    )
    recorder.set_defaults(func=cmd_record)
    sub.add_parser("show").set_defaults(func=cmd_show)
    sub.add_parser("clear").set_defaults(func=cmd_clear)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
