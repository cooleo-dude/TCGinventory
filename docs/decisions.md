# Decision Log

Lightweight ADR-style record of significant decisions. Newest first.

---

### 2026-07-10 — M5 implementation: import/export & backup

- **Two export families.** Human-readable flat CSVs (items/inventory/
  transactions, money in dollars) for spreadsheets, and a **full dump/restore**
  (`export_all`/`import_all`) that writes every table verbatim so the dataset
  round-trips exactly (the exit criterion). Empty CSV cells load as SQL NULL
  (repos never store empty strings, so this is faithful); integer columns rely on
  SQLite affinity to reconvert.
- **Portable backup** = the full CSV dump **and** a one-file DB copy via SQLite's
  online-backup API (`backup_database`). We still never hard-delete, so backup is
  copy-only.
- **Import column mapping is header-based** (case-insensitive aliases) with a
  validation preview, rather than an interactive column-mapper UI — pragmatic for
  V1; the preview surfaces per-row errors before any write.
- **Startup auto-backup** (F-DA-6) writes a timestamped copy to
  `<data_dir>/backups/`, keeps the newest 10, and never blocks launch on failure.

---

### 2026-07-10 — M4 implementation: dialog-based card entry

- The unified Transaction window adds cards via **modal add-card dialogs**
  (give / receive) that append to summary tables, rather than fully inline
  editable table rows. Same ratified behavior and validation (market value
  required, condition greys out when graded, oversell blocks submit, ripping
  excluded from the give picker) — the dialog form just handles the conditional
  grade fields far more robustly than in-cell editors. Inline row editing can be
  a later refinement.
- **Edit** pre-loads the transaction and reconstructs the cash split single-
  sided from the stored net `cash_delta` (only the net is stored); this is exact
  because allocation and cash both depend only on cards-out and net cash.
- Live type-inference badge and the summary/margin footer are driven by
  `TransactionService.preview()` (headless, tested).

---

### 2026-07-10 — M2 implementation decisions (services & money math)

Three judgment calls made while building the service layer (all in code + tests):

- **Indivisible qty>1 allocation splits a line into two lots.** When a line's
  allocated total doesn't divide evenly by its quantity, it yields two lots —
  `qty − r` units at the base unit cost and `r` at base+1 — so
  `Σ(unit_cost × qty)` still equals the total exactly (invariant 6). Quantity-1
  lines (the common case) always yield a single lot. Keeps unit costs integer
  without rounding drift.
- **Negative `net_consideration_in` clamps to $0.** If you're paid more cash than
  the value you gave up (rare), incoming cards get a $0 basis rather than a
  negative one. The cash is still captured in `cash_delta`. Revisit if real usage
  wants negative basis.
- **Historical collection-value series deferred to M3.** The two exact,
  event-based dashboard series (money-movement, realized-profit) are implemented
  now; a true market-value-of-holdings-over-time series needs a market-value
  history the schema doesn't record. `collection_value_cents` gives the current
  point-in-time figure. To be built with the chart in M3.

---

### 2026-07-10 — Margin surfacing + trade summary; two market values clarified

- **Clarified** that the schema holds **two** market values doing different jobs:
  `lot.acquired_market_value_cents` (frozen market-at-purchase, used only to set
  cost basis; preserves e.g. $10 vs $15 per lot) and `item.market_value_cents` /
  `market_value_asof` (current estimate, used only for unrealized valuation on
  cards still held). Neither is used for margin.
- **Margin** = realized P/L on cards given up (`proceeds − cost basis`), already
  stored per `out` line in `realized_pl_cents` — **no new column**. It is
  measured against cost basis (what you paid), not market value. Displayed as
  **profit $ + gross-margin %** (`profit ÷ proceeds`); the % is derived, not
  stored.
- **Trade summary overview** added to the Transaction window (Spec 04, F-TX-5): a
  live panel showing value given/received, net cash, cost basis drawn (FIFO), and
  margin, repeated on the success screen.
- Spec/docs only (surfacing, not schema). Propagated to specs 02/03/04 and the
  glossary.

---

### 2026-07-10 — Add Card # and Language to item identity

- **Language** is a new per-item field (enum: EN | JP | CN | KO | FR | DE | IT |
  ES | PT | TH | ID | RU; default EN) and joins the **item identity key** — the
  same card in a different language is a distinct item. Added to the DB via
  migration `0002_add_language.sql` (additive `ALTER TABLE`, forward-only).
- **Card #** already existed on `item` and in the identity key; this change
  surfaces it (and Language) as explicit **Transaction-window row fields** and an
  Inventory column. Both are filterable/sortable under F-VW-9.
- Done at M1 (data layer). Propagated to specs 02/03/04, the enums/models/repos,
  the glossary, and tests.

---

### 2026-07-10 — Spec 05 ratified; baseline complete

- Roadmap reworked to the ratified model (unified posting algorithm, intents incl.
  ripping, void/edit/audit, dashboard charts via QtCharts, full History, CSV
  import+export). Post-V1 backlog now includes an explicit Rip/Open event, the
  shows/auctions calendar, and intent-change history.
- **All six spec sections (00–05) are ratified.** Baseline is complete; next step
  is implementation at **M0**.

### 2026-07-10 — Added "Ripping" intent

- Intent enum extended to **personal | resale | ripping**. Ripping = sealed
  product bought to be opened, never sold.
