---
name: sdd-ticket
description: File, sync and close TCGinventory tickets, which live on GitHub as issues that become pull requests. Use when a sweep or a plan produces work that will not be done immediately, when asked to open an issue or ticket, to list what is outstanding, or to close one that has landed. Also use when the user types /sdd-ticket.
argument-hint: <list | open <what> | show N | push N <file> | start N | check N | close N> — defaults to list
allowed-tools: [Read, Grep, Glob, Bash, PowerShell, Edit, Write]
---

# sdd-ticket

Part of the **`/sdd-*`** corpus: **`/sdd-ticket`** (work intake), `/sdd-version`
(the number), `/sdd-release` (tag, build, publish).

Request: **$ARGUMENTS** — if empty, run `list` and report what is outstanding.

```bash
PY=.venv/Scripts/python.exe; T=.claude/skills/sdd-ticket/scripts/ticket.py
$PY $T open my-slug     # file the specification as an issue; its number is X
$PY $T list             # the queue: X# not started, X! in flight
$PY $T show 46          # print the title and body
$PY $T push 46 t.md     # replace the body from a file you wrote
$PY $T start 46         # convert issue 46 into pull request 46
$PY $T check 46         # non-zero if it is malformed
$PY $T close 46         # record the outcome, then merge
```

## Numbering: one work item, one number, two phases

**`X` is a GitHub number, and the sigil says which phase the work is in.** `X#` is
the specification; `X!` is the change that satisfies it. `46#` and `46!` are not two
things kept in agreement — they are **the same object**, and `start` moves it from
one phase to the other without the number changing.

That is possible because of how GitHub is actually built:

> **A pull request is an issue** with a head and a base attached. That is why issues
> and pull requests share one per-repo counter — measured here, issues took
> 2/7/10/12/13/14/16/17/22/31/32/36 while PRs took 1/3/4/5/6/8/9/11/15/18…, one
> interleaved sequence. `POST /pulls` accepts an `issue` parameter in place of
> `title`/`body` and **converts that issue into a pull request, keeping its number**.
> Undocumented in the current REST reference, but live: verified when `46#` became
> `46!`.

So numbering needs no discipline at all. There is one number because there is one
object, and one copy of the text because it is that object's body.

## The one rule

**The body on GitHub is the ticket. There is no local copy.**

`show` prints it, `push` replaces it from a file you wrote — keep that file in the
session scratchpad, never in the repo. `close` rewrites it one last time so it
records what happened rather than what was planned, and after the merge it is the
permanent record.

This replaced a `.claude/context/tickets/` directory, and the reason is worth
keeping: a file meant the number lived in three places (filename, heading sigil,
`mr:` field) with a checker to keep them agreeing. **One copy cannot disagree with
itself.** The cost is that nothing here works offline — accepted deliberately.

## Where tickets come from

Four skills feed this. Each ends by filing what qualifies; none of them invent
their own format.

| source | what becomes a ticket |
|---|---|
| `/optimization-sweep` grounded pass | findings it may not auto-apply: money path, migrations, stored precision, retention |
| `/optimization-sweep` research pass | `MECHANISM` proposals clearing all five bars |
| `/dead-code-sweep` | *Flagged for review* items — a symbol whose only reference could be dynamic, a handler whose failure mode was not pinned down |
| `/task-planning` | **always** — every approved brief, before any code is written |

### What earns a ticket

For a **found improvement** (either sweep), all five:

1. **Measured better** on at least one axis, against real data, before and after.
2. **No measured regression** elsewhere.
3. **No correctness or accuracy cost** — or one measured to be zero. "Probably
   fine" is not a measurement.
4. **Not a product decision.** A tradeoff the user must weigh is a question for
   them, not a ticket.
5. **Passes the constraint checklist** — packaging, offline, layer, platform,
   maintenance.

For **planned work** (`/task-planning`) there is no bar: an approved brief is always
filed, before any code is written, whether or not it is being worked on now. The
ticket is what the branch, the MR and the commit point back at.

**Anything that does not qualify goes to the user in the calling skill's report, and
no further.** Minimal-impact wins, alternatives worth weighing and `none` results
are for judgement. If the user declines one, it belongs in
`.claude/skills/optimization-sweep/references/declined.md`.

