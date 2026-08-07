---
name: optimization-sweep
description: Find and fix real performance costs in TCGinventory — wall-clock latency, resident memory, and bytes on disk. Use when asked to make the app faster, fix a slow window or startup, reduce memory or RAM use, shrink the database or image cache, hunt N+1 queries or missing indexes, or profile something. Also use when the user types /optimization-sweep.
argument-hint: <area to scope the sweep, optional> — e.g. the Models window, startup, vision.db, the image cache
allowed-tools: [Read, Grep, Glob, Bash, PowerShell, Edit, Write]
---

# Optimization sweep

Scope: **$ARGUMENTS** — if that is empty, sweep the whole platform, in the order
the harness ranks things.

Three axes, because they disagree with each other and a sweep that watches only
one will miss the biggest item in the repo:

- **Latency** — wall clock the user waits on: startup, nav switch, refresh, typing.
- **Memory** — resident bytes. `RecognitionView` builds in 198 ms and costs
  112 MB; a stopwatch cannot see that.
- **Storage** — bytes on disk. 8.7 GB of app data, of which 2.2 GB is a PNG cache.

The governing rule, and it is not negotiable:

> **No edit without a before number and an after number, both from the harness.**

This repo already works that way. `docs/decisions.md` (2026-08-07) records a
`refresh()` going **4201 ms → 58 ms** under the heading *"Measured, not
guessed."* The same log records *"A performance worry that was not real"* —
`environment_summary()` looked expensive, measured 0.82 s cold and 0.07 s warm,
and **no change was made**. Both halves are the job. Deleting a finding because
it measured small is as valuable as fixing one that measured large.

---

## Step 0 — Ground rules

| File | Why |
|---|---|
| `CLAUDE.md` | The non-negotiable architecture rules. They outrank every idea below. |
| `specs/02-data-model.md` | The money and schema invariants. |
| `references/baseline.md` (next to this file) | What was measured on 2026-08-07 and how. Read it before running anything — it will stop you rediscovering settled facts. |
| `docs/decisions.md`, newest first | Several performance ADRs. Something that looks slow is often a decision. |

Interpreter and tools (Windows is primary; both given):

```bash
# Windows                          # POSIX
.venv/Scripts/python.exe           .venv/bin/python
.venv/Scripts/ruff.exe             .venv/bin/ruff
```

Search with the **Grep tool**, not shell pipelines — it honours `.gitignore`, so
`.venv/`, `__pycache__/` and `dist/` are already out.

---

## Step 1 — Measure, before reading a single line of code

```powershell
.\.venv\Scripts\python.exe .claude\skills\optimization-sweep\scripts\perf.py all
```

Subcommands, when you want one axis: `latency [--views|--services]`, `memory`,
`storage`, `plans`, `profile <View>`, `all`.

The harness copies `inventory.db` to a temp file and points `TCGINVENTORY_DB` at
the copy, so it never holds a handle on the file the app writes to. `storage`
opens everything `mode=ro`. Nothing it does can damage the user's data.

Three things about its output:

- **It exits 0 by calling `os._exit(0)`.** Qt teardown without an event loop
  raises an access violation — the same total behind `tests/conftest.py`'s
  deferred-deletion fixture. The measurement is already printed by then.
- **View build times swing run to run.** Recognition's prewarm thread competes
  with whatever is constructed after it; Transaction has read 13 ms and 219 ms in
  consecutive runs. Trust the total and the outliers, re-run before believing a
  small gap.
- **Memory deltas are only meaningful after settling.** The harness calls
  `processEvents()` for two seconds between views for exactly this reason.
  Without it, async allocation is billed to the *next* view — that is how a
  105 MB cost first appeared under `MarketView`, which allocates nothing.

Then pick your targets **from the ranking**, not from intuition. If `$ARGUMENTS`
named an area, still run the full harness first: you need to know whether that
area is even in the top ten before you spend the budget on it.

To go one level deeper on a target:

```powershell
.\.venv\Scripts\python.exe .claude\skills\optimization-sweep\scripts\perf.py profile Inventory
```

