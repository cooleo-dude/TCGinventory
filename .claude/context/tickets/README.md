# Tickets

Work that has been **found and specified but not done**, filed so it survives
the conversation that produced it. One file per ticket, on the branch that will
land it, mirrored into its merge request by `/sdd-ticket`.

**`X` is the merge-request number**: `X#` is the specification, `X!` the change
that satisfies it. One work item, one number — see `/sdd-ticket` § Numbering for
why it has to be the MR's and not a counter of our own.

A ticket exists so the work can be picked up **without the conversation that
produced it**. It carries the evidence, the blast radius, the approach that was
chosen and the ones that were not, and a plan. If a ticket cannot be executed
from a cold start, it is not finished.

## Where tickets come from

Four skills feed this directory. Each has a closing step that files what
qualifies; none of them invent their own format.

| source | what becomes a ticket |
|---|---|
| `/optimization-sweep` — grounded pass | findings it is forbidden to auto-apply: the money path, migrations, stored precision, retention policy |
| `/optimization-sweep` — research pass | `MECHANISM` proposals clearing all five bars below |
| `/dead-code-sweep` | *Flagged for review* items — a symbol whose only reference could be dynamic, a handler whose failure mode was not pinned down |
| `/task-planning` | **every approved brief, always** — the plan, the blast-radius table and the open questions |

## What earns a ticket

For a **found improvement** (either sweep), all five:

1. **Measured better** on at least one axis, against real data, before and after.
2. **No measured regression** elsewhere.
3. **No correctness or accuracy cost** — or one measured to be zero. "Probably
   fine" is not a measurement.
4. **Not a product decision.** A tradeoff the user must weigh is a question for
   them, not a ticket.
5. **Passes the constraint checklist** — packaging, offline, layer, platform,
   maintenance.

For **planned work** (`/task-planning`) there is no bar: an approved brief is
always filed, before any code is written, whether or not it is being worked on
now. The ticket is what the branch, the merge request and the commit point back
at.

Everything else — minimal impact, an alternative worth weighing, a `none` result
— goes to the user in the skill's report and no further.

**Declined is not the same as worthless.** A finding the user has said "not now"
to, but which is real and measured, is filed as `deferred` so it stays visible
and re-openable; `declined.md` keeps the reasoning and the reopening condition.
A finding that measurement *killed* is not a ticket at all — it is a permanent
result and lives only in `declined.md`.

## Format

Every ticket opens with this block:

```
status:   open | in-progress | deferred | done | dropped
kind:     optimization | dead-code | feature | fix
source:   /optimization-sweep research | /dead-code-sweep | /task-planning | ...
mr:       N         (the merge request that lands it — the same N as the filename)
axis:     latency | memory | storage | correctness | maintainability
found:    YYYY-MM-DD
saving:   the measured number, or "-" for non-optimization work
risk:     low | medium | high, and one clause saying why
```

The heading carries the sigil — `# 41# — Title` — and the filename, the heading and
`mr:` must all agree. `ticket.py check` fails when they do not: a disagreement means
`push` would retitle one MR from a file describing another.

Then: the finding and its evidence, an **area of impact** table of `file:line`,
the chosen approach **and the rejected alternatives with reasons**, a numbered
plan, and how to verify.

## Status

| status | meaning |
|---|---|
| `open` | ready to implement |
| `deferred` | real and measured, but the user has said not now — a draft MR left open, carrying the condition that reopens it |
| `in-progress` | someone is on it — say who and when |
| `done` | landed; record the commit, the MR and the measured after |
| `dropped` | abandoned; say why, and mirror it into `declined.md` |

`done` and `dropped` are terminal, and **a ticket file only ever exists on the
branch that is landing it** — never on `main`. So they are written into the file,
and then the file goes, in the commit before the merge.

## Resolving one

Edit the ticket first — the commit, the MR, the **measured after**, or for a
`dropped` one, why and what would reopen it. *Then* `/sdd-ticket close`, which:

1. rewrites the MR body with that final text, so the MR stops describing the work
   as pending;
2. removes the ticket file and commits the removal to the branch.

Then you merge (or, for a dropped one, close the MR without merging).

The order is the point, and so is the timing: the MR body becomes the durable
record a moment before the file that fed it is removed, and because the removal
rides in the same branch, the merge leaves `main` with nothing to sweep up. Run
`close` *after* the merge and both guarantees are gone — the file is already on
`main`, and deleting it needs its own PR. `close` refuses in that state.

To read a resolved ticket later: the merged MR carries its full final text, and
`git log --diff-filter=D --name-only -- .claude/context/tickets/` names every file
that has been removed. The **number is never reused**, because GitHub never
reissues one.

Never delete a ticket by hand. That strands an open MR describing work that is
finished; `ticket.py check` will not catch it, because there is nothing left to
check.

## The queue

There is no index here to maintain. `main` carries no ticket files, so the
outstanding queue is the set of open merge requests:

```bash
.venv/Scripts/python.exe .claude/skills/sdd-ticket/scripts/ticket.py list
```

Draft MRs are filed-but-not-started work (including everything `deferred`); ready
ones are waiting to land.
