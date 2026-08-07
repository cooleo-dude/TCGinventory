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

A verdict is bound to a commit sha. Push another commit and the gate reopens,
because the panel judged something that is no longer what would merge.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REVIEWS = REPO / ".claude" / "context" / "reviews"

# A verdict of `revise` is not a failure — it is a note the author answers.
# Only `block` stops the gate.
BLOCKING = {"block"}
VALID = {"pass", "revise", "block"}

# What the gate guards. Matched against the command text of a Bash/PowerShell call.
GUARDED = (
    (re.compile(r"\bgh\b[^\n]*\bpr\b[^\n]*\bmerge\b"), "pr-merge"),
    (re.compile(r"ticket\.py[^\n]*\bclose\b"), "ticket-close"),
)


def head() -> str:
    done = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() or "unknown"


def verdict_path(sha: str) -> Path:
    return REVIEWS / f"{sha}.json"


# ---- hook mode -------------------------------------------------------------


def guarded_scope(command: str) -> str | None:
    for pattern, scope in GUARDED:
        if pattern.search(command):
            return scope
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

    sha = head()
    path = verdict_path(sha)
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

    sha = head()
    REVIEWS.mkdir(parents=True, exist_ok=True)
    verdict_path(sha).write_text(
        json.dumps(
            {
                "sha": sha,
                "scope": args.scope,
                "ref": args.ref,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "verdicts": verdicts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    blocked = [k for k, v in verdicts.items() if v in BLOCKING]
    state = f"BLOCKED on {', '.join(blocked)}" if blocked else "clear"
    print(f"  recorded {sha}  {args.scope} {args.ref}  — {state}")
    return 1 if blocked else 0


def cmd_show(args) -> int:
    sha = head()
    path = verdict_path(sha)
    print(f"  HEAD {sha}")
    if not path.is_file():
        print("  no review on record — the gate will block a merge or a close")
        return 1
    record = json.loads(path.read_text(encoding="utf-8"))
    for lane, value in record["verdicts"].items():
        print(f"    {lane:<12} {value}")
    print(f"  {record['scope']} {record['ref']}, recorded {record['at']}")
    return 0


def cmd_clear(args) -> int:
    removed = 0
    for path in REVIEWS.glob("*.json") if REVIEWS.is_dir() else []:
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
    recorder.set_defaults(func=cmd_record)
    sub.add_parser("show").set_defaults(func=cmd_show)
    sub.add_parser("clear").set_defaults(func=cmd_clear)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