- **Assumed opened no matter what**, so the loss is certain: a ripping lot is
  valued at **$0** and shows in inventory as an **unrealized loss = cost basis**.
  It stays visible in inventory, is **excluded from the sellable picker**, and
  never touches the realized-P/L line.
- Cards pulled from opening are entered **separately** as an ordinary acquisition
  in the Transaction window — **no linkage** to the ripped lot.
- Propagated across Specs 00/02/03/04 (F-TX-4, F-VW-10, invariant 8) + glossary
  and user guide.

### 2026-07-10 — Spec 04 ratified (interaction layer)

- **Live type-inference badge** on the Transaction window (PURCHASE/SALE/TRADE),
  plus a live totals/estimated-realized-P/L footer.
- **Validation defaults:** market value required on every incoming row (submit
  disabled otherwise); oversell blocked inline with a live remaining indicator;
  errors inline, never post-hoc modals.
- **Success screen** shows inferred per-card cost + realized P/L, then returns to
  a fresh Transaction with a toast linking to History.
- **Void asks for confirmation; Edit warns when realized P/L changes.**
- Keyboard-first entry (Enter adds a row, Ctrl+S submits); accessibility — color
  is never the only signal.

### 2026-07-10 — Spec 03 ratified

- **Items section reframed** around auto match-or-create (no manual "add item"
  step); TCGplayer/eBay links are item fields; duplicate-as-template dropped.
- **Transaction log / History is a full V1 feature (MUST)** — not deferrable —
  since it's the surface for voiding/editing misentries. Added as a nav view in
  Spec 04.
- **CSV import and export both included in V1** (export MUST; item import MUST;
  transaction import SHOULD). User is not necessarily migrating a spreadsheet but
  wants the capability.

---

### 2026-07-10 — Unified transaction window, profit at disposal, audit trail

- **Profit is recorded at disposal, never at purchase.** A purchase records cash
  out + cost basis; realized profit is the difference captured when cards leave
  (sale/trade). Dashboard series: money-movement = cumulative net cash;
  realized-profit = cumulative realized P/L; collection-value = market value
  over time.
- **Correction/audit.** Transactions are never hard-deleted. **Void** reverses
  all effects atomically (soft `status='voided'`, stays in history); **Edit** =
  void + re-post atomically. An append-only `audit_log` records every
  create/edit/void with before/after snapshots.
- **Keep item + lot; each acquired card is its own lot row.** Rejected a fully
  flat one-row-per-card table. The item holds shared identity + one current
  market value; the lot holds per-acquisition cost — so the same card bought at
  $90 and $75 is two lots (distinct basis), one item.
- **Merged Windows 2 & 3 into one bidirectional Transaction window.** A purchase
  is "cash out → cards in," a sale is "cards out → cash in," a trade is both.
  Type is **inferred** from which sides carry cards. Incoming cost basis =
  **consideration given** (cash out + agreed value of cards out) split across
  incoming cards by market value — the purchase rule is the cash-only corner
  case. App is now **three windows**: Dashboard, Transaction, Inventory.
- **Window 4 (Inventory): every field filterable and sortable** (incl. sold
  status, intent, value ranges).

---

### 2026-07-10 — Dual-purpose ledger, per-lot intent, market-value cost allocation

- **Dual purpose.** The platform serves personal collecting *and* business/resale
  simultaneously. Every card brought in is recorded regardless of resale intent.
- **Intent is per card / per lot** (not per acquisition, not per item). Each row
  in an acquisition is tagged **Personal** or **Resale**, so one lot purchase can
  mix keepers and resale cards, and the same item can be held under both intents.
- **Cost basis is inferred by market-value weighting, not entered.** The user
  enters a **total purchase price** for the acquisition and a **market value**
  per card; each card's cost basis =
  `total_price × (market_value / Σ market_values)`. This replaces the earlier
  "enter unit price + pro-rata shipping" approach.
- **Edge cases (resolved 2026-07-10):**
  - Market value is **required** on every row (submit blocked otherwise) — no
    missing-value fallback needed.
  - Rounding: allocated costs sum **exactly** to the total; residual cent(s) go
    to the highest-value card.
  - Intent change is a **plain editable toggle** (no dated history in V1).
  - **Condition greys out when a card is graded** (grade identifies the card).
  - Items **auto match-or-create** by identity key (name + set + condition +
    grade + grading company/number); no separate catalog step.

---

### 2026-07-10 — Baseline stack & scope

- **GUI framework: PySide6 (Qt 6).** Best-in-class table/model-view for an
  inventory grid; native look; LGPL is fine for personal use. Alternatives
  (Tkinter, Flet) rejected for weaker tables / less conventional CRUD.
- **Persistence: SQLite.** Zero-config, transactional, single portable file the
  owner can back up. Money stored as **integer cents** to avoid float errors.
- **V1 scope: core inventory + transactions.** Purchases, sales, trades on
  single-card or lot basis; cost basis & P/L; search/filter; CSV import/export.
  **No external price APIs in V1.**
- **Domain focus: Pokémon**, with a schema generic enough to add other games
  later.
- **Cost-basis method: FIFO** for V1; `lot_consumption` table keeps
  specific-lot / LIFO / average open for later without schema changes.
- **Layering:** domain + services are Qt-free and unit-tested headlessly; UI is
  a thin layer that never does SQL or money math.

---

_Template for new entries:_

```
### YYYY-MM-DD — <short title>
- **Decision:** …
- **Context / why:** …
- **Alternatives considered:** …
- **Consequences:** …
```