---

## Step 2 — What is big here, and what only looks big

This is the section that earns the skill. TCGinventory's ledger is **tiny** and
its catalog is **large**, and almost every plausible-sounding optimization is
aimed at the wrong one.

| Big enough to matter | Rows |
|---|---:|
| `catalog_price_snapshot` | 223,893 |
| `card_catalog` | 53,289 |
| `card_embedding` | 49,551 |
| `catalog_price_latest` | 49,493 |
| `price_snapshot` | 23,583 |
| cached card PNGs on disk | 50,546 |

| Not big. Leave it alone. | Rows |
|---|---:|
| `transaction_line` | 1,320 |
| `lot` | 1,105 |
| `item` | 903 |
| `transaction` | 262 |
| `lot_consumption` | 213 |

**Do not spend the sweep on any of these**, all measured:

- `InventoryService.all_positions()` — a genuine N+1, two queries × 903 items,
  **25 ms**, and no view calls it. The most tempting non-finding in the repo.
- Every `ReportingService` method except `collection_value_series` (44 ms) —
  they run in **0.2–3.5 ms**. There is nothing there.
- `VACUUM` — the freelist is **0 pages in all four databases**. It reclaims
  nothing. Re-check `PRAGMA freelist_count` before ever proposing it.
- `SELECT *` in `data/repositories.py` — 25 of them, all against ledger tables of
  a few hundred rows, all index-backed.
- A `SCAN` over a table under ~20,000 rows. The harness marks these; say so and
  move on.

**Standing decisions — do not propose these, in a finding or in prose:**

- **Lazy view construction.** Yes, `main_window.py:132-158` builds all eleven
  views eagerly and that is ~2 s of startup. It is out of scope by decision. Do
  not raise it.
- **A performance-regression test.** Out of scope by decision. The suite runs
  `-n auto`; a timing assertion in it would be flaky. Results go in the ADR.
- Vectorising, batching or reordering anything in `services/allocation.py` or
  `services/transactions.py`. They cost ~0.3 ms and they are the money path.

---

## Step 3 — The shapes that are actually real here

Each of these was found by measurement in this repo, and each names where it was
found. Look for more of the same shape; do not assume the listed instance is the
only one.

### 3a. Latency

**Repeated derivation inside `data()`.** `ui/models/inventory_model.py:350` is
called **31,598 times** for a 1,105-row grid — Qt asks per row *per role*, so
~28 calls each — and rebuilds strings, fonts and URLs every time. The fix is to
compute the display tuple once in `set_rows()` and have `data()` index into it.
Check all three Qt models (`inventory_model`, `history_model`,
`recognition_history_model`) and the five delegates in `ui/views/_*_delegate.py`.
Precedent: `ui/thumbcache.py` already does this for pixmaps (ADR 2026-08-04).

**The same expensive call, twice in one pass.** `catalog_index.index_status` runs
twice per `ModelsView` build — 0.43 s, each one constructing the shared ONNX
embedder. ADR 2026-08-07 fixed the identical shape in Recognition
(4201 ms → 58 ms) by having the summary take what it needs as arguments instead
of rebuilding it. Grep for a helper called more than once in one refresh.

**A big-table query from inside a per-row loop.** `InventoryView.refresh()`
spends 0.37 s in `fetchall`, nearly all of it `repositories.image_for()` firing
27 `gather()` queries at the 53k-row `card_catalog`. The queries are individually
index-backed; the problem is the count. Batch them, or cache the resolution.

**Filesystem walks.** `trainingenv._walk_size` costs 0.28 s across **6,139
`nt.scandir` calls** during `ModelsView` build. `catalog_index` globs the 50,546
PNGs in `cache_dir()` in four places. ADR 2026-08-07 already made one such walk
opt-in after it cost 2.07 s for two integers a 3 ms `COUNT(*)` already had.

