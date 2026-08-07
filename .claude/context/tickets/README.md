# Tickets

Work that has been **found and specified but not done**, filed so it survives
the conversation that produced it. One file per ticket, one number sequence
across every kind. Mirrored to GitHub issues by `/sdd-ticket`.

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
now. The ticket is what the branch, the pull request and the commit point back
at.

Everything else — minimal impact, an alternative worth weighing, a `none` result
— goes to the user in the skill's report and no further. If the user declines it,
it belongs in
`../../skills/optimization-sweep/references/declined.md`, not here.

## Format

Every ticket opens with this block:

```
status:   open | in-progress | done | dropped
kind:     optimization | dead-code | feature | fix
source:   /optimization-sweep research | /dead-code-sweep | /task-planning | ...
issue:    #N        (the GitHub mirror; "none" until /sdd-ticket files it)
axis:     latency | memory | storage | correctness | maintainability
found:    YYYY-MM-DD
saving:   the measured number, or "-" for non-optimization work
risk:     low | medium | high, and one clause saying why
```

Then: the finding and its evidence, an **area of impact** table of `file:line`,
the chosen approach **and the rejected alternatives with reasons**, a numbered
plan, and how to verify.

## Status

| status | meaning |
|---|---|
| `open` | ready to implement |
| `in-progress` | someone is on it — say who and when |
| `done` | landed; record the commit, the PR and the measured after |
| `dropped` | abandoned; say why, and mirror it into `declined.md` |

Close a ticket by editing it, never by deleting it. A finished ticket is the
record of why the code looks the way it does.

## Index

| # | title | kind | issue | status |
|---|---|---|---|---|
| [001](001-card-embedding-float16.md) | Store card embeddings as float16 | optimization | #2 | `open` |
| [002](002-enumerate-the-rule-registry.md) | Enumerate the rule registry to full coverage | feature | #7 | `open` |
