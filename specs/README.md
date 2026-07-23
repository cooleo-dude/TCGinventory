# TCGinventory — Specifications

This directory holds the **planning baseline** for TCGinventory: a Python desktop
application for tracking a personal inventory of trading card collectibles
(Pokémon-focused), including purchases, sales, and trades on a single-card or
lot basis.

These specs are the source of truth for *what* we are building and *why*. They
should be updated as decisions change. Implementation lives outside this folder;
personal operating records live in [`/docs`](../docs).

## Reading order

| # | Document | Purpose |
|---|----------|---------|
| 00 | [Overview & Goals](00-overview.md) | Vision, users, scope, non-goals |
| 01 | [Architecture](01-architecture.md) | Tech stack, layering, project layout |
| 02 | [Data Model](02-data-model.md) | Entities, schema, cost-basis rules |
| 03 | [Features & Requirements](03-features.md) | Functional requirements for V1 |
| 04 | [UI / UX](04-ui-ux.md) | Screens, navigation, interactions |
| 05 | [Roadmap](05-roadmap.md) | Milestones, phasing, future work |
| 06 | [Recognition](06-recognition.md) | Card image-recognition & learning engine (post-V1, drafting) |

## Review sign-off

Tracks agreement as we step through each section. "Ratified" = owner has
confirmed; pending sections may still change.

| Section | Status | Notes |
|---------|--------|-------|
| 00 Overview & Goals | ✅ Ratified (2026-07-10) | Added dual-purpose: serves personal **and** business; every card accounted for regardless of resale intent. |
| 01 Architecture | ✅ Ratified (2026-07-10) | Python **3.10+** floor (tested through 3.12). **Desktop-only** for now. Qt-free service layer, `src/tcginventory` layout, PyInstaller packaging all confirmed. |
| 02 Data Model | ✅ Ratified (2026-07-10) | Per-card/per-lot intent **Personal/Resale/Ripping** (editable, no history); ripping lots valued at $0 → shown as a loss = cost basis, never sold. Market-value-weighted cost allocation; market value **required** per row; rounding residual → largest line. Items **auto match-or-create** by identity key. Condition greys out when graded. Item gains TCGplayer/eBay links; lot gains `intent` + `acquired_market_value_cents`. |
| 03 Features | ✅ Ratified (2026-07-10) | Items auto match-or-create (F-IT reframed, links added). Unified-transaction reqs (F-TX-*). Market-value cost model; net-consideration wording (F-TR-3). Dashboard charts (F-VW-6/7/8); universal filter/sort (F-VW-9). **Transaction log = full V1 (MUST)**. CSV import + export included. Soft-void + audit (F-DA-*). Acceptance scenarios rewritten. |
| 04 UI/UX | ✅ Ratified (2026-07-10) | **Three windows** (Dashboard · unified Transaction · Inventory) + full **History** view; Item detail as drill-down. Interaction layer finalized: live type-inference badge, live totals/P-L preview, market-value-required + oversell-blocked validation, success screen, void/edit confirmations, keyboard-first entry, accessibility (color never the only cue). Calendar & image/scan deferred. |
| 05 Roadmap | ✅ Ratified (2026-07-10) | Reworked to ratified model: M2 = unified posting algo + intents (incl. ripping $0) + void/edit/audit + 3 dashboard series; M3 read UI (QtCharts graphs, universal filter/sort, full History); M4 = single unified Transaction window + edit-only item editor; M5 CSV import+export+backup; M6 packaging. Post-V1 adds explicit Rip/Open event, calendar, intent-change history. |

## Baseline decisions (v0)

- **GUI framework:** PySide6 (Qt 6)
- **Persistence:** SQLite (local, single-file DB)
- **V1 scope:** Core inventory + transactions (purchases, sales, trades),
  cost basis & profit/loss, search/filter, CSV import/export. No external
  pricing APIs in V1.
- **Domain focus:** Pokémon TCG (sets/series, graded slabs, sealed product),
  with a schema flexible enough for other games later.
- **Status:** All six sections ratified (2026-07-10). Baseline complete —
  ready to begin implementation at **M0** (see [roadmap](05-roadmap.md)).

_Last updated: 2026-07-10_
