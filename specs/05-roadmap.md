# 05 — Roadmap

Milestones are ordered to get a **correct, testable core** working before UI,
and to defer anything requiring external services. Each milestone should end in
a runnable, committed state. IDs in parentheses trace to
[Features](03-features.md).

## M0 — Project scaffolding
- `pyproject.toml`, `ruff`, `pytest`, package layout from
  [architecture](01-architecture.md) (`src/tcginventory/{domain,data,services,ui}`).
- `config.py` (OS-appropriate DB path resolution, env override), logging.
- Python **3.10+**; CI-ready test command; empty PySide6 window launches.
- **Exit:** `python -m tcginventory` opens a blank main window; `pytest` runs.

## M1 — Data layer & migrations
- SQLite connection helper (WAL, `foreign_keys=ON`), `schema_version` +
  forward-only migration runner (runs pending scripts in a transaction).
- `0001_init.sql` implementing the [schema](02-data-model.md#schema-sqlite-v1):
  `item`, `lot` (with `intent` + `acquired_market_value_cents`), `transaction`
  (with `status`/`voided_at`), `transaction_line`, `lot_consumption`,
  `audit_log`.
- Domain dataclasses (no Qt) + repositories (CRUD for each entity).
- **Exit:** migrations apply on a fresh DB; repository unit tests pass.

## M2 — Core services & money math *(highest-risk, do first)*
The correctness core, proven headlessly before any UI exists.
- **`TransactionService`** — the **one unified posting algorithm**
  ([Spec 02](02-data-model.md#posting-a-transaction-the-one-unified-algorithm)):
  type inference (purchase/sale/trade), FIFO lot draw-down, market-value-weighted
  allocation of `net_consideration_in` (cash out + cards out − cash in) with
  largest-line rounding residual, per-row **intent** (personal/resale/ripping),
  and `adjustment`. Item **match-or-create** by identity key.
- **Void / Edit** — atomic reverse (and reverse+re-post) with `audit_log`
  entries (F-DA-1/2/3).
- **`InventoryService` / `ReportingService`** — per-lot valuation (**ripping lots
  valued at $0** → loss = cost basis), unrealized/realized P/L, and the three
  dashboard series (money-movement, realized-profit, collection-value), all
  intent-filterable.
- Enforce every [invariant](02-data-model.md#invariants-must-always-hold);
  typed exceptions (e.g. `InsufficientQuantityError`).
- Unit + property tests, incl. all
  [acceptance scenarios](03-features.md#acceptance-scenarios-representative).
- **Exit:** all service tests green; invariants hold under fuzz/property tests;
  zero Qt imports in domain/services.

## M3 — Read-only UI over real data
- Main window shell + nav (Dashboard · Transaction · Inventory · History).
- **Dashboard** — the two line graphs via **QtCharts**: money-movement &
  realized-profit, and collection-value (with cost-basis comparison);
  intent filter (F-VW-6/7/8).
- **Inventory** grid with **every column sortable + filterable** (F-VW-9),
  quick-filter presets (F-SE-1/2), intent & sold-status columns.
- **History** — full transaction log with filters + transaction detail
  (F-VW-3).
- **Item detail** — attributes, open lots, history (F-VW-2).
- **Exit:** a seeded DB renders correctly across all read views, including a
  ripping lot showing as a loss.

## M4 — Write UI (the unified Transaction window)
- **One bidirectional Transaction window** (F-TX-*): cards/cash out ⇄ cards/cash
  in, **live type-inference badge**, live totals + estimated realized-P/L
  footer, per-row intent, incoming-market-value-required + oversell-blocked
  validation, ripping lots excluded from the out picker, success screen.
- **Item editor** (edit/maintain only — creation is automatic): links, market
  value, location, soft-delete. (SKUs are auto-generated per lot, not edited.)
- **Void / Edit** actions wired from History (atomic reverse+re-post; warns on
  P/L change).
- **Exit:** full purchase→sale→trade→rip lifecycle done entirely through the UI,
  reconciling with History and the invariants.

## M5 — Import / export & backup
- CSV **export** of items, lots, and transactions; full portable backup
  (F-IE-1/4).
- CSV **item import** with column mapping + validation preview (F-IE-2);
  transaction import (F-IE-3, SHOULD).
- "Backup database" action (F-DA-5); optional timestamped startup auto-backup
  (F-DA-6).
- **Exit:** round-trip export → wipe → import reproduces the dataset.

## M6 — Packaging & polish
- PyInstaller build for macOS/Windows; app icon; first-run DB setup.
- Empty states, keyboard-flow pass, accessibility (color never the only cue),
  error-dialog polish.
- Finalize the user guide in [`/docs`](../docs).
- **Exit:** distributable build runs on a clean machine.

## Post-V1 (future work)

Prioritized backlog once the baseline is solid:

1. **Price feeds** — TCGplayer / PriceCharting / eBay sold comps to auto-update
   market value (biggest value-add; deferred for API/keys complexity).
2. **Explicit Rip/Open event** — finalize a ripping lot as a realized loss and
   remove it from on-hand, optionally **linking pulled cards** to the opened
   product (V1 enters pulls separately with no linkage).
3. **Dashboard calendar** — upcoming shows & auctions, plus extra summary tiles.
4. **Specific-lot & alternative cost-basis methods** (LIFO, average).
5. **Images & scanning** — item photos, barcode/QR quick-add, card OCR.
6. **Reporting exports** — P/L by period, tax-oriented summaries (business intent).
7. **Bulk operations** — bulk edit, bulk price update, saved filter presets.
8. **Intent-change history** — dated reclassification log (V1 is a plain toggle).
9. **Multi-currency** and locale formatting.
10. **Cloud sync / backup** (e.g. to a user's Drive) — optional, opt-in.
11. **Other games** — MTG/sports-specific fields and imports.

## Sequencing rationale

- **Money math (M2) before UI** so the correctness-critical logic — allocation,
  FIFO, intents, void/edit — is proven headlessly and can't be masked by UI bugs.
- **Read UI (M3) before write UI (M4)** to validate the data model against real
  rendering before building the more complex unified entry window.
- **External integrations deferred entirely** to keep V1 offline, portable, and
  free of API/rate-limit/keys concerns.
