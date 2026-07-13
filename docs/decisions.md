# Decision Log

Lightweight ADR-style record of significant decisions. Newest first.

---

### 2026-07-13 — Transaction value derived from the deal (drop "agreed value")

- **Decision:** Remove the per-outgoing-card manual "agreed value". A card's
  value is now derived from the whole transaction:
  - **Cards received** get cost basis from `cash_out + Σ(given cards' current
    market value) − cash_in` (allocated by market weight, unchanged mechanism).
  - **Cards given** realize P&L against `cash_in + Σ(received cards' market
    value) − cash_out`, split across them by market weight;
    `OUT line.unit_price_cents` = per-unit proceeds (what reporting reads).
- **Why:** the owner wanted the give side to carry no manual value — "the price
  is determined by all the cards involved." Given cards are valued at their
  current market value; received cards at their entered market value.
- **Behaviour:** pure sale → proceeds = the cash in (unchanged P&L); pure
  purchase → unchanged; **trades change** — a received card's basis now uses
  the given card's *market* value (not an arbitrary agreed number), e.g. give a
  $20-market card + get $10 back → received card basis $10 (was whatever agreed
  value implied). Helpers: `_deal_values`, `_allocate_amount` (money math,
  tested). `OutgoingCard.agreed_value_cents` removed.
- **UI:** give-side "Agreed Value" column gone; add/remove are now a `＋` and a
  per-row `✕`. (Full inline receive-entry with catalog autocomplete is next.)

---

### 2026-07-13 — Card-show attendance status (denied/pending/attending/walking)

- **Decision:** Each show can be marked with an attendance status —
  denied (red), pending (orange), attending (green), walking (blue) — via four
  colour buttons per show in the day list. Statused shows sort to the top of a
  day's cell and render with a coloured highlight bar; the day list mirrors the
  ordering.
- **Storage:** a separate `card_show_status(source_id, status)` table (migration
  0007) keyed by tcdb's show id, **not** a `card_show` FK — so it survives the
  wholesale delete+reinsert a state refresh does. Joined onto `CardShow.status`
  on read; absence = no status.

---

### 2026-07-13 — Card-shows calendar: data layer (scrape tcdb via curl)

- **Decision:** Add a shows data layer for the planned Dashboard calendar:
  migration `0006_card_show`, a `CardShow` model + `CardShowRepository`
  (upsert-by-tcdb-id, date-range/state queries, past pruning), and
  `services/cardshows.py` — a Qt-free parser + `CardShowService.refresh(states)`
  that replaces each state's stored shows with a fresh scrape.
- **Source:** tcdb `CardShows.cfm?MODE=Location&State=<code>&Display=<name>`.
  The list page alone yields date, name, venue, city, time range, and a detail
  link per show — no per-show fetch needed.
- **Fetch via `curl`, not httpx:** tcdb is behind Cloudflare, which 403s Python
  HTTP clients on TLS fingerprint (even with browser headers) but lets the
  system curl through. `_fetch_html` shells out to curl; the injected-fetcher
  design keeps parsing/refresh unit-testable offline and makes swapping to
  `curl_cffi` a one-function change if curl is later blocked.
- **Scope:** background refresh covers **only the user-toggled states**. UI
  (month calendar + per-cell show names, state multi-select, "TCG only" toggle)
  and the daily-task integration come next; this increment is source-only
  (the packaged exe isn't rebuilt until the UI lands).

---

### 2026-07-13 — Continuous background price updates (headless + Task Scheduler)

- **Decision:** Prices now refresh in the background even when the GUI is
  closed. A headless `--refresh-prices` entry point (`app.refresh_prices_headless`,
  no Qt/display) does one poll-and-record pass; a **Windows Scheduled Task**
  (`scheduling.py`, via `schtasks`) runs it **once daily**. The packaged app
  **self-registers the task on startup by default** (default ON) and exposes a
  File-menu toggle ("Automatic daily price updates") to turn it off; the choice
  persists in `QSettings` so a user who opts out isn't re-enrolled.
- **Context / why:** Refresh only happened on GUI launch (throttled) or manual
  click, so price history had gaps whenever the app wasn't opened. The owner
  wanted continuous history with no manual step.
- **Why daily:** tcgcsv mirrors TCGplayer roughly once a day, so one snapshot
  per day captures all real movement; more frequent polls would mostly dupe.
- **Scoping choices:** startup self-registration is gated to the **frozen** app
  so running from source / the test suite never shells out to `schtasks`; the
  task's command points at `sys.executable` (the exe) so it survives rebuilds
  in place. Windows-only (macOS/Linux would need launchd/cron — deferred).

---

### 2026-07-11 — Hard-delete a mistaken inventory entry (exception to soft-void)

- **Decision:** Add `TransactionService.delete_inventory_entry(lot_id)` — a
  true hard delete of a single lot, invoked from a "Delete entry…" button in
  the Inventory view. It removes the rows for good (not a void) and is recorded
  in `audit_log` as a new `AuditAction.DELETE`.
- **Context / why:** The architecture rule is "transactions are never
  hard-deleted." But the owner needs to erase *blatant/junk entries* entirely;
  a soft-void leaves them cluttering History. Real purchases here are
  multi-card, so deleting the whole transaction to remove one bad row would
  destroy good data — hence per-lot granularity.
- **No business guardrails (revised same day):** the first cut blocked deletes
  when the lot had been sold or came from a trade / cash-in transaction. That
  got in the way of cleanup, so it was removed — the owner is trusted to prune
  their own data. Delete now always succeeds; the only work is keeping the DB
  internally consistent: consumption records that drew *from* the lot are
  removed (the sale stays, minus its cost-basis link), a plain cash-out
  purchase has its cash reduced by the lot's basis, the acquiring line/txn are
  removed once empty, and orphaned items are dropped.
- **Consequences:** History no longer perfectly reproduces every entry, and a
  deleted lot that had sales against it leaves that sale's realized P/L as a
  historical figure with no live basis link. The append-only `audit_log` keeps
  a `before_json` record so the deletion itself stays traceable. For surgical,
  reconciled changes (rather than deletion), Edit remains the tool.

### 2026-07-11 — Closest-match set→group resolution for pricing

- **Decision:** When resolving an item's set to a tcgcsv group, try an exact
  normalized match first (on `set_code` then `set_name`, plus the name after any
  `SVxx:` prefix), then fall back to the **closest fuzzy match**
  (`difflib` ratio ≥ 0.6, with substring hits treated as strong). 
- **Context / why:** tcgcsv names sets inconsistently ("SV09: Journey Together")
  and users type them loosely ("Journey Together"), so exact matching silently
  dropped whole sets from pricing.
- **Why it's safe:** the price join is by **product ID** (from the item's
  TCGplayer URL), and a product belongs to exactly one group — so a wrong fuzzy
  match yields *no* price, never a wrong one. Fuzzy matching only ever helps.
