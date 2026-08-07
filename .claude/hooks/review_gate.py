"""Review gate — no merge and no ticket close without a green critic panel.

Two jobs in one file:

* **hook mode** (no arguments, JSON on stdin): a `PreToolUse` hook. If the tool
  call is a PR merge or a ticket close, it demands a fresh green verdict for the
  current `HEAD` and blocks the call when there is not one.
* **`record`**: how the panel's verdicts get written, after the agents have run.

The agents cannot trigger themselves — they are definitions the Agent tool
dispatches. This is the piece that makes "the panel runs before a merge" a fact
rather than a habit.

    review_gate.py record --scope pr-merge --ref 4 \
        --verdict invariants=pass --verdict risk=pass --verdict test=pass
    review_gate.py show
    review_gate.py clear

A verdict is bound to a commit sha **and to the session that recorded it**. Push
another commit and the gate reopens, because the panel judged something that is no
longer what would merge. Record from another session and the gate stays shut,
because that panel judged somebody else's work (ticket 007).

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
# Only `block` stops the gate.
BLOCKING = {"block"}
VALID = {"pass", "revise", "block"}

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
        print(
            f"BLOCKED: {scope} at {sha} has no review on record.\n"
            f"Dispatch the critic panel first — tcgi-invariant-critic, "
            f"tcgi-risk-critic and tcgi-test-critic (plus tcgi-ui-critic if the "
            f"diff touches ui/), or tcgi-mini-critic for a small diff — then:\n"
            f"  python .claude/hooks/review_gate.py record --scope {scope} "
            f"--ref <n> --verdict invariants=pass --verdict risk=pass "
            f"--verdict test=pass\n"
            f"See .claude/agents/ for what each critic judges.",
            file=sys.stderr,
        )
        return 2

    record = json.loads(path.read_text(encoding="utf-8"))
    blocked = [k for k, v in record.get("verdicts", {}).items() if v in BLOCKING]
    if blocked:
        print(
            f"BLOCKED: the panel returned `block` on {', '.join(sorted(blocked))} "
            f"for {sha}.\nFix the findings, commit, and re-run the panel — the "
            f"verdict is bound to the sha, so a new commit reopens the gate.",
            file=sys.stderr,
        )
        return 2

    # Whose review was it? Two sessions sitting on the same sha — both just
    # branched from main, neither has committed — otherwise trade verdicts: one
    # records green for its own diff and opens the gate for the other's unreviewed
    # work (ticket 007). A verdict recorded before the lease registry existed
    # carries no session and stays admissible; the guarantee is weaker and the
    # `session: null` in the artifact says so.
    recorded = record.get("session")
    caller = event.get("session_id")
    if recorded and caller and recorded != caller:
        print(
            f"BLOCKED: the verdict for {sha} was recorded by session "
            f"{recorded[:8]}, not by this one ({caller[:8]}).\n"
            f"That panel judged a different session's work. Two sessions on the "
            f"same commit would otherwise open the gate for each other.\n"
            f"Dispatch the panel for YOUR diff and record it, or work in your own "
            f"worktree — see `## Working in parallel sessions` in CLAUDE.md.",
            file=sys.stderr,
        )
        return 2
    return 0


# ---- record / show / clear -------------------------------------------------


def cmd_record(args) -> int:
    verdicts: dict[str, str] = {}
    for raw in args.verdict:
        lane, _, value = raw.partition("=")
        if value not in VALID:
            raise SystemExit(f"{raw!r}: verdict must be one of {sorted(VALID)}")
        verdicts[lane] = value
    missing = {"invariants", "risk", "test"} - set(verdicts)
    if missing:
        raise SystemExit(f"missing verdict for: {', '.join(sorted(missing))}")

    repo = repo_root(os.getcwd())

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
    reviews_dir(repo).mkdir(parents=True, exist_ok=True)
    verdict_path(repo, sha).write_text(
        json.dumps(
            {
                "sha": sha,
                "scope": args.scope,
                "ref": args.ref,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "verdicts": verdicts,
                "findings": [str(p) for p in args.findings],
                # Whose panel this was. `null` means no lease held this worktree at
                # record time, so the gate cannot tell sessions apart for this sha —
                # legible in the artifact rather than assumed.
                "session": session,
                "branch": branch_of(repo),
                "worktree": str(repo),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    blocked = [k for k, v in verdicts.items() if v in BLOCKING]
    state = f"BLOCKED on {', '.join(blocked)}" if blocked else "clear"
    attribution = f"session {session[:8]}" if session else "unattributed (no lease)"
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
    recorder.set_defaults(func=cmd_record)
    sub.add_parser("show").set_defaults(func=cmd_show)
    sub.add_parser("clear").set_defaults(func=cmd_clear)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
