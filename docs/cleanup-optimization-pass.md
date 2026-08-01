# Cleanup & Optimization Pass — Plan

## Context

The recognition + market roadmap (spec 06) is feature-complete. Before building
further, we're doing a hygiene pass to (1) fix code that diverges from the specs,
(2) remove orphaned code, and (3) optimize hot paths that degrade at realistic
scale (thousands of items/lots, ~47k-row catalog, months of daily price
snapshots). Three read-only audits (spec-conformance, orphaned-code, performance)
grounded in `specs/02-data-model.md` (the 9 invariants), `CLAUDE.md`, and
`docs/decisions.md` produced the findings below. Outcome: the money core is
sound — no high-severity spec violations — so this is mostly one real correctness
fix, targeted perf wins in `services/`+`data/`, dead-code removal, and stale-spec
reconciliation.

## ⚠️ Concurrent-agent boundary (blast-radius ground rule)

Another agent is actively editing the **UI/theme** layer. Treat these as
**off-limits unless a step explicitly coordinates**: `ui/views/auctions.py`,
`inventory.py` (the *view*), `item_editor.py`, `shows.py`, `calendar.py`,
`dashboard.py`, `main_window.py`, `theme.py`, `icons.py`, `ui/assets/icons/`.
Note: **`services/inventory.py` is a service, not the view** — safe to touch.
Stage only our own files by path; never `git add -A`. A red full suite may be
their WIP — confirm our tests pass and that other failures trace to their files.
(See memory `concurrent-agent-boundaries`.)

Everything in Phases 1–4 avoids these files entirely. Phase 5 touches
`recognition.py`/`transaction.py` (not in the concurrent set). Phase 6 touches
`dashboard.py`/`market.py` and is explicitly **coordinate-or-defer**.

---

## Phase 1 — Correctness: exact multi-quantity disposal proceeds (spec)

**Problem (MED-1).** `services/transactions.py:585-589` floors per-unit proceeds:
`unit_price_cents = share // qty`, then `realized = unit_price*qty - cost`. For a
multi-unit `out` line whose allocated `share` isn't divisible by `qty`, up to
`qty−1` cents of proceeds **and** realized P/L are silently dropped — even though
`_allocate_amount` made the shares sum exactly to `net_consideration_out`. The
incoming side reconciles exactly (invariant 6 two-lot split, `allocation.py:90-100`);
the outgoing side has no equivalent, breaking invariant 4 for stored figures.
Also `preview()` (`:403-404`) uses the **un-floored** share, so the live footer's
realized can disagree with what actually posts.

**Fix — mirror the incoming two-lot split on the outgoing side.** When
`share % qty != 0`, emit **two** `out` transaction_lines for that card:
`(qty − r)` units @ `floor` and `r` units @ `floor+1` (r = `share % qty`), so
`Σ unit_price×qty == share` exactly and each line's `realized = price*qty − cost`
is exact. Split the FIFO draw-down across the two lines (two `_consume_fifo`
calls in order) so `lot_consumption` stays per-line-correct. Divisible lines and
all qty-1 lines are unchanged (the common case).

- **File:** `services/transactions.py` — the outgoing loop in `_post` (`:564-589`).
  Reuse the existing `_allocate_amount` (`share` per card) and `_consume_fifo`.
- **Verify:** `preview()`'s realized already uses full shares, so post now matches
  it — assert `preview.realized == posted realized` for a multi-qty indivisible
  sale.
- **Blast radius:** money-critical path, but heavily covered by
  `tests/test_transactions.py`. Visible behavior change: an indivisible multi-unit
  disposal now shows **two** history lines for that card (rare). HIGH care; its
  own commit; add targeted tests (multi-qty indivisible **sale** and **trade-away**
  asserting exact realized+revenue, invariants 4 & 6, preview==post).

---

## Phase 2 — Spec/doc reconciliation (docs only, zero code risk)

Code follows later ADRs that `specs/02-data-model.md` / `CLAUDE.md` were never
updated to match. Update the docs (no code):

- **LOW-2:** `lot.sku` is no longer `UNIQUE`/"per-lot" — it's per-acquisition-line,
  non-unique (migration `0016`, `services/sku.py`). Fix the schema block + prose
  in spec 02 (`:216-269`).
- **LOW-4:** `holo_pattern` + `edition` are in the item **identity key** + schema
  (`repositories._ITEM_IDENTITY_COLUMNS`, migration `0011`) but absent from spec
  02. Add them to the identity key (`:10-17`) and `item` schema block; add a
  decision-log entry.
- **LOW-5:** cost formula wording — spec 02 (`:81`, invariant 6) still says
  "agreed value of cards out"; ADR 2026-07-13 switched to **current market
  value** (`transactions._deal_values`). Update the formula/invariant wording.