- **Consequences:** far fewer unmapped items; the group lookup is just a
  performance narrowing over which price file to download.

---

### 2026-07-10 — "Mark for Review" (manual, poll-protected prices)

- New `item.review` flag (migration `0005`), set by a checkbox at card entry.
  For a reviewed item, auto-refresh **still records price_snapshots** (history/
  chart keep filling) but **does not overwrite its shown Market Price Current** —
  the value is user-maintained (for cards tcgcsv can't price: LP/MP, JP, graded,
  sealed).
- Inventory current source: reviewed items use their manual
  `item.market_value_cents`; others use the latest poll. Reviewed cells are
  amber-highlighted and the Market Price Current cell is **inline-editable**.
- Each reviewed cell has a **two-stage button**: click 1 arms it (previews the
  unmark — highlight off, label → "Refresh"); click 2 commits (unmark + adopt the
  latest polled price). It reverts to stage one if the selection moves or the
  page changes. Implemented as a `QStyledItemDelegate` (survives sort/filter/
  scroll, unlike per-cell index widgets).

---

### 2026-07-10 — Live market pricing via tcgcsv + price history

- **Source: tcgcsv.com**, a free JSON mirror of TCGplayer's pricing, joined by
  the TCGplayer **product ID** parsed from `item.tcgplayer_url` (set → tcgcsv
  group resolved from the item's set code/name). Chosen over scraping
  tcgplayer.com (JS-rendered, ToS, Cloudflare, heavy) and over pokemontcg.io.
  The `PriceProvider` abstraction lets another source slot in later.
  - **Limits:** product-level market price only (≈ NM English); no per-condition
    (LP/MP) and no Japanese; ripping stays $0.
- **History stored** in `price_snapshot` (migration `0004`) — one row per item
  per refresh. Drives the live "Market Price Current"/"Unrealized P/L" columns
  and the Inventory price-history chart, and will unlock the deferred
  collection-value-over-time dashboard series.
- **Refresh: both** — a throttled auto-refresh on startup (skipped if the newest
  snapshot is <12h old) plus a manual **File ▸ Refresh prices**. Runs on a
  background `QThread` with its own DB connection so the UI never blocks;
  network/parse failures are captured, never fatal.
- **Inventory price plot:** selecting a card shows its market price over the last
  90 days with flat **Cost** and **Market at ToP** reference lines.
- Dependency added: `httpx` (plain JSON; no headless browser).

---

### 2026-07-10 — Per-copy SKUs on the lot (auto-generated)

- **SKUs moved from the item to the lot.** A SKU identifies an acquired *copy*,
  formatted `{NAME3}-{SET}-{NUMBER}-{COND|GRADE}-{LANG}-{NNNN}` (e.g.
  `CHA-CPA-074-NM-EN-0001`). This matches the user's spec: each new card gets a
  new SKU, and `NNNN` sequences per identity (NM separate from LP). Because our
  model merges identical copies into one *item* (with multiple lots), the SKU
  had to live on the **lot** for the sequence to mean "how many copies exist."
- **The old manual `item.sku` was retired** (migration `0003_lot_sku.sql`:
  `lot.sku` + partial unique index; `item.sku` dropped). Existing lots are
  backfilled with SKUs on startup (`services/sku.backfill_skus`).
- **Sequence** = distinct existing SKUs for the identity + 1, bumped on the rare
  cross-identity collision (from name truncation) to stay globally unique.
- **Lookup**: `LotRepository.find_by_sku`; the give dialog resolves a typed SKU
  to its card, and the Inventory search matches SKUs. Set/number/grade segments
  fall back to `NA`/`GR` when absent (e.g. a sealed box → `BRI-NA-NA-SEALED-EN-…`).
- Work is on `main` directly (no feature branch), per the new workflow.

---

### 2026-07-10 — M6 implementation: packaging & polish

- **PyInstaller onedir** is the default build (`packaging/tcginventory.spec`);
  faster/more debuggable than onefile, still a zip-and-share distributable. A
  `ONEFILE` flag flips to a single exe. The spec bundles the SQL migrations and
  icon as data and pins `PySide6.QtCharts` as a hidden import.
- **App icon** generated programmatically (indigo→teal "TCG"), committed as
  `icon.png` + `icon.ico`; loaded via `importlib.resources` so it works framed
  and frozen.
- Polish: empty-state notes, **Ctrl+S** to submit, accessibility tooltips/names,
  finalized user guide. Verified the frozen exe boots on its own.

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
