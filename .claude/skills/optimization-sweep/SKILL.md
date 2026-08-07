---
name: optimization-sweep
description: Find and fix real performance costs in TCGinventory — wall-clock latency, resident memory, and bytes on disk. Use when asked to make the app faster, fix a slow window or startup, reduce memory or RAM use, shrink the database or image cache, hunt N+1 queries or missing indexes, or profile something. Also use when the user types /optimization-sweep.
argument-hint: <area to scope the sweep, optional> — e.g. the Models window, startup, vision.db, the image cache
allowed-tools: [Read, Grep, Glob, Bash, PowerShell, Edit, Write, WebSearch, WebFetch]
---

# Optimization sweep

Scope: **$ARGUMENTS** — if that is empty, sweep the whole platform, in the order
the harness ranks things.

**Two passes, and both are mandatory.** Steps 1–6 are the *grounded* pass: fix
what measures badly, ranked by the harness. **Step 7 is the research pass, it
runs at the end of every sweep, and it runs even when nothing measured badly** —
it asks whether there is a more effective way to do what the code already does,
across *every* location, not just the slow ones. A sweep that stops after Step 6
is half a sweep.

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
| `references/declined.md` (next to this file) | Every variant the user has turned down, and every one measurement rejected. Read it **before proposing anything**, in either pass. |
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
`storage`, `plans`, `profile <View>`, `capabilities`, `all`.

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

**Declined already — do not propose these again.** The full list, with dates and
reopening conditions, is **`references/declined.md`**; read it before proposing
anything, in this pass or in Step 7. The standing ones:

- **Lazy view construction.** Yes, `main_window.py:132-158` builds all eleven
  views eagerly and that is ~1.2 s of startup. Declined by the user. Do not
  raise it.
- **Recognition's 112 MB prewarm** and the **2.2 GB PNG cache** (~1.2 GB
  recoverable as WebP). Both measured, both declined by the user.
- **A performance-regression test.** Declined. The suite runs `-n auto`; a
  timing assertion in it would be flaky. Results go in the ADR.
- Vectorising, batching or reordering anything in `services/allocation.py` or
  `services/transactions.py`. They cost ~0.3 ms and they are the money path.

---

## Step 3 — The shapes that are actually real here

Each of these was found by measurement in this repo, and each names where it was
found. Look for more of the same shape; do not assume the listed instance is the
only one.

### 3a. Latency

**A Qt model that is slow on reset — count the roles before caching anything.**
`ui/models/inventory_model.py` was the largest live latency item, and every
guess about *why* was wrong until the roles were counted. `data()` fired 31,598
times for 1,105 rows, which looks like per-cell display derivation over 20
columns. It was not: **all 31,598 calls were `Qt.UserRole`** — the sort role —
over only **1,105 distinct cells**, because a `QSortFilterProxyModel` re-reads
both operands of every comparison and asked one cell **84 times**.
`Qt.DisplayRole` was never called during a reset, so a display cache would have
bought exactly nothing.

The real costs were the role-comparison chain — `role == Qt.SomeRole` is a rich
comparison against a wrapped C++ enum, not an int compare — and the 96.5% repeat
rate. Fixed with module-level `int` role constants, an early return for the sort
role, and a `(row, column) -> key` cache cleared in `set_rows()`:
**refresh 340 ms → 108–139 ms, build 306 ms → 128 ms.**

So: instrument `data()` with a `Counter` keyed by `(row, column, role)` **before**
designing a cache. Subclass to do it — PySide6 binds virtual overrides when the
class is created, so a monkeypatch applied afterwards is never called and will
silently report zero.

`history_model` measured the same and got the same fix — 5,132 calls for 262
rows, **100% sort role**, 94.9% repeats, one cell asked 124 times;
`set_rows()` 40.0 ms → 15.5 ms, History build 128 ms → 87 ms, refresh
151 ms → 70 ms. **`recognition_history_model` is unmeasured**: its model holds
zero rows on this dataset, so there was nothing to profile. Do not "fix" it by
analogy — measure it on a dataset that populates it, or leave it.

