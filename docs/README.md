# TCGinventory — Docs

Personal operating records for running the TCGinventory platform. Unlike
[`/specs`](../specs) (which defines *what we're building*), `/docs` is the
**owner's manual and logbook**: how to use the app, how the money math works in
practice, and notes about your own collection and workflow.

## Contents

| Document | Purpose |
|----------|---------|
| [decisions.md](decisions.md) | Log of key project/product decisions (ADR-style) |
| [glossary.md](glossary.md) | Terms: item, lot, cost basis, boot, FIFO, grading |
| [cost-basis-examples.md](cost-basis-examples.md) | Worked examples of purchases, sales, trades & P/L |
| [user-guide.md](user-guide.md) | How to use the app (fills in as features land) |
| [collection-notes.md](collection-notes.md) | Personal notes: sources, locations, conventions |

## Conventions

- Money is tracked internally in **integer cents**; docs show dollars.
- Dates are **ISO 8601** (`YYYY-MM-DD`).
- Default cost-basis method is **FIFO** (see the examples doc).
