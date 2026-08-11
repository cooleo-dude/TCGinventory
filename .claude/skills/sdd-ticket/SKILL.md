---
name: sdd-ticket
description: File, sync and close TCGinventory tickets as merge requests. Use when a sweep or a plan produces work that will not be done immediately, when asked to open an issue or ticket, to list what is outstanding, or to close one that has landed. Also use when the user types /sdd-ticket.
argument-hint: <list | check | open <what> | push | close> — defaults to list
allowed-tools: [Read, Grep, Glob, Bash, PowerShell, Edit, Write]
---

# sdd-ticket

Part of the **`/sdd-*`** corpus: **`/sdd-ticket`** (work intake), `/sdd-version`
(the number), `/sdd-release` (tag, build, publish).

Request: **$ARGUMENTS** — if empty, run `list` and report what is outstanding.

```bash
PY=.venv/Scripts/python.exe; T=.claude/skills/sdd-ticket/scripts/ticket.py
$PY $T open my-slug   # branch commit + push + draft MR; its number is X
$PY $T list           # every open ticket, read from GitHub
$PY $T check          # non-zero if the ticket on this branch is malformed
$PY $T push           # rewrite the MR title and body from the file
$PY $T close          # record the outcome, remove the file, then merge
```

## Numbering: one work item, one number

**`X` is the GitHub merge-request number.** `X#` names the specification and `X!`
names the change that satisfies it — `41#` and `41!` are the same work, and you
never need a lookup table to get from one to the other.

They cannot drift, because they are the same object. That is the point of the
scheme and the reason it is shaped this way rather than as a naming convention:

> GitHub allocates issues and pull requests from **one per-repo counter**. Measured
> on this repo, issues took 2/7/10/12/13/14/16/17/22/31/32/36 while PRs took
> 1/3/4/5/6/8/9/11/15/18…, one interleaved sequence with no overlap. A work item
> that files both an issue *and* a PR can therefore never have them agree.

So the issue is retired. A ticket is a **branch with a draft MR** carrying the
ticket file, and `deferred` work is simply a draft MR left open — a better backlog
than an issue, because it carries the branch and the plan with it.

Three places hold the number and `check` makes them agree: the filename
(`041-my-slug.md`), the heading sigil (`# 41# — Title`) and the `mr:` field. A
disagreement means `push` would retitle one MR from a file describing another.

**The number arrives after the branch exists**, because GitHub mints it. So the
branch is named for the slug, not the number — you find the branch from the MR.

## The one rule

**The file on the branch is the truth while the MR is open. The MR body is a
mirror.** Edit the file, then `push`.

`.claude/context/tickets/NNN-*.md` is versioned with the code, readable offline,
and actionable by an agent with no network and no auth. The MR body exists so the
work is visible from a phone. When they disagree, the file wins.

Then `close` inverts it exactly once: it rewrites the body from the file and
removes the file, so the **MR body is the durable record** from that moment on.
Never hand-edit an MR body before then — `push` overwrites it.

## Where tickets come from

Four skills feed this. Each ends by filing what qualifies; none of them invent
their own format.

| source | what becomes a ticket |
|---|---|
| `/optimization-sweep` grounded pass | findings it may not auto-apply: money path, migrations, stored precision, retention |
| `/optimization-sweep` research pass | `MECHANISM` proposals clearing all five bars |
| `/dead-code-sweep` | *Flagged for review* items — a symbol whose only reference could be dynamic, a handler whose failure mode was not pinned down |
| `/task-planning` | **always** — every approved brief, before any code is written |

Read `.claude/context/tickets/README.md` for the bar each must clear. The short
version: a found improvement needs a measurement, no regression, no accuracy
cost, no product decision, and a clean constraint checklist. Planned work from
`/task-planning` files unconditionally — the ticket is what the branch, the MR
and the commit point back at.

**Anything that does not qualify goes to the user in the calling skill's report,
not into this directory.** Minimal-impact wins, alternatives worth weighing and
`none` results are for judgement. If the user declines one, it belongs in
`.claude/skills/optimization-sweep/references/declined.md`.

## Filing one

1. **Take a worktree first** (`EnterWorktree`). `open` commits and pushes, so it
   needs a branch that is yours.
2. `ticket.py open <kebab-title>`. It writes `draft-<slug>.md`, commits it, pushes
   the branch, opens the draft MR, then renames the file to the number GitHub
   handed back and retitles the MR `X! Title`.
3. Fill in `.claude/context/tickets/NNN-<slug>.md` — the front block (`status kind
   source mr axis found saving risk`), then the finding and its evidence, an **area
   of impact** table of real `file:line`, the chosen approach **and the rejected
   alternatives with reasons**, a numbered plan, and how to verify.
4. `ticket.py check` must pass.
5. `ticket.py push` mirrors it to the MR body.

`open` needs network and refuses on `main`, on a non-kebab slug, and on a branch
that already has a ticket — one ticket per MR, or `push` and `close` cannot tell
which one you mean.

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

`ticket.py close` then sets `status: done`, rewrites the MR body with that final
text, and **removes the ticket file** in a commit on the branch. Then you merge.

**The order is the whole design, and `close` runs *before* the merge.** Rewriting
the body first means the MR carries the finished write-up rather than the brief;
removing the file in the same branch means the merge leaves `main` with no ticket
files to sweep up afterwards. Run it after the merge and both guarantees are gone —
the file is already on `main` and deleting it needs its own PR, which is the
overhead this replaced. `close` refuses in that state and says so.

Nothing is lost by the delete: the merged MR carries the full final body, and the
file is in `git log`. The number is never reused, because GitHub never reissues
one.

For a ticket that is abandoned rather than finished, set `status: dropped`, say
why, mirror it into `declined.md` so no future sweep re-derives it, then `close` —
it keeps the `dropped` wording — and close the MR without merging.

Never delete a ticket file by hand: that strands an open MR describing finished
work, and `check` cannot flag what is no longer there.

## Notes that will bite you

- `gh` lives in the **machine** PATH. A shell opened before it was installed will
  not see it; the script falls back to the absolute path, but your own `gh`
  commands may need a fresh terminal.
- `gh auth login` is interactive and this harness redirects stdin — it cannot be
  run from a tool call. The user runs it in a real terminal.
- The repo is **private**, so a token needs the `repo` scope for MRs.
- **`open`, `list`, `push` and `close` need network.** Only `check` is offline.
  That is the trade for a number GitHub owns; the old local counter needed
  `O_EXCL` marker files to stop two sessions minting `007` twice, and none of that
  exists any more.
- **`main` carries no ticket files**, so `list` reads open MRs from GitHub. The
  local directory would only ever show the branch you are standing on, which is
  not a queue.