- **LOW-3:** annotate the "transactions are never hard-deleted" rule (CLAUDE.md,
  spec 02 `:369`) to carve out the **sanctioned** `delete_inventory_entry`
  hard-delete exception (ADR 2026-07-11), which already writes an audit `DELETE`.

- **Files:** `specs/02-data-model.md`, `CLAUDE.md`, `docs/decisions.md`.
- **Blast radius:** none (documentation). One commit.

---

## Phase 3 — Performance: service/data hot paths (no concurrent UI)

Each is a self-contained service/data change; verify output-equivalence with
existing tests + add a couple of focused tests. Ranked by impact.

- **H2 — exemplar reload per box.** `Recognizer.suggest` →`_exemplars_by_card`
  (`recognizer.py:206-214`) reloads + `np.frombuffer`-parses the **entire**
  `region_embedding` table on every call, once per detected box. Fix: parse once
  and cache on the `Recognizer` (invalidate on model-version change), and/or add a
  `region_vectors_for_cards(version, ids)` in `data/vision.py` filtering
  `WHERE card_id IN (…)` (index `idx_region_embedding_card` already exists).
  **Files:** `services/recognizer.py`, `data/vision.py`.
- **M2 — missing index.** Disposal-by-lot aggregations full-scan `lot_consumption`
  (`inventory.py:93`, `reporting.py:495`, `repositories.list_for_lot`). Add
  `CREATE INDEX idx_consumption_lot ON lot_consumption(lot_id);` as ledger
  migration **`0018`** (verify still free — the concurrent agent added `0017`).
  **Files:** `data/migrations/0018_lot_consumption_lot_index.sql`.
- **M3 — `latest_prices()` full read.** `repositories.py:706-715` loads the whole
  `price_snapshot` table into Python on every Inventory refresh to derive one
  latest price/item. Fix: `MAX(as_of)` group-join in SQL (mirror
  `reporting._earliest_prices`). **File:** `data/repositories.py`.
- **H1 — collection-value reconstruction.** `_reconstruct_collection_series`
  (`reporting.py:535-549`) is an O(dates × lots) double loop (dates grows with
  price-history depth) whose result is mostly filtered away
  (`collection_value_series:477`). Fix: single sweep-line over lots in
  acquired-date order, or only reconstruct the pre-recording prefix.
  **File:** `services/reporting.py`.
- **M1 — market-signal N+1.** `market_signal.py:152-154` runs one
  `history_for_item` per resale item for volatility. Fix: one grouped
  `price_snapshot` query since the window, bucketed in Python. **Files:**
  `services/market_signal.py` (+ maybe a batch helper in `data/repositories.py`).
- **L1 — inventory positions N+1.** `sellable_items`/`on_hand`/`all_positions`
  (`inventory.py:97-157`) loop items and call `lots.list_for_item` per item;
  `sellable_items` runs live on the give-row path. Fix: one grouped `lot` query by
  `item_id` (+ intent), aggregate in a pass. **File:** `services/inventory.py`
  (+ a grouped query in `data/repositories.py`).

- **Blast radius:** `data/repositories.py` is central (M3/M1/L1 ripple to
  inventory/reporting/market) but well-tested — assert identical outputs.
  Migration `0018` bumps `schema_version` (coordinate the number). H2 changes a
  vision hot path — cover with a test that a 2nd `suggest` doesn't re-query.
  Suggest 2–3 commits (index; repo/query batching; recognizer cache; reporting
  sweep).

---

## Phase 4 — Orphaned-code removal

- Remove the fully-dead `VisionRepository.list_images` and `captures_with_card`
  (`data/vision.py:392,414` — zero references repo-wide).
- Remove the dead M-V0 `capture_card` path (superseded by `region`): the
  `capture_card` table, `add_capture_card`/`capture_card_count`
  (`data/vision.py`), `ingest_capture` (`services/vision.py:60`; **keep**
  `store_file`/`_unpack`, which are live), and their tests
  (`tests/test_vision.py:33/43/52/53`). Drop the table in `vision.db`'s own schema
  path (add `DROP TABLE IF EXISTS capture_card` in `data/vision.py`
  `open_vision`/`_reset_legacy` — **not** a numbered ledger migration; vision.db
  is separate).
- Consolidate the byte-identical `_safe()` slug helper: have
  `services/condition.py:183` import it from `services/exemplars.py` (already
  imports `crop_region` from there).

- **Files:** `data/vision.py`, `services/vision.py`, `services/condition.py`,
  `tests/test_vision.py`.
- **Blast radius:** low, isolated to vision.db (not the ledger). Deferred/kept:
  the `_money_spin`/`_combo`/`_select` UI-helper duplication is **[CONCURRENT-UI]**
  — leave for later coordination. One commit.

---

## Phase 5 — Off-thread vision inference (optional extra — chosen)