**Verify a sort cache against a Python sort**, every column, both directions.
And when the check fails, suspect the check first: ties keep source order, so
comparing row *identities* reports a false failure. Compare the sequence of
sort *keys*. That caught nothing real twice and would have looked like a bug
both times.

**cProfile lies about this path.** It counts calls honestly — 31,598 — but its
per-call overhead swamps a function this small, and it inflated `image_for` from
a real 0.45 ms/item to an apparent 83 ms/call. When a profile points at
something called tens of thousands of times, **re-measure it with
`perf_counter` before believing the magnitude.** Use cProfile to find *where*,
never to size *how much*.

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

**Deferring a heavy import is not automatically a win — measure the total.**
`import main_window` costs 383 ms, 159 ms of it `services.inference` pulling
onnxruntime and numpy for one eager `write_back`. Deferring it does drop the
import phase to 230 ms with the whole vision chain gone, and makes total startup
**worse**: `RecognitionView(prewarm=True)` then loads onnxruntime itself on a
background thread, contending for the import lock, and Recognition's build goes
35 ms → 260 ms for a net **+96 ms**. Measured over three runs each way and
reverted. Use `python -X importtime` to see the chain, but judge the change on
`perf.py latency --views` **TOTAL BUILD**, which already includes import cost —
adding the two together double-counts.

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

## Step 4 — Report before editing

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

## Step 5 — Apply

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

## Step 6 — Verify

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

## Step 7 — The research pass (always, and last)

Steps 1–6 fix what measured badly. **This step asks a different question, and it
runs at the end of every sweep whether or not anything measured badly:**

> Is there a more effective way to do what this code already does?

Not "what is slow" — *what could be done better*. A function that costs 12 ms
doing something a stored column would answer in 0.1 ms is a finding here even
though it will never top a profile. The grounded pass is driven by the harness's
ranking; this pass is driven by **coverage**. Work the locations, all of them,
and record a verdict for each — including "nothing better available", which is a
result and stops the next sweep re-deriving it.

The discipline from Step 1 still binds: **a mechanism is not reported until a
spike has run it against real staged data.** A mechanism you have not run is a
hypothesis, and this skill does not report hypotheses as findings.

### 7a. Read the declined register first — this is not optional

`references/declined.md` records every variant and approach the user has turned
down, and every one that measurement rejected. **Read it before you enumerate
anything.** Re-proposing something already declined wastes the user's attention
and erodes the value of the whole pass.

Each entry says what would have to change for it to be worth raising again. That
condition is the only thing that reopens it — and if you believe one has been
met, say which and why, do not just re-propose.

Then the three greps, for anything the register does not cover:

```
Grep: <topic>  in docs/decisions.md    # settled? read the ADR before proposing
Grep: <topic>  in specs/05-roadmap.md  # already on the backlog? say so
Grep: <topic>  in src/tcginventory/    # already half-built somewhere?
```

### 7b. Enumerate the locations — coverage, not ranking

```powershell
.\.venv\Scripts\python.exe .claude\skills\optimization-sweep\scripts\perf.py candidates
```

That prints every site in `src/tcginventory/` matching a mechanism-bearing
pattern — stored dtypes, image formats, filesystem walks, subprocess calls,
`fetchall()` on big tables, exact-arithmetic stdlib, Qt models, network calls,
retention-free tables. It is an inventory, **not** a list of defects; most
entries are correct as they stand.

Walk it and give **every** location one of four verdicts:

| verdict | meaning |
|---|---|
| `better` | a mechanism exists, spiked, with numbers — write it up (7h) |
| `equal` | an alternative exists and measured no better — record it, it is a permanent result |
| `none` | nothing better available given the constraints |
| `declined` | already in the register — name the entry, do not re-litigate |

A pass that reports only the `better` ones is not finished. The `none` and
`equal` verdicts are what make the *next* sweep cheap.

