"""Review gate — a panel you ran cannot be quietly ignored.

**The critic panel is opt-in.** It costs roughly 26k tokens per agent spawned, so
it runs when someone asks for it — "run the critics" before a merge — and not
otherwise. A merge with no panel on record proceeds untouched.

What this gate still guarantees is the other half: once critics *have* judged the
commit you are about to merge, a `block` stops it. Running the panel and merging
over its objection takes a deliberate act (`clear`, or another commit), not
silence.

Two jobs in one file:

* **hook mode** (no arguments, JSON on stdin): a `PreToolUse` hook. On a merge or a
  ticket close it looks for a verdict **for that MR**; no verdict is fine, a
  blocking verdict is not.
* **`record`**: how the panel's verdicts get written, after the agents have run.

    review_gate.py record --ref 41 \
        --paths $(git diff --name-only origin/main...HEAD) \
        --verdict risk=pass --verdict test=pass
    review_gate.py show
    review_gate.py clear

**`--ref` is the MR number, and it is the only number in play.** It used to mean
two different sequences depending on `--scope`: a GitHub PR number for `pr-merge`
and our own ticket number for `ticket-close`, while admission keyed on
`(scope, ref)`. One work item therefore needed two panels recorded under two
numbering schemes to land. Now that `X#` and `X!` are the same number — the MR's —
closing the ticket and merging the MR are two steps of landing one reviewed change,
and one recorded verdict admits both.

A verdict is bound to **the commit that would actually merge** — resolved from the
PR itself at record time, not read off the local checkout. Push another commit and
any objection stops applying, because the panel judged something that is no longer
what would merge. An objection recorded by another session still stands against
that session's work, not yours — it judged somebody else's diff (ticket 008).

Binding to local `HEAD` was the original design and it was wrong in a way that
emptied the gate: the two agree only when the PR's branch happens to be checked
out, so from `main` a recorded `block` was keyed on the *base* commit, the lookup
missed it, and the merge proceeded. The one guarantee this gate still makes was
bypassed by standing in a different directory. Admission is therefore keyed on
`(scope, ref)`, and the sha decides only whether the verdict has been superseded.

When `gh` cannot resolve the PR, `record` falls back to local `HEAD` and writes
`"binding": "local-head"` rather than refusing (ADR 2026-08-08). The weaker
guarantee is *named*: such an artifact speaks for the commit it was recorded on and
for no other, and both `record` and `show` say so out loud.

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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _shell import program, segments  # noqa: E402  (same directory; see the insert)

FALLBACK_REPO = Path(__file__).resolve().parents[2]

# A verdict of `revise` is not a failure — it is a note the author answers.
# Only `block` stops the gate. `n/a` is a lane the rule registry says this diff
# cannot violate: recorded so the panel's coverage stays legible, never spawned.
# One scope, because one work item now has one number: `X#` is the ticket and `X!`
# the MR, so `gh pr merge 41` and `ticket.py close` are two steps of landing the
# same reviewed change and share a verdict.
SCOPE = "land"

BLOCKING = {"block"}
VALID = {"pass", "revise", "block", "n/a"}
REQUIRED = {"invariants", "risk", "test"}

# Flags of `gh pr merge` that consume the token after them, so a value is never
# mistaken for the PR number. Short list, and stable — `gh pr merge`'s other flags
# (`--squash`, `--admin`, `--auto`, `--delete-branch`, …) are all standalone.
GH_MERGE_VALUE_FLAGS = {
    "-b",
    "--body",
    "-F",
    "--body-file",
    "-t",
    "--subject",
    "--match-head-commit",
    "--author-email",
}


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


def rev_parse(repo: Path, rev: str) -> str | None:
    done = subprocess.run(
        ["git", "rev-parse", rev],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() or None if done.returncode == 0 else None


def same_commit(a: str | None, b: str | None) -> bool:
    """Prefix comparison, because one side is abbreviated and the other is not.

    `git rev-parse --short` picks a length that keeps the sha unambiguous *in this
    repository*, while `gh` returns the full 40-character oid. Comparing them for
    equality would report every commit as different, which would silently mark
    every verdict superseded — the gate failing open while looking like it works.
    """
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def pr_head(repo: Path, ref: str) -> tuple[str, str] | None:
    """`(head oid, head branch)` for a PR, or `None` if `gh` cannot say.

    Called only at record time, where a network round-trip is affordable — the
    author has just spent ~100k tokens on the panel. The hook must never do this:
    a `PreToolUse` call has a 20s budget, and a gate that intermittently times out
    is a gate people switch off.
    """
    done = subprocess.run(
        ["gh", "pr", "view", str(ref), "--json", "headRefOid,headRefName"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        return None
    try:
        payload = json.loads(done.stdout)
        return str(payload["headRefOid"]), str(payload["headRefName"])
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        return None


# ---- hook mode -------------------------------------------------------------


def _ref_after(rest: list[str], value_flags: set[str]) -> str | None:
    """The first bare number in `rest`, skipping flags and the values they take.

    A PR or ticket number is what binds a verdict to the thing being merged, so
    reading it out of the wrong argv position is worse than not reading it: the
    gate would look up a verdict for something nobody asked about. Only a plain
    integer counts — `--match-head-commit <sha>` and a branch name are not refs
    this gate can key on, and `None` falls back to the weaker lookups in `hook()`.
    """
    skip = False
    for token in rest:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            skip = token in value_flags
            continue
        if token.isdigit():
            return token
    return None


def ticket_mr(repo: Path) -> str | None:
    """The MR number from the ticket file on this branch, or `None`.

    `ticket.py close` takes no argument any more — it acts on the one ticket on the
    branch — so the number has to be read rather than parsed off argv. A local file
    read is affordable where `pr_head`'s network call is not: this runs inside a
    `PreToolUse` hook with a 20s budget.

    Every failure path returns `None`, which falls back to the weaker lookups in
    `matching()`. A hook that raises stops every shell call in the session.
    """
    try:
        tickets = repo / ".claude" / "context" / "tickets"
        for path in sorted(tickets.glob("*.md")):
            if path.name == "README.md":
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("mr:"):
                    value = line.split(":", 1)[1].strip().lstrip("#")
                    return value if value.isdigit() else None
    except Exception:
        return None
    return None


def guarded(command: str, repo: Path | None = None) -> tuple[str, str | None] | None:
    """`(scope, ref)` for a command this gate judges, else `None`.

    There is one scope, `land`, because there is now one number. `gh pr merge 41`
    and `ticket.py close` are two steps of landing MR 41, so a panel recorded for it
    admits both rather than needing to be recorded twice.

    Classified from **argv positions**, never by searching the command text. The
    regex this replaced was `\\bgh\\b[^\\n]*\\bpr\\b[^\\n]*\\bmerge\\b`, which was
    simultaneously too broad and too narrow (ticket 003): it blocked a `gh pr
    create` whose *body prose* contained the word "merge" — nothing was being
    merged — and the workaround was `--body-file`, which is a guard teaching people
    to route around it. Tokenising fixes both ends: quoted prose is one opaque
    token, and `merge` only counts in the action position.
    """
    for argv in segments(command):
        if not argv:
            continue
        name = program(argv[0])
        words = [t for t in argv[1:] if not t.startswith("-")]
        if name == "gh" and words[:2] == ["pr", "merge"]:
            after = argv[argv.index("merge") + 1 :]
            return "land", _ref_after(after, GH_MERGE_VALUE_FLAGS)
        # `ticket.py close`, however it is invoked — python, a venv interpreter, or
        # directly. The script path is an argv token, so prose cannot fake it.
        if any(t.replace("\\", "/").endswith("ticket.py") for t in argv):
            if "close" in words:
                after = argv[argv.index("close") + 1 :]
                # A bare number is still honoured if someone types one, but `close`
                # no longer takes an argument, so normally the MR comes from the
                # ticket file on the branch.
                ref = _ref_after(after, set())
                if ref is None and repo is not None:
                    ref = ticket_mr(repo)
                return "land", ref
    return None


def artifacts(repo: Path) -> list[dict]:
    """Every readable verdict in this checkout. Unreadable ones are skipped.

    A corrupt artifact must not raise: this runs inside a `PreToolUse` hook, and
    an exception here stops every shell call in the session.
    """
    out: list[dict] = []
    reviews = reviews_dir(repo)
    for path in sorted(reviews.glob("*.json")) if reviews.is_dir() else []:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError, OSError):
            continue
    return out


def matching(repo: Path, scope: str, ref: str | None) -> list[dict]:
    """The verdicts that speak to *this* merge or close.

    Lookup is by `(scope, ref)` — the hole this closes is that `cmd_record` stored
    `ref` and `hook()` never read it, so admission was keyed on the local `HEAD`
    alone. Standing anywhere but the PR's branch, a recorded `block` simply was not
    found and the merge proceeded: the one thing this gate still enforces was
    bypassed by `cd`.

    With no ref in the command (`gh pr merge` merges the current branch) fall back
    to artifacts recorded on this branch, then to the commit we are standing on —
    the old behaviour, so this path is never weaker than what it replaced.
    """
    here = artifacts(repo)
    if ref is not None:
        return [
            r for r in here if r.get("scope") == scope and str(r.get("ref")) == str(ref)
        ]
    branch = branch_of(repo)
    on_branch = [
        r for r in here if r.get("scope") == scope and r.get("branch") == branch
    ]
    if on_branch:
        return on_branch
    return [r for r in here if same_commit(str(r.get("sha") or ""), head(repo))]


def superseded(repo: Path, record: dict) -> bool:
    """Has the thing this verdict judged moved on since it was recorded?

    A new commit clears an objection: the panel judged something that is no longer
    what would merge. That is deliberate and predates this ticket — it is why a
    verdict is bound to a sha at all.

    The comparison is **offline**, because the hook cannot afford a network call.
    For a `pr-head` binding the recorded head branch is resolved locally, remote
    tracking ref first; in the ordinary workflow you push from this machine and
    then merge, so `origin/<branch>` is current. When neither ref resolves the
    answer is *not superseded* — the one place this file fails closed, and it is
    the right direction, because a `block` is already on record and the alternative
    is discarding it on the strength of a lookup that did not work.
    """
    sha = str(record.get("sha") or "")
    if record.get("binding") == "pr-head":
        branch = record.get("head_branch")
        for rev in (f"refs/remotes/origin/{branch}", str(branch)) if branch else ():
            resolved = rev_parse(repo, rev)
            if resolved:
                return not same_commit(resolved, sha)
        return False
    # `local-head` (and any artifact written before bindings were named) attests to
    # the checkout only — per the 2026-08-08 ADR it may speak for the commit it was
    # recorded on and must not speak for any other.
    return not same_commit(head(repo), sha)


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
    # The repo is resolved first because `guarded` may need it: `ticket.py close`
    # carries no number, so the MR is read from the ticket file on that branch.
    repo = repo_root(event.get("cwd"))
    what = guarded(command, repo)
    if what is None:
        return 0
    scope, ref = what
    # No panel was run, and that is allowed. The critics are opt-in — they cost
    # ~26k tokens per spawn, so they run when someone asks for them, not on every
    # merge. What this hook still enforces is that a panel you *did* run cannot be
    # ignored, and it now finds that panel by the PR it judged rather than by
    # whichever commit this checkout happens to be sitting on.
    live = [r for r in matching(repo, scope, ref) if not superseded(repo, r)]
    blocked = sorted(
        {
            lane
            for r in live
            for lane, value in r.get("verdicts", {}).items()
            if value in BLOCKING
        }
    )
    if blocked:
        where = f"{ref}!" if ref else scope
        shas = ", ".join(sorted({str(r.get("sha", "?")) for r in live}))
        print(
            f"BLOCKED: critics were run on {where} ({shas}) and returned `block` "
            f"on {', '.join(blocked)}.\nFix the findings and commit — the verdict "
            f"is bound to the commit that would merge, so a new commit clears the "
            f"objection. To merge over it deliberately: review_gate.py clear.",
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
            f"{args.ref!r} — a lane may only be carried forward within one MR"
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

    # Bind to the commit that would actually merge, not to whatever this checkout
    # is standing on. Those coincide only when the MR's branch happens to be
    # checked out; from `main` the verdict used to attest to the *base*, and a
    # recorded `block` was then invisible to the merge it was about.
    #
    # This used to be conditional: `ticket-close` bound to local `HEAD` instead, on
    # the reasoning that closing a ticket described work that had already landed. It
    # no longer does. `close` now runs on the ticket's own branch in the commit
    # *before* the merge, so the MR's head is the right referent for both — and one
    # binding rule beats a special case whose justification has expired.
    binding, head_branch = "local-head", None
    resolved = pr_head(repo, args.ref)
    if resolved:
        oid, head_branch = resolved
        binding = "pr-head"
        # Abbreviated for the filename and the printed line; `same_commit` compares
        # by prefix, so this never has to match git's chosen length.
        sha = oid[:7]
    else:
        sha = head(repo)
        print(
            f"  note: `gh` could not resolve MR {args.ref} — binding this "
            f"verdict to local HEAD only. It will speak for this commit and "
            f"no other.",
            file=sys.stderr,
        )
    session = owning_session(repo)
    owners = lease_holders(repo)
    reviews_dir(repo).mkdir(parents=True, exist_ok=True)
    verdict_path(repo, sha).write_text(
        json.dumps(
            {
                "sha": sha,
                "scope": SCOPE,
                "ref": args.ref,
                # Which guarantee this artifact carries, named rather than implied:
                # `pr-head` was resolved from the PR, `local-head` is the weaker
                # fallback for an unreachable `gh` (ADR 2026-08-08). `show` prints
                # it, and `hook()` treats the two differently.
                "binding": binding,
                "head_branch": head_branch,
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
    bound = "the MR head" if binding == "pr-head" else "local HEAD only"
    print(
        f"  recorded {sha}  {args.ref}!  — {state}, {attribution}"
        f"\n  bound to {bound} ({binding})"
    )
    return 1 if blocked else 0


def cmd_show(args) -> int:
    repo = repo_root(os.getcwd())
    sha = head(repo)
    print(f"  HEAD {sha}  on {branch_of(repo)}  in {repo}")

    # Report every artifact in this checkout, not just the one keyed on local HEAD.
    # `hook()` admits by `(scope, ref)`, so a `show` that only looked at HEAD would
    # go quiet about the very verdict about to block a merge — the same drift
    # between what the gate does and what it says that step 0 fixed. Getting this
    # wrong is easy: the first version of this function listed the other verdicts
    # and *then* printed "nothing will be blocked" underneath a live block.
    everything = artifacts(repo)
    if not everything:
        # A missing panel is the ordinary state since the opt-in redesign, not a
        # fault. The pre-opt-in text promised a block the gate had long stopped
        # delivering, which is worse than saying nothing: it invites trusting the
        # gate for a guarantee it never makes. Exit 0 for the same reason — a
        # scripted `show` must not read the normal case as a failure.
        print("  no panel on record in this checkout — nothing will be blocked")
        print("  a `block` recorded here would stop the merge it names; none is")
        return 0

    stops: list[str] = []
    for record in sorted(everything, key=lambda r: str(r.get("sha") or "")):
        mine = same_commit(str(record.get("sha") or ""), sha)
        binding = record.get("binding", "local-head")
        blocked = sorted(
            k for k, v in record.get("verdicts", {}).items() if v in BLOCKING
        )
        stale = superseded(repo, record)
        where = f"{record.get('ref')}!"
        print(
            f"    {'HEAD ' if mine else '     '}{where} @{record.get('sha')} "
            f"[{binding}]{'  — superseded' if stale else ''}"
        )
        for lane, value in record.get("verdicts", {}).items():
            print(f"      {lane:<12} {value}")
        session = record.get("session")
        print(
            f"      recorded {record.get('at')} by session "
            f"{session[:8] if session else '— (unattributed)'}"
        )
        print(
            f"      bound to the PR head ({record.get('head_branch')})"
            if binding == "pr-head"
            else "      bound to local HEAD only — speaks for that commit, no other"
        )
        if blocked and not stale:
            stops.append((where, ", ".join(blocked)))

    for where, lanes in stops:
        print(f"  a merge of {where} is BLOCKED on {lanes}")
    if not stops:
        print(
            "  nothing blocking — every verdict on record is clear or superseded, "
            "so a merge proceeds"
        )
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
    # No `--scope`: there is one number and one thing it refers to. The flag existed
    # to say which sequence `--ref` was counted in, and that question is gone.
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
