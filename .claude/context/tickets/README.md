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
— goes to the user in the skill's report and no further.

**Declined is not the same as worthless.** A finding the user has said "not now"
to, but which is real and measured, is filed as `deferred` so it stays visible
and re-openable; `declined.md` keeps the reasoning and the reopening condition.
A finding that measurement *killed* is not a ticket at all — it is a permanent
result and lives only in `declined.md`.

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
| `deferred` | real and measured, but the user has said not now — carries the condition that reopens it |
| `in-progress` | someone is on it — say who and when |
| `done` | landed; record the commit, the PR and the measured after |
| `dropped` | abandoned; say why, and mirror it into `declined.md` |

`done` and `dropped` are terminal, and **this directory holds live work only** —
a listing is the outstanding queue, not a growing pile of finished ones. So they
are written into the file, and then the file goes.

## Resolving one

Edit the ticket first — the commit, the PR, the **measured after**, or for a
`dropped` one, why and what would reopen it. *Then* `/sdd-ticket close N`, which:

1. pushes that final text to the mirrored issue, so the issue stops describing
   the work as pending;
2. closes the issue (`not planned` for a dropped one);
3. deletes the ticket file and re-indexes this README.

The order is the point: the issue becomes the durable record a moment before the
file that fed it is removed. `close` refuses on a ticket with no mirrored issue —
there would be nowhere for the record to land. `push N` first.

To read a resolved ticket later: the closed GitHub issue carries its full final
text, and `git log --diff-filter=D --name-only -- .claude/context/tickets/` names
every file that has been removed. The **number is never reused** — `ticket.py
next` reads every ref, deleted files included.

Never delete a ticket by hand. That skips the mirror and strands an open issue
describing work that is finished; `ticket.py check` will not catch it, because
there is nothing left to check.

## Index

| # | title | kind | issue | status |
|---|---|---|---|---|
| [004](004-recognition-prewarm-costs-112mb-at-startup.md) | Recognition prewarm holds 112 MB at startup for everyone | optimization | #12 | `deferred` |
| [005](005-card-image-cache-is-2.2gb-of-png.md) | The card image cache is 2.2 GB of PNG | optimization | #13 | `deferred` |
| [006](006-sub-threshold-optimizations.md) | Two measured optimizations that are real and too small to ship | optimization | #14 | `deferred` |
| [007](007-worker-threads-are-not-parked-at-shutdown.md) | Worker threads are parked when replaced, but not when the app shuts down | fix | #16 | `open` |
