# 007 — Isolate each Claude session on its own worktree and branch

```
status:   in-progress
kind:     feature
source:   /task-planning 2026-08-07
issue:    #17
axis:     correctness
found:    2026-08-07
saving:   -  (correctness of the branch/commit boundary between concurrent sessions)
risk:     medium — a PreToolUse guard fires on every shell call, so a bug here stops all work
```

## The finding

Several Claude sessions work this repo at once, and they share one checkout at
`C:\Users\colel\Documents\Development\TCGinventory`. **A checkout has exactly one
`HEAD`.** Since the repo moved to a branch + PR flow (2026-08-07), that single
`HEAD` is a shared mutable resource with no owner, and every stateful piece of the
`/sdd-*` corpus keys off it.

### Hole 1 — commits land on another session's branch

Session A runs `git switch -c ticket/007`. Session B runs `git switch -c
ticket/008`. A's next commit lands on `ticket/008` and into B's pull request.
Observed twice already, recorded in the `concurrent-agent-boundaries` memory: on
2026-08-03 an 831-line Recognition History feature went in under the commit
message *"Transaction: lock Reference/Counterparty width…"*, because the other
session's `git add -A && git commit` loop staged files between this session's
`add` and its `commit`. Undoing it needs a history rewrite that races the other
session's live commits, so the only safe response after the fact is to tell the
user.

`docs/decisions.md:1559` carries the cost in the record already — a documented
piece of work *deferred* "while those four files are under concurrent edit."

### Hole 2 — the review gate can go false-green across sessions

`review_gate.py` writes a verdict as `{sha, scope, ref, at, verdicts, findings}`
(`cmd_record`, `.claude/hooks/review_gate.py:152-167`) and looks it up by sha
alone (`hook()`, `:87-104`). **There is no session, branch or worktree in the
record.** So:

- Session A branches from `main`, dispatches the panel, records green. It has not
  committed yet, so the verdict is filed under *`main`'s* sha.
- Session B, on its own branch, still at that same sha, runs `ticket.py close`.
  The gate finds a green `<sha>.json` and opens.

B's work was never reviewed. The gate's real guarantee is *"somebody, somewhere,
recorded a verdict while standing on this commit"* — and it fails **open**.