If `candidates` misses a category you find by hand, add the pattern to
`PATTERNS` in `perf.py` in the same change.

### 7c. Know what is already paid for

```powershell
.\.venv\Scripts\python.exe .claude\skills\optimization-sweep\scripts\perf.py capabilities
```

Most "we should use library X" ideas turn out to be something numpy, Pillow, Qt
or SQLite already ships. As probed on 2026-08-07 — re-run, do not trust this
list:

- **SQLite 3.49.1** with FTS5, R*Tree, JSON functions, generated columns,
  `WITHOUT ROWID`, expression indexes and incremental BLOB I/O
  (`Connection.blobopen`). **No `dbstat` vtab and no STAT4** — so per-table page
  accounting is unavailable (the harness estimates payload with `LENGTH()`
  instead) and `ANALYZE` gives the planner counts without histograms.
- **numpy 2.5.1, Pillow 12.3.0, PySide6 6.11.1, httpx 0.28.1,
  onnxruntime 1.27.0** — all hard dependencies already, all free to use.
- **Image formats are a two-sided constraint.** Pillow writes WebP, JPEG *and
  AVIF*; Qt reads PNG, JPEG and WebP but **cannot read AVIF or JPEG-XL**. A
  format is only usable if both sides handle it — AVIF would give smaller files
  the UI cannot display. Exactly the trap a benchmark comparing compression
  ratios alone walks into.

### 7d. The three in-repo questions

Ask these of each location before looking outward. Each has precedent here and
none adds a dependency, so each is cheap to try and cheap to revert.

**1. Can the result be cached?** — the most common answer.

| Kind | Use when | Precedent |
|---|---|---|
| `functools.lru_cache` on a pure function | inputs repeat within a run | — |
| module-level memo with an explicit invalidator | a probe whose inputs rarely change | `services/training.py`'s cached probes |
| a memo persisted to disk | the answer survives a restart and the walk to rebuild it does not | `trainingenv.env_size` |
| `QPixmapCache` | decoded images | `ui/thumbcache.py` (ADR 2026-08-04) |
| on-disk cache keyed by id | expensive to fetch, cheap to store | `services/cardimages.py` (ADR 2026-07-14) |
| a stored column | derived from data that changes on write | `catalog_price_latest` |

The hard part is never the cache, it is the invalidation. Say in the proposal
what makes the entry stale and who clears it. A cache with no answer to that is
a bug with better latency. Then measure what it holds — a cache whose footprint
you have not measured is half a finding.