**`subprocess` and model loads on the UI thread.** `ModelsView.refresh()` spends
44 ms shelling out to `nvidia-smi`. `get_shared_embedder()` builds an ONNX
session (0.13 s) on a path a user is waiting on. Cache the probe, or move the
work behind `ui/views/_worker.py`'s `TaskWorker` — the repo's standard for "call
a service, report a sentence".

**First-touch imports on a UI path.** `services.training` (0.20 s) and
`services.detector` (0.18 s) are imported lazily, which just relocates the cost
to whoever clicks first.

### 3b. Memory

**Prewarming something the user may never open.** `RecognitionView(prewarm=True)`
costs **112 MB** for the DINOv2 ONNX session, at startup, for everyone. Build
time barely moves (198 ms vs 182 ms), so this is invisible to latency work.

**Whole result sets held when a window would do.** Anything reading
`card_embedding` (49,551 × 1,589 B ≈ 79 MB) into a list before reducing it.
Check `services/catalog_index.py`, `services/recognizer.py`,
`services/identity_projection.py` for `np.vstack` over the full pool.

**Cache limits set once and forgotten.** `ui/thumbcache.py:26` pins
`QPixmapCache` at 32 MiB. That is a deliberate ADR, not a bug — but it is a real
32 MiB and it belongs in the accounting.

When a delta looks wrong, **isolate it**: build that one view in a fresh process
and re-measure after two seconds of `processEvents()`. Do not report an RSS
delta you have not isolated.

### 3c. Storage

**The image cache is the biggest app-owned item on disk**: 2,212 MB across
50,546 PNGs, 200×279 RGB, 48.8 KB average (`services/cardimages.py:33` writes
`.png` unconditionally). Measured on a 25-file sample: WebP q85 averages 19.7 KB
— **60% smaller, ~1.2 GB saved**; JPEG q88 is 46% smaller. Qt and Pillow both
read WebP, and Pillow is already a hard dependency. A format change needs a
migration path for the 50k files already there, and that is the real cost of the
idea — say so if you propose it.

**Embedding dtype.** `card_embedding` is float32 (`data/vision.py:85`), 1,589
B/row, 78.8 MB. float16 halves it, int8 quarters it — at some cost in retrieval
accuracy that must be benchmarked, not assumed, and against a **68-minute**
re-embed (ADR 2026-08-06). Do not propose this without a plan to measure top-1
before and after.

**Unbounded derived tables.** `catalog_price_snapshot` has 223,893 rows and
nothing prunes it. `auctions.db` is documented as never pruned. Retention is a
product decision — surface it, do not implement it unasked.

**Backups of derived data.** `backups/` holds 211 MB: ten full copies of
`inventory.db`, each carrying 7.2 MB of `card_catalog` that is re-downloadable.

**Redundant indexes.** `perf.py storage` flags them automatically. Currently two
on `card_catalog`: `idx_catalog_name` duplicates `idx_card_catalog_name`, and
`idx_card_catalog_set_name` is a prefix of `idx_catalog_set`. Each one is paid
again on every insert. Dropping an index is a forward-only migration like any
other.

**Connection pragmas.** `data/db.py:29-32` sets only `journal_mode` and
`foreign_keys`. For a single-user local app, `synchronous`, `cache_size`,
`mmap_size` and `temp_store` are unset and free to tune — one file, no new
dependency. `synchronous` trades durability against speed, so it is a
recommendation to the user, not an autonomous edit.

---

## Step 4 — The research half, bounded

When the shapes above do not cover a target, research a better mechanism — but
in a fixed order, because this is an **offline, single-user, PyInstaller-frozen
desktop app** and most of what a general search returns cannot ship here.

Ask these three first. All three have precedent in this repo and none of them
adds a dependency:

1. **Can the result be cached?** Session cache, on-disk cache, or a column.
   Precedents: `ui/thumbcache.py`, `services/cardimages.py`'s lazy on-disk cache
   (ADR 2026-07-14), the cached probes in `services/training.py`.
2. **Can it move off the UI thread?** `ui/views/_worker.py`'s `TaskWorker` is the
   standard. Anything that blocks a repaint and is not needed for the first
   frame is a candidate.
