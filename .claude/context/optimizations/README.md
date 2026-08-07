# Optimization tickets

Findings from `/optimization-sweep`'s research pass (Step 7) that are
**irrefutably positive** and ready for someone else to implement. One file per
finding, newest number highest.

A ticket exists so the work can be picked up **without the conversation that
produced it**. It carries the measurement, the blast radius, the approach that
was chosen and the ones that were not, and a step-by-step plan. If a ticket
cannot be executed from a cold start, it is not finished.

## What earns a ticket

All five, or it does not get filed:

1. **Measured better** on at least one axis (latency, memory, storage), against
   real staged data, with a before and an after.
2. **No measured regression** on the other two.
3. **No correctness or accuracy cost** — or a cost measured to be zero. "Probably
   fine" is not a measurement.
4. **Not a product decision.** A tradeoff the user has to weigh is a question for
   them, not a ticket.
5. **Passes the constraint checklist** in SKILL.md 7f — packaging, offline,
   layer, platform, maintenance.

Everything else — minimal impact, an alternative worth considering, a `none`
result — goes to the user in the sweep's report for judgement. It does not get
filed here, and if the user declines it, it goes to
`../../skills/optimization-sweep/references/declined.md` instead.

## Status

Set `status:` in the front block of each ticket.

| status | meaning |
|---|---|
| `open` | ready to implement |
| `in-progress` | someone is on it — say who/when |
| `done` | landed; record the commit and the measured after |
| `dropped` | abandoned; say why, and mirror it into `declined.md` |

Close a ticket by editing it, not deleting it. A finished ticket is the record
of why the code looks the way it does.

## Index

| # | title | axis | status |
|---|---|---|---|
| [001](001-card-embedding-float16.md) | Store card embeddings as float16 | storage | `open` |