**2. Can it move off the UI thread?** `ui/views/_worker.py`'s `TaskWorker(db_path,
task)` runs `task(conn) -> sentence` with its own connection — the repo standard
for "call a service, report a sentence". Anything blocking a repaint that is not
needed for the first frame is a candidate. Two rules: an exception crossing a
`QThread` boundary takes the app down, and a SQLite connection belongs to the
thread that made it — hence the separate connection.

**3. Can SQLite do it instead of Python?** Confirm any answer with
`perf.py plans` before and after.

| Mechanism | Use when |
|---|---|
| an index | a `SCAN` on a table over ~20k rows |
| a **covering** index | the query reads only indexed columns — no row lookup at all |
| a **partial** index (`WHERE …`) | you filter on a sparse condition; smaller and cheaper to maintain. Precedent: `idx_lot_sku` |
| an **expression** index | you filter on `lower(x)` or a computed form |
| a composite index | you filter on A and sort by B — column order matters |
| `GROUP BY` / aggregate | a Python loop is summing rows |
| a window function | a per-group "latest" or running total done in Python |
| one query with `IN (…)` or a join | N single-row lookups in a loop |
| `executemany` in one transaction | N inserts, each currently its own commit |
| FTS5 | a `LIKE '%…%'` scan over a large text column |
| `WITHOUT ROWID` | a narrow table keyed by a text primary key |
| `blobopen` | reading part of a large BLOB without materialising it |
| `ANALYZE` / `PRAGMA optimize` | the planner is choosing badly |

Every index costs write time and disk. `perf.py storage` flags redundant ones,
and adding one is a forward-only migration like any other.

**Never propose dropping an index on column names alone.** `card_catalog` looks
like it carries two duplicate pairs and carries none: `idx_catalog_name(name)`
is BINARY while `idx_card_catalog_name(name COLLATE NOCASE)` is NOCASE, and
SQLite picks between them by the collation of the *comparison* — `name = ?` uses
one, `name = ? COLLATE NOCASE` the other. Both are live. Collation and `DESC`
are part of the key and `PRAGMA index_info` reports neither; read
`PRAGMA index_xinfo` and confirm with a plan before touching anything.

### 7e. Mechanism repertoire for the other two axes

**Memory.** Do not materialise what you can stream — a generator or a
`fetchmany` loop instead of `fetchall` on the 49k/224k-row tables. `__slots__` on
a dataclass instantiated per row. `array`/`memoryview` instead of a list of
Python floats. numpy views instead of copies (`astype` always copies; in-place
ops do not). Load a model lazily on first real use rather than prewarming it.
For a Qt model over a large table, `canFetchMore`/`fetchMore` populates
incrementally instead of holding every row.

**Storage.** Narrow the dtype (float32 → float16 → int8) when accuracy is
*measured* to survive. Change container format when both writer and reader
support it. Content-address duplicates rather than storing them twice. Move a
large re-derivable BLOB out of the ledger so it stops being backed up. Add a
retention policy to an append-only table. Drop a redundant index. `VACUUM` only
helps when `PRAGMA freelist_count` is non-zero — today it is zero in all four
databases.

**Latency in Qt.** Answer the sort role before anything else and cache it per
`(row, column)`. `dataChanged` over a column beats `beginResetModel` (which
loses selection and scroll — see `set_trends` in `inventory_model.py`). Compare
plain ints, not wrapped C++ enums. `setUniformRowHeights` on a tree with fixed
rows. Keep delegate `paint()` free of allocation. Batch signal emissions rather
than emitting per row.

**Pure Python.** `statistics.mean`/`pstdev`/`median` use exact `Fraction`
arithmetic — correct, and 10× slower than longhand on short lists of ints.
Hoist loop-invariant work out of the loop. Sort once and walk with a pointer
instead of re-scanning. Precompute a cumulative series and `bisect` it instead
of re-summing a prefix.

### 7f. Going outward

Only when 7d and 7e are exhausted for that location. Then a candidate must pass
**all** of these, and the write-up must say so explicitly for each:

1. **Packaging.** A new runtime dependency has to survive
   `packaging/tcginventory.spec` (`hiddenimports`), boot inside the frozen
   onedir build, and fit the exe budget (~300 MB today, mostly the bundled
   ONNX model). State the added size.
2. **Offline.** No new network call on a path a user waits on. The app has no
   external price feeds in V1 by design.
3. **Layer.** `domain/` and `services/` are Qt-free; `tests/test_headless.py`
   enforces it. A library that drags in Qt cannot live in a service.
4. **Platform.** Windows is the primary target and macOS is a real dev box. A
   wheel that is Linux-only, or needs a compiler, is not a candidate.
5. **Maintenance.** One person maintains this app offline. A dependency that
   needs pinning, patching or a build step costs more than it saves.

Search for the *mechanism*, not the endorsement — "how does X work / what does
it cost", not "best library for X". Prefer primary sources: the SQLite
documentation, Qt class documentation, a library's own changelog. Treat
benchmark blog posts as leads, never as evidence: none of them ran against
53,289 catalog rows on this machine, and you can.

A stdlib or already-present answer that looks worse on paper usually wins here
once packaging is priced in.

### 7g. The spike — the part that makes it research

Before proposing any mechanism, build the smallest thing that measures it,
against **real staged data**, and report the number. Write the spike in the
scratchpad, never in `src/`.

The harness already knows how to stage a copy safely; reuse it rather than
opening the live database:

```python
# scratchpad/spike_<idea>.py
import sys, tempfile, time
from pathlib import Path
sys.path.insert(0, r".claude\skills\optimization-sweep\scripts")
import perf  # _stage_db copies the live DB and repoints TCGINVENTORY_DB

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    staged = perf._stage_db(Path(tmp))
    conn = perf._conn(staged)          # NOT db.connect() — that defaults to :memory:
    # measure the current way, then the proposed way, on the same connection