**H3.** Detection + embedding + ONNX + per-box `suggest` run synchronously in
click handlers (`recognition._run_suggestions:732-744`,
`transaction._autofill_from_photo:900-904`), and `build_pipeline` loads the ONNX
model on first use on the UI thread — freezing the window for seconds. Move
pipeline-build and the `pipeline(path)`+`suggest` loop onto a `QThread` worker,
delivering results by signal (reuse the pattern in `ui/views/inventory.py:247-292`).

- **Files:** `ui/views/recognition.py`, `ui/views/transaction.py` (neither in the
  concurrent set).
- **Blast radius:** medium; large files but not concurrent-owned. Keep the
  injectable-pipeline seam so offscreen tests stay synchronous; ensure the worker
  is cleaned up on view close. Its own commit.

### Phase 5 outcome (executed — changed approach)

A full `QThread` worker was blocked by **SQLite thread-affinity**: the recognizer
carries main-thread connections (tests even use in-memory DBs), which can't be
used from a worker, and the fix would have forced worker-owned connections +
refactoring the sync test seams. **Chosen instead (owner-approved): background
model prewarm.** A process-wide shared embedder (`recognizer.get_shared_embedder`
/ `prewarm`) is loaded on a daemon thread when the Recognition window opens
(`RecognitionView(prewarm=True)`, skipped under pytest), so the first Suggest /
Transaction-autofill no longer pays the multi-second model-load freeze. Per-box
embedding still runs on the main thread but is sub-second for a handful of cards.

---

## Phase 6 — UI-adjacent perf (optional extra — chosen; COORDINATE / do last)

- **M4:** `reporting.top_gainers`/`top_losers` each recompute `_price_movers`
  (holdings group-by + full `price_snapshot` baseline build) — twice per dashboard
  refresh. Add a single `price_movers()` in `reporting.py` (safe) that returns
  both; the dashboard caller change is in **`dashboard.py` [CONCURRENT]**.
- **M5:** catalog `search` (`repositories.py:987`) is a leading-`%wildcard%` full
  scan of ~47k rows per keystroke from **`market.py`** with no debounce. Anchor
  head-tokens to prefix to use `idx_catalog_name` (safe, repo side) + debounce
  input in the view (UI).

- **Blast radius:** HIGH coordination risk — `dashboard.py`/`market.py` are under
  active edit. Do the **reporting.py**/`repositories.py` service-side halves first
  (safe), and sequence the view edits after the other agent's UI churn settles, or
  hand them the one-line caller change. May defer entirely.

### Phase 6 outcome (executed)

- **M4 DONE.** Added `ReportingService.price_movers()` (one-pass gainers+losers)
  and switched the dashboard's two `top_gainers`/`top_losers` calls to it —
  `dashboard.py` was clean at the time, edit was surgical and committed
  immediately. `top_gainers`/`top_losers` kept for existing callers/tests.
- **M5 DEFERRED (coordinate).** The catalog-search prefix-anchor changes search
  *results* (contains → prefix), not a pure perf fix; the real win (debounce) is a
  UI-behaviour change in the concurrently-owned `market.py` for a ~few-ms 47k-row
  LIKE scan. Low value / higher coordination risk — left for a coordinated pass
  once the concurrent UI/theme work settles.

---

## Sequencing & commits

1. Phase 2 (docs) — zero risk, quick.
2. Phase 4 (orphan removal) — low risk, isolated.
3. Phase 1 (MED-1 correctness) — own commit, careful, new tests.
4. Phase 3 (perf) — 2–3 commits (index migration; repo/service batching + caches;
   reporting sweep).
5. Phase 5 (off-thread vision).
6. Phase 6 (UI-adjacent) — service halves now; view halves coordinated/last.

Small, single-purpose commits straight to `main` (per `work-off-main`); stage
files by explicit path.

## Verification

- **After each phase:** `ruff check` the changed files; run the affected test
  modules, then the full suite excluding the concurrent agent's WIP
  (`--ignore=tests/test_trends.py` and skip failing auction/inventory-view tests
  that trace to their files). Confirm every remaining failure is theirs.
- **Phase 1:** new `tests/test_transactions.py` cases — multi-qty indivisible sale
  & trade-away: exact realized + revenue, invariants 4 & 6 hold, `preview()`
  realized == posted realized.
- **Phase 3:** output-equivalence tests (same inventory/reporting/market numbers
  before/after); a `Recognizer.suggest` test asserting the exemplar table is read
  once, not per-box; confirm migration `0018` applies on a fresh DB and
  `schema_version` advances.
- **Phase 4:** full vision test suite green after removals; fresh `vision.db` has
  no `capture_card` table; existing `vision.db` files drop it cleanly on open.
- **Phase 5:** offscreen recognition/transaction autofill tests still pass
  (injected pipeline stays sync); manual smoke that the window stays responsive.
- **End:** rebuild the PyInstaller exe and boot-verify (per `rebuild-after-task`),
  attributing any boot issue to the concurrent UI WIP if present.