This is **not** the same defect as ticket 003. 003 rekeys the **`pr-merge`** leg
to the PR head sha and, by explicit design (003's plan step 4), *keeps
`ticket-close` bound to local `HEAD`* because closing a ticket is a statement
about work that has already landed. That reasoning is right, and it is exactly
where the cross-session hole survives 003 untouched. The two fixes are
orthogonal: 003 answers *which commit*, this answers *whose review*.

### Hole 3 — the gate will read the wrong tree the moment worktrees are used

`REPO = Path(__file__).resolve().parents[2]` (`.claude/hooks/review_gate.py:33`),
and `.claude/settings.json` invokes the hook as
`$CLAUDE_PROJECT_DIR/.claude/hooks/review_gate.py`. When a session switches into
a worktree, `CLAUDE_PROJECT_DIR` stays at the launch directory — so the hook runs
the **main clone's** copy and `head()` returns the **main clone's** `HEAD` while
the agent works somewhere else entirely. The gate would attest to a commit nobody
is merging.

**This one is latent today and becomes live the instant the fix for hole 1 is
adopted.** It has to land first.

### Hole 4 — two sessions allocate the same ticket number

`cmd_next` is `max(local ticket numbers) + 1`
(`.claude/skills/sdd-ticket/scripts/ticket.py:121-124`). Two sessions both get
`007`, both write `007-*.md`, both `push`. `check` catches the reuse only once
both files exist, and by then two GitHub issues are live. Ticket numbers are our
own sequence, so nothing upstream dedupes them.

### Hole 5 — a release is hostage to a foreign working tree

`release.py cmd_check` refuses on any dirty tree
(`.claude/skills/sdd-release/scripts/release.py:94-99`) and cannot say whose dirt
it is. `cmd_build` already dodges this with a throwaway worktree — that lesson was
learned on 2026-08-07, when the tree held another session's half-finished
`history.py` and a build from it would have shipped an exe that crashed
constructing `MainWindow`.

### Why the existing mitigation is not enough

The `concurrent-agent-boundaries` memory already prescribes worktrees. It is
advisory, and it triggers on *detection* — "uncommitted `M` files you did not
make, a branch moving under you." Detection is late by construction: by the time
a branch has moved under you, the shared `HEAD` has already moved. The rule has to
be unconditional and enforced.

## Area of impact

| File | Line | What changes |
|---|---|---|
| `.gitignore` | 35 | add `.claude/worktrees/` — `EnterWorktree` creates worktrees there and it is **not** ignored today, so each session's worktree is untracked noise in every other session |
| `.claude/hooks/review_gate.py` | 33 | resolve the repo from the payload `cwd`, then `$CLAUDE_PROJECT_DIR`, then `__file__` |
| `.claude/hooks/review_gate.py` | 152-167 | record `session`, `branch`, `worktree` in the verdict |
| `.claude/hooks/review_gate.py` | 87-104 | refuse a verdict recorded by a different session |
| `.claude/hooks/branch_lease.py` | new | lease registry + `PreToolUse` guard |
| `.claude/settings.json` | 4-16 | register the guard; add `SessionStart` / `SessionEnd` |
| `.claude/skills/sdd-ticket/scripts/ticket.py` | 121-124 | `next` unions local + `origin/*` refs; new `claim` creates the stub `O_EXCL` |
| `.claude/skills/sdd-release/scripts/release.py` | 94-99 | `check` prints the lease table when the tree is dirty |
| `.claude/context/app-map.json` | 377 | `workflow.branch` is **stale**: "Commit directly to main. No PR / feature branch by default." |
| `CLAUDE.md` | — | new `## Working in parallel sessions` section, placed **outside** `## Non-negotiable architecture rules` |
| `.claude/skills/*/SKILL.md` | — | the worktree step and the new command shapes |
| `tests/test_session_lease.py` | new | guard matching, lease lifecycle, reaping |
| `tests/test_review_gate.py` | new | **there is no test for the gate at all today**; 003's plan step 1 asks for this file and inherits it |

**No migration.** Nothing under `src/tcginventory/` changes — no table, no
repository, no service, no Qt model, no view. The four SQLite files are untouched.

`rules.py scope` over this set dispatches **risk** and **test**. `record` demands
all three lanes, so the invariant critic runs regardless.

## Approach

**Chosen: one session = one worktree = one branch, enforced by a lease.**

Do not try to make branch-switching in a shared checkout safe. Remove the sharing.
Every session that intends to commit takes its own worktree *first*,
unconditionally — not in reaction to detecting a neighbour.

1. **The instruction.** `EnterWorktree` is permitted only when the user or
   CLAUDE.md/memory says so, so the CLAUDE.md section is what makes the tool
   usable, not documentation of a habit.
2. **The registry.** `$(git rev-parse --git-common-dir)/tcgi-sessions/<session_id>.json`,
   holding `{session, worktree, branch, pid, started, heartbeat, data_dir}`. The
   git common dir is the one location every worktree of the repo shares, is never
   committed, and is already machine-local.
3. **The guard.** A `PreToolUse` hook on `Bash|PowerShell` denies a
   branch-mutating git command when a live *foreign* lease holds this worktree,
   and denies `git add -A` / `add .` / `commit -a` / pathspec-less `git commit`
   whenever any foreign lease is live. Deny, not warn: a warning in a tool result
   is what a busy agent skims past.
4. **Match by tokenising, not by substring.** Split the command on `;`, `&&` and
   newlines, `shlex` each segment, check `argv[0]` is git and read the
   subcommand. Ticket 003 recorded that `review_gate.GUARDED`
   (`.claude/hooks/review_gate.py:42-45`) is "simultaneously too broad and too
   narrow" — it blocked a `gh pr create` whose *body prose* contained the word
   "merge". This guard fires on every shell call, so the same defect would be
   worse here than it was there.
5. **Fail open on internal confusion, closed on the detected condition.**
   `hook()` already returns `0` on unparseable stdin — "never block on our own
   confusion." A guard that hard-blocks every shell command when it has a bug
   bricks the session.
6. **Session-bind the gate**, closing hole 2 on both scopes without touching
   003's sha question.

### Rejected alternatives

- **A mutex/lock around branch switching in the shared checkout.** Serialises
  access to a resource that should not be shared. The second session still cannot
  work — it just waits instead of corrupting — and long-lived branch ownership
  means the lock is held for hours. A worktree lets both sessions actually run.
- **Rely on the memory's advisory rule.** What is in place now. It triggers on
  detection, which is inherently after the fact, and it has already failed twice
  in observable ways.
- **Add the worktree rule to `## Non-negotiable architecture rules`.** Nine
  `invariants` rules in `.claude/context/rules.json` seal that exact section
  (`MONEY-CENTS`, `BASIS-SPLIT`, `FIFO-DRAWDOWN`, `NO-HARD-DELETE`,
  `RIPPING-ZERO`, `MIGRATIONS-FORWARD-ONLY`, `QT-FREE-CORE`, `UI-NO-SQL`,
  `TEST-PROVES-BEHAVIOUR`). Adding a line breaks all nine seals and forces a
  resealing pass. Beyond the cost: a process rule about checkouts is not the same
  kind of claim as "money is integer cents", and diluting that section makes it
  easier to reseal casually — which is the one thing `rules.py` exists to prevent.
- **Isolate `release.py check` with a throwaway worktree, like `build`.** My own
  first proposal, dropped on reflection: once each session owns its checkout,
  `check`'s dirty-tree refusal is *correct*, and the only thing missing is knowing
  whose dirt blocked it. An `--ignore-foreign` escape hatch would re-introduce
  precisely the bug the check exists to catch.
- **Give the guard no manual override.** A guard with no escape hatch gets
  disabled wholesale at the first false positive. `branch_lease.py release --all`
  stays, documented.
- **Per-session `TCGINVENTORY_DATA_DIR` as a separate ticket.** Worktrees isolate
  code, not the app's SQLite files, and `rebuild-after-task` means sessions
  routinely boot the app. It is one field in the lease plus a line of docs, so it
  rides along here rather than becoming a ticket nobody files.

## Plan

1. `.gitignore`: add `.claude/worktrees/`. Must precede anything that creates one.
2. `review_gate.py`: resolve the repo from payload `cwd` → `git rev-parse
   --show-toplevel`, falling back to `$CLAUDE_PROJECT_DIR`, then `__file__`.
   **Lands before worktrees are adopted** — otherwise hole 3 goes live.
3. `tests/test_review_gate.py`: the regression floor, ported from the eleven
   piped-JSON cases that currently live only in a transcript.
4. `.claude/hooks/branch_lease.py`: registry + tokenising guard + `claim`,
   `release`, `show` subcommands.
5. `tests/test_session_lease.py`, with an **injectable registry root** — the suite
   runs `-n auto --dist loadfile`, so a test touching the real
   `.git/tcgi-sessions/` would fight both the other workers and the live session.
6. `.claude/settings.json`: register the guard plus `SessionStart` (claim) and
   `SessionEnd` (release). Reap a lease whose heartbeat is over **8 hours** old —
   long enough to survive a lunch break, short enough that a crashed session does
   not wedge the repo overnight.
7. `review_gate.py`: record `session`/`branch`/`worktree`; refuse a foreign
   session's verdict.
8. `ticket.py`: `next` unions the local directory with `NNN-` names across
   `origin/*` refs; add `claim` creating the stub with `O_EXCL`.
9. `release.py check`: print the lease table when the tree is dirty.
10. Docs: the `CLAUDE.md` section, `app-map.json:377`, the three SKILL.md files,
    and an ADR in `docs/decisions.md`.

## Verification

- `rules.py check` — every seal still resolves. The nine `CLAUDE.md` invariant
  seals must be **unchanged**, proving the new section landed outside the sealed
  one. **Never reseal to make this pass.**
- `ticket.py check`, `plan.py validate`, `ruff format .`, `ruff check .`, and the
  full suite with `QT_QPA_PLATFORM=offscreen`.
- Negative tests, per `CHECKER-NEEDS-NEGATIVE-TEST`: `git log --oneline` is not
  guarded; `gh pr create --body "...checkout..."` is not guarded; a **same**-session
  branch switch is allowed; `git add <path>` is allowed while a foreign lease is
  live; session A's recorded verdict does **not** open session B's `ticket-close`.
- Observe a real lease file after install to confirm the `PreToolUse` payload
  actually carries `session_id`. The guard degrades to never-block if it does not,
  so this is confirmation rather than a gamble.

## What the panel found

`invariants: pass` (no rule in scope — nothing under `src/`), `test: pass`,
`risk: revise` with three findings, all real and all fixed before merge:

1. **Ambiguous ownership silently reopened hole 2.** `owning_session()` returns
   `None` when *more than one* lease holds a tree; `cmd_record` wrote that as
   `"session": null`; and the gate's `if recorded and caller and …` reads null as
   "nothing to compare". So in exactly the two-session case this ticket exists for,
   either session could use the other's verdict. The artifact now carries
   `holders` as well, and a null session with several holders fails **closed**,
   while a null with none stays admissible as the legacy case.
2. **`cd elsewhere && git commit` was a false positive.** Rivals are detected
   against the tool call's starting directory, but classification is textual over
   the whole chain, so a commit in an unrelated scratch repo was blocked with a
   message that was false for that command. Judging now stops at any directory
   change. The first attempt at this fix tried to resolve the `cd` target and
   failed — posix tokenising eats Windows backslashes, which is precisely why this
   file reads subcommands and flags but never a path *value*.
3. **`git pull` was missing** from the HEAD-moving set, beside `merge` and
   `rebase` which were both guarded. `apply` too. Neither was excluded
   deliberately.

The accepted cost of (2) is that `cd . && git switch -c x` is not judged. That is
a decision, not an oversight, and it has a test saying so: the guard exists to
stop an *unthinking* `git switch`, and everything before the `cd` is still judged.

## Notes

- Every hole here is in the **dispatch and admission machinery**, not in any
  critic — the same pattern ticket 003 recorded. The critics have been the
  reliable part; the plumbing around them has not.
- Worktrees do not make merges free. Two sessions editing `main_window.py` still
  conflict — but visibly, at merge time, instead of silently at commit time.
- Run the venv from the main clone (`.venv/Scripts/python.exe`) with
  `PYTHONPATH=<worktree>/src`, or the editable install resolves to the main clone
  and the wrong tree gets verified. Confirmed working 2026-08-07.