```

For a storage idea, sample rather than convert everything — 25 files was enough
to size the PNG→WebP question to within a few percent, and it cost seconds
instead of an hour.

A worked example, run against the staged 53,289-row catalog: replacing **300
single-row lookups** with one `IN (…)` query measured **1.35 ms → 0.35 ms**. A
3.8× speedup — and one millisecond. That is the whole discipline in one result:
the mechanism was genuinely better, and shipping it would have been a waste.

Rules for a spike:

- Compare against the **current** implementation on the same data in the same
  run. A number with nothing beside it means nothing.
- Sample honestly and say the sample size.
- If the mechanism trades accuracy for size or speed — quantisation, a lossy
  format, a cache with a staleness window — **measure the accuracy too**, or
  report that you could not and mark the finding blocked.
- Delete the spike when done. It is evidence, not code.

### 7h. Report, and record every decline

A research finding is reported as a mechanism proposal, not a suggestion:

```
MECHANISM  <name>
  location    <file:line>
  today       <what the code does now, and what it costs>
  mechanism   <the alternative, in one sentence>
  spike       <what you ran, on what data, sample size>
  result      <before> -> <after>, measured
  cost        <accuracy / packaging / migration / write-time cost, or "none">
  verdict     better | equal | none
  blocked by  <what must be decided or measured before it can ship, or "nothing">
```

Then a one-line coverage table for every other location `candidates` printed,
with its verdict. Short is fine — `services/foo.py:120 — none (already a single
aggregate)`. That table is the deliverable as much as the proposals are.

**When the user declines a proposal, append it to `references/declined.md`
before doing anything else.** One entry, with: what was proposed, the date, who
declined it (user decision vs. measurement), the reason if given, and the
condition that would make it worth raising again. A decline that is not written
down will be re-proposed by the next sweep, which is the single most annoying
thing this skill can do.

The same applies to a mechanism that measured flat or worse — record it in the
register and in `docs/decisions.md`. A dead end proven with numbers is a
permanent asset.

---

## Step 8 — Return

**From the grounded pass (Steps 1–6):**

- Latency: before → after per fix, and the new total
- Memory: before → after resident
- Storage: bytes saved, and where
- Measured and not worth it: the list, with numbers
- Flagged: `file:line` each, one line of reasoning
- `ruff check` and `pytest` results, stated plainly, including failures

**From the research pass (Step 7) — always present, even when it found nothing:**

- Each `MECHANISM` block, in the 7h format
- The coverage table: every location `candidates` printed, with its verdict
- Anything newly written to `references/declined.md`

If nothing measured badly and research found nothing better:
*"Measured; nothing is worth changing, and nothing better is available."* That
is a complete and successful sweep — but it is only credible with the coverage
table behind it. **A sweep that reports no research section is unfinished, not
clean.**

Do not commit. Per this repo's workflow the user reviews, commits to `main`, and
rebuilds the exe. If the sweep found something significant, offer an ADR entry
for `docs/decisions.md` — the measured baseline is worth recording even when the
verdict was "no change".

**If the baseline in `references/baseline.md` no longer matches what the harness
prints, update that file in the same change.** A stale baseline is how the next
sweep gets confidently misdirected.

**And if the user declines anything you proposed, write it into
`references/declined.md` before you close out** — what it was, the date, that it
was a user decision, and what would make it worth raising again. That file is
the only thing stopping the next sweep from proposing it all over again.