3. **Can SQLite do it instead of Python?** An aggregate, a join or an index in
   place of a loop. Precedents: `0013_card_catalog_indexes.sql`,
   `0018_lot_consumption_lot_index.sql`. Confirm with
   `perf.py plans` — and remember an index costs write time and disk.

Only when all three fail do you look outward. Then the proposal must state:

- **the packaging cost** — a new runtime dependency has to survive
  `packaging/tcginventory.spec` (`hiddenimports`), the frozen boot, and the
  125 MB exe budget. `numpy` and `Pillow` are already hard dependencies and are
  free to use.
- **the offline constraint** — no new network calls in a path a user waits on.
- **the layer it lands in** — `domain/` and `services/` are Qt-free and
  `tests/test_headless.py` enforces it.

Check what is already paid for before adding anything. A stdlib answer
(`functools.lru_cache`, `sqlite3` pragmas, `array`, `memoryview`) that is worse
on paper usually wins here once packaging is priced in.

---

## Step 5 — Report before editing

Write the findings down before changing anything. Every line carries a real
`file:line` you have read and a real number you have measured:

```
LATENCY
  <file>:<line> — <what> costs <N> ms of <total> ms in <operation>
                  fix: <one line>            expected: <N> ms -> <M> ms

MEMORY
  <file>:<line> — <what> holds <N> MB resident, isolated in a fresh process

STORAGE
  <path or table> — <N> MB across <N> rows/files
                    fix: <one line>          measured saving: <N> MB

MEASURED AND NOT WORTH IT   (say this out loud — it is half the value)
  <file>:<line> — looks like <shape>, measures <N> ms. No change.

FLAGGED — needs your call, not applied
  <file>:<line> — <why it is a judgement call>
```

---

## Step 6 — Apply

Apply, one edit at a time, re-measuring after each:

- Memoising a derived value in a Qt model or delegate
- Hoisting a repeated call or a loop-invariant query
- Caching a probe whose inputs have not changed
- Moving blocking work behind `TaskWorker`
- Adding or dropping an index, as a new numbered migration
- Batching N single-row queries into one

Flag, never apply on your own:

- Anything touching money, FIFO, or the basis split — `services/allocation.py`,
  `services/transactions.py`, `domain/`
- Any change to stored precision, including the embedding dtype
- An image-format change to the 50k-file cache (it needs a migration plan)
- Retention or pruning policy on any table
- `PRAGMA synchronous` — it trades durability, so it is the user's call
- Anything whose only justification is that it looks inefficient

After each edit, re-read the surrounding lines to confirm the structure held.

---

## Step 7 — Verify

```powershell
.\.venv\Scripts\ruff.exe format .
.\.venv\Scripts\ruff.exe check .
$env:QT_QPA_PLATFORM = "offscreen"; .\.venv\Scripts\python.exe -m pytest
```

Then **re-run the harness** and put the before/after side by side. An
optimization with no after-number is not finished.

Two failures are especially informative: `tests/test_headless.py` means a Qt
import reached `domain/` or `services/`, and `tests/test_app_map.py` means you
moved something `.claude/context/app-map.json` still claims — fix the map in the
same change.

Fix and re-run, up to three attempts. If a failure survives that, revert the edit
that caused it and move it to *Flagged*.

---

## Step 8 — Return

- Latency: before → after per fix, and the new total
- Memory: before → after resident
- Storage: bytes saved, and where
- Measured and not worth it: the list, with numbers
- Flagged: `file:line` each, one line of reasoning
- `ruff check` and `pytest` results, stated plainly, including failures

If nothing measured badly: *"Measured; nothing is worth changing."* That is a
complete and successful sweep.

Do not commit. Per this repo's workflow the user reviews, commits to `main`, and
rebuilds the exe. If the sweep found something significant, offer an ADR entry
for `docs/decisions.md` — the measured baseline is worth recording even when the
verdict was "no change".

**If the baseline in `references/baseline.md` no longer matches what the harness
prints, update that file in the same change.** A stale baseline is how the next
sweep gets confidently misdirected.