**Declined is not the same as worthless.** A finding the user has said "not now" to,
but which is real and measured, stays filed as `deferred` so it remains visible and
re-openable — an unstarted `X#` in the queue. A finding that measurement *killed* is
not a ticket at all: it is a permanent result and lives only in `declined.md`.

## Filing one

**No branch, no commit, no worktree.** Filing is `open`, and it happens before any
code exists — that is the whole reason the specification is an issue.

1. `ticket.py open <kebab-title>` → files issue `X#` with the skeleton body.
2. `ticket.py show X > <scratchpad>/t.md`, then write it: the front block (`status
   kind source axis found saving risk`), the finding and its evidence, an **area of
   impact** table of real `file:line`, the chosen approach **and the rejected
   alternatives with reasons**, a numbered plan, and how to verify.
3. `ticket.py push X <scratchpad>/t.md`.
4. `ticket.py check X` must pass.

Then, when the work actually starts: take a worktree, commit, push, and
`ticket.py start X` — the issue becomes pull request `X`, same number, and the title
picks up the `!`.

**Write for someone who was not in the conversation.** The measurement you did
not record is a measurement the next agent has to redo. The alternative you
rejected in your head will be re-proposed by someone else in a fortnight — that
is what the *rejected alternatives* section is for, and it is not optional.

## The review gate

**The critic panel is opt-in. Do not dispatch it unless the user asks.**

A spawn costs ~26k tokens before it judges anything, so critics run when someone
says *"run the critics"* (or names one) — not on every merge, and not because a
diff looks important. Merging and closing tickets proceed normally without them.

What `.claude/hooks/review_gate.py` still enforces, as a `PreToolUse` hook, is the
other half: **a panel that was run cannot be quietly ignored.** No verdict on
record → the merge proceeds. A recorded `block` → the merge stops until the
finding is fixed and committed, or the objection is cleared deliberately
(`review_gate.py clear`). Opting in to run the critics is not opting in to obey
them selectively.

If you think a change warrants review, *say so and let the user decide* — a
sentence recommending it costs nothing; four agents cost ~100k.

### When you are asked to run it: once, on a finished branch

**The panel is the most expensive thing in this repo. Run it last.** A verdict is
bound to a sha, so every push throws away a completed panel: on 2026-08-07 a PR
was reviewed three times — twelve agents, ~560k tokens — because the author kept
pushing between rounds. The code under review never changed after the first.

So before you dispatch anything: land every fix, push, and confirm the branch is
one you intend not to touch again. `git status` clean, branch pushed, suite green.
A panel dispatched on a branch you are still editing is a panel you will pay for
twice.

**Push before you `record`.** `record` resolves the MR's head commit from GitHub
and binds the verdict to *that* — not to your local `HEAD` (ticket 003; it used to
bind to the checkout, which meant a `block` recorded anywhere but the MR's branch
was invisible to the merge it was about). An unpushed commit is therefore not part
of what the verdict covers. `record` prints what it bound to, and `show` repeats
it: `pr-head` is the real guarantee, `local-head` the weaker fallback when `gh`
cannot be reached.

**`--ref` is the MR number, and one verdict covers landing it.** There used to be
a `--scope`, because `--ref` meant a PR number for `pr-merge` and our own ticket
number for `ticket-close` — two sequences behind one flag, so landing one change
needed two panels recorded. Now `gh pr merge 41` and `ticket.py close` are two
steps of landing `41!` and share a verdict.

### Ask the registry who to dispatch — never guess

**A critic spawn costs ~26k tokens before it judges anything**, so the question
is not "which critics by default" but "which lanes can this diff even violate".
The registry answers that for free:

```bash
git diff --name-only origin/main...HEAD          # the changed files
python .claude/hooks/rules.py scope <those files>
```

It prints the rules in play per lane and a `dispatch:` line. **Spawn exactly what
that line names — no more.** If it says *no rules in scope*, spawn **nothing**: a
docs- or tooling-only MR has no lane to judge, and three agents discovering that
is ~80k tokens for an answer a script already gave you.

Then record with `--paths`, so lanes the registry ruled out are logged `n/a`
rather than left missing:

```bash
python .claude/hooks/review_gate.py record --ref 41     --paths $(git diff --name-only origin/main...HEAD)     --verdict risk=pass --verdict test=pass
```

Two refinements once you know who to spawn:

- **Small diffs get one agent, not three.** At **≤150 changed lines** with no
  migration and no money path, `tcgi-mini-critic` covers all four lenses in one
  spawn. The tier is sized by **lines, not directories** — this is a desktop app,
  so almost everything touches `ui/`, and a tier that excluded it never fired.
- **Hand each critic the rule ids `scope` printed for its lane.** It saves them
  re-deriving what you already computed, which is a real slice of that 26k floor.

Hand them the diff and nothing else — they judge the change, never your account
of it. Each ends in a verdict line. Record them:

```bash
python .claude/hooks/review_gate.py record --ref 41     --verdict invariants=pass --verdict risk=pass --verdict test=pass
```

### Re-running: only the lanes that got dirty

`revise` is a note to answer, not a failure. **`block` stops the gate** — fix the
findings, commit, and re-run. Verdicts are bound to a sha, so a new commit
reopens the gate deliberately: the panel judged something that is no longer what
would merge.

When you do have to push again, **batch every outstanding fix into one commit**,
then re-dispatch **only the lanes whose files changed** and carry the rest
forward:

```bash
# risk asked for a change in ui/views/inventory.py; nothing else moved
python .claude/hooks/review_gate.py record --ref 41     --verdict risk=pass --inherit <sha of the previous panel>
```

`--inherit` copies the lanes you did not re-judge from an earlier verdict on the
**same MR** — never across MRs, and never over a lane you judged this round. The
artifact records which lanes were carried and from where, so the inheritance is
auditable rather than invisible. Re-judging a lane whose files did not change is
pure cost: a documentation-only follow-up commit does not need four agents to
re-read the same `src/` diff.

They are bound to the **session** that recorded them too. Two sessions sitting on
the same commit — both just branched from `main`, neither has committed yet —
would otherwise trade verdicts, one recording green for its own diff and opening
the gate for the other's unreviewed work. Run your own panel; you cannot inherit
someone else's.

Relay every finding to the user. A panel whose findings you summarise away is a
panel that did nothing.

## Closing one

**Edit the ticket first, then close it.** Record the commit, the MR and the
**measured after** — a ticket that says what actually happened is the reason the
code looks the way it does.

`ticket.py close X` sets `status: done` and rewrites the body, so it records the
outcome instead of the plan. Then you merge.

**`close` runs *before* the merge**, and refuses afterwards. Once merged, the body is
a plan nobody goes back to correct and there is no local copy to rewrite it from — so
the last moment it can be made true is the moment before it lands.

The number is never reused, because GitHub never reissues one.

For a ticket that is abandoned rather than finished, set `status: dropped`, say why,
mirror it into `declined.md` so no future sweep re-derives it, then `close X` — it
keeps the `dropped` wording — and close the MR without merging. A specification that
was never started needs no `close` at all: it refuses, and says to drop the issue
instead, because nothing landed.

## Notes that will bite you

- `gh` lives in the **machine** PATH. A shell opened before it was installed will
  not see it; the script falls back to the absolute path, but your own `gh`
  commands may need a fresh terminal.
- `gh auth login` is interactive and this harness redirects stdin — it cannot be
  run from a tool call. The user runs it in a real terminal.
- The repo is **private**, so a token needs the `repo` scope for MRs.
- **Everything needs network**, `check` included. There is no offline mode and no
  local mirror to fall back on — that is the trade for one copy of the text. The old
  local counter also needed `O_EXCL` marker files to stop two sessions minting `007`
  twice; GitHub cannot issue a number twice, so none of that exists any more.
- **`start` needs a pushed branch with real commits.** GitHub cannot open a pull
  request against a branch identical to its base. An earlier design worked around
  that with an empty commit; filing as an issue means the number already exists, so
  `start` can simply refuse until there is something to review.
- **`list` shows both phases.** `X#` rows are specified-but-unstarted (including
  everything `deferred`); `X!` rows are in flight, draft or ready. A row marked
  `(no sigil)` predates the convention — it is still real work.
