---
name: sdd-ticket
description: File, sync and close TCGinventory tickets and their GitHub issues. Use when a sweep or a plan produces work that will not be done immediately, when asked to open an issue or ticket, to list what is outstanding, or to close one that has landed. Also use when the user types /sdd-ticket.
argument-hint: <list | check | file <what> | push [N] | close N> — defaults to list
allowed-tools: [Read, Grep, Glob, Bash, PowerShell, Edit, Write]
---

# sdd-ticket

Part of the **`/sdd-*`** corpus: **`/sdd-ticket`** (work intake), `/sdd-version`
(the number), `/sdd-release` (tag, build, publish).

Request: **$ARGUMENTS** — if empty, run `list` and report what is outstanding.

```bash
PY=.venv/Scripts/python.exe; T=.claude/skills/sdd-ticket/scripts/ticket.py
$PY $T list           # every ticket, its status, its issue
$PY $T next           # the number a new ticket takes
$PY $T claim my-slug  # reserve that number by creating the file — use this
$PY $T check          # non-zero if malformed, unindexed or out of sync
$PY $T push 1         # create or update the GitHub issue
$PY $T close 1        # mark done and close the issue
```

**Use `claim`, not `next`.** `next` is a read, and several sessions work this repo
at once: two that read `007` a second apart both write `007-*.md` and both file an
issue. `claim` reserves the number atomically and hands back a skeleton to fill in.
It also counts numbers used on *every* ref, so a ticket filed on another session's
branch — invisible to this checkout — is not handed out twice.

## The one rule

**The local file is the truth. The GitHub issue is a mirror.**

`.claude/context/tickets/NNN-*.md` is versioned with the code, readable offline,
and actionable by an agent with no network and no auth. The issue exists so the
work is visible from a phone. When they disagree, the file wins — edit the file,
then `push`.

Never hand-edit an issue body on GitHub. `push` overwrites it from the file.

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
`/task-planning` files unconditionally — the ticket is what the branch, the PR
and the commit point back at.

**Anything that does not qualify goes to the user in the calling skill's report,
not into this directory.** Minimal-impact wins, alternatives worth weighing and
`none` results are for judgement. If the user declines one, it belongs in
`.claude/skills/optimization-sweep/references/declined.md`.

## Filing one

1. `ticket.py claim <kebab-title>` — reserves the number *and* writes the skeleton.
2. Fill in `.claude/context/tickets/NNN-<kebab-title>.md`. First line
   `# NNN — Title`, then the front block — `status kind source issue axis found
   saving risk` — then the finding and its evidence, an **area of impact** table
   of real `file:line`, the chosen approach **and the rejected alternatives with
   reasons**, a numbered plan, and how to verify.
3. `ticket.py check` must pass.
4. `ticket.py push N` files the issue and writes `issue: #N` back into the file.
   It also regenerates the README index, so do not maintain that by hand.

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
docs- or tooling-only PR has no lane to judge, and three agents discovering that
is ~80k tokens for an answer a script already gave you.

Then record with `--paths`, so lanes the registry ruled out are logged `n/a`
rather than left missing:

```bash
python .claude/hooks/review_gate.py record --scope pr-merge --ref <n>     --paths $(git diff --name-only origin/main...HEAD)     --verdict risk=pass --verdict test=pass
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
python .claude/hooks/review_gate.py record --scope pr-merge --ref <n>     --verdict invariants=pass --verdict risk=pass --verdict test=pass
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
python .claude/hooks/review_gate.py record --scope pr-merge --ref 6     --verdict risk=pass --inherit <sha of the previous panel>
```

`--inherit` copies the lanes you did not re-judge from an earlier verdict on the
**same ref** — never across PRs, and never over a lane you judged this round. The
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

`ticket.py close N` sets `status: done` and closes the mirrored issue — the
gate above must be green first, or the call is blocked. Then edit
the ticket to record the commit, the PR and the **measured after** — a ticket
that says what actually happened is the reason the code looks the way it does.

For a ticket that is abandoned rather than finished, set `status: dropped`, say
why, and mirror it into `declined.md` so no future sweep re-derives it.

## Notes that will bite you

- `gh` lives in the **machine** PATH. A shell opened before it was installed will
  not see it; the script falls back to the absolute path, but your own `gh`
  commands may need a fresh terminal.
- `gh auth login` is interactive and this harness redirects stdin — it cannot be
  run from a tool call. The user runs it in a real terminal.
- The repo is **private**, so a token needs the `repo` scope for issues and PRs.
- Ticket numbers and issue numbers are **not** the same sequence. PR #1 and issue
  #2 already share a counter on GitHub's side; the ticket number is ours.
