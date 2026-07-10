# TCGinventory

A local Python desktop app for tracking a personal inventory of trading card
collectibles (Pokémon-focused) — purchases, sales, and trades, on a single-card
or lot basis, with cost basis and profit/loss.

> **Status:** Planning. The implementation hasn't started yet — the baseline
> plan is being established first.

## Baseline at a glance

- **GUI:** PySide6 (Qt 6) · **Storage:** local SQLite · **Money:** integer cents
- **V1:** items, purchases/sales/trades, FIFO cost basis & P/L, search/filter,
  CSV import/export. No external price feeds in V1.

## Repository layout

- [`/specs`](specs/) — the planning baseline: what we're building and why. Start
  at [`specs/README.md`](specs/README.md).
- [`/docs`](docs/) — personal operating records: owner's manual, decision log,
  glossary, worked cost-basis examples, collection notes.

## Next step

Review the specs (especially the [data model](specs/02-data-model.md) and
[roadmap](specs/05-roadmap.md)), then begin **M0 — project scaffolding**.
