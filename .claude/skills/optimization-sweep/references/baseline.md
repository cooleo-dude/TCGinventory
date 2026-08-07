# Measured baseline — 2026-08-07

Everything here came from `scripts/perf.py` against the real app data on the
Windows dev box (Python 3.12.10, PySide6 offscreen). Re-measure before trusting
any of it; the numbers move as the data grows. **The shape of the table matters
more than the digits** — it is what tells a real cost from an imagined one.

Regenerate with:

```powershell
.\.venv\Scripts\python.exe .claude\skills\optimization-sweep\scripts\perf.py all
```

## Scale — what is big and what is not

Four SQLite files. Only `inventory.db` is the ledger; the other three are
derived or ephemeral.

| Table | Rows | Database |
|---|---:|---|
| `catalog_price_snapshot` | 223,893 | pricehistory.db |
| `card_catalog` | 53,289 | inventory.db |
| `card_embedding` | 49,551 | vision.db |
| `catalog_price_latest` | 49,493 | pricehistory.db |
| `price_snapshot` | 23,583 | inventory.db |
| `transaction_line` | 1,320 | inventory.db |
| `lot` | 1,105 | inventory.db |
| `item` | 903 | inventory.db |
| `transaction` | 262 | inventory.db |
| `lot_consumption` | 213 | inventory.db |

Plus **50,546 cached card PNGs** on disk.

The ledger is three orders of magnitude smaller than the catalog. That single
fact decides most of what is worth doing.

## Latency

### Views — 2.0–2.4 s of construction on every launch

| View | Build | Refresh |
|---|---:|---:|
| Models | **1,095–1,112 ms** | 75 ms |
| Inventory | 322–361 ms | **371–374 ms** |
| Transaction | 13–219 ms | 0.1 ms |
| Dashboard | 137–177 ms | **192–202 ms** |
| History | 124–147 ms | 150 ms |
| Market | 1.5–146 ms | 118 ms |
| Operations | 111–121 ms | 76–80 ms |
| eBay Auctions | 36–42 ms | 0.1 ms |
| Calendar | 32–38 ms | 26 ms |
| Recognition | 33 ms | 0.0 ms |
| Price Lookup | 4.5–13.5 ms | — |
| **Total build** | **1,921–2,399 ms** | |

`main_window.py:122` already names this: *"Building the ten views is the two
seconds of startup."*

Ranges are real run-to-run variance, not rounding. Recognition's prewarm thread
competes with whatever is constructed after it, which is why Transaction reads
13 ms in one run and 219 ms in another. **Trust the total and the outliers.**

### Services — all fast, none worth touching

| Call | Best of 5 |
|---|---:|
| `ReportingService.collection_value_series()` | 43.9 ms |
| `MarketSignalService.overview()` / `.signals()` | 39.1 ms |
| `InventoryService.all_positions()` | 25.4 ms |
| `InventoryService.inventory_rows()` | 12.5 ms |
| `InventoryService.sellable_items()` | 6.5 ms |
| `ReportingService.price_movers()` / `.top_gainers()` | 3.5 ms |
| everything else in `ReportingService` | 0.2–0.8 ms |

`all_positions()` is a textbook N+1 — two queries per item across 903 items —
and it costs 25 ms and is called from no view. It is the single most tempting
non-finding in the repo.

### Profiles worth knowing

**`ModelsView` build (0.93 s under cProfile):**

- `catalog_index.index_status` called **twice**, 0.43 s, each call constructing
  the shared ONNX embedder (0.13 s each)
- `trainingenv._walk_size` 0.28 s across **6,139 `nt.scandir` calls**
- first-touch imports of `services.training` (0.20 s) and `services.detector`
  (0.18 s), both on the UI thread
- `ModelsView.refresh()` spends 44 ms in `subprocess.run` — `nvidia-smi`, per
  refresh

**`InventoryView` refresh — measured without cProfile, which distorts this path
badly.** `refresh()` is 340–365 ms, and it decomposes as:

| Part | Cost |
|---|---:|
| `set_rows()` | **310–336 ms** |
| `inventory_rows()` (the service) | 17 ms |
| `_update_summary()` | 6 ms |
| `_fill_columns()`, `_on_selection()` | <1 ms each |

Inside `set_rows()` — and the answer is **not** what the shape suggests. Every
one of the 31,598 `data()` calls is `Qt.UserRole`, the **sort** role, over only
**1,105 distinct cells**: the proxy re-reads both operands of every comparison,
so one cell is asked up to **84 times**. `Qt.DisplayRole` is never called during
a reset. A display cache would have bought nothing.

| | before | after |
|---|---:|---:|
| `set_rows()` as shipped | 334.0 ms | **126.1 ms** |
| sort key replaced by a constant | 65.1 ms | 78.1 ms |
| `data()` returns `None` for everything | 25.9 ms | 40.8 ms |
| `InventoryView.refresh()` | 340 ms | **108–139 ms** |
| `InventoryView` build | 306 ms | **128 ms** |

Column 0's key is `lambda r: r.sku`, so the 269 ms was never derivation — it was
the role-comparison chain (`role == Qt.SomeRole` is a rich comparison against a
wrapped C++ enum, not an int compare) multiplied by the 96.5% repeat rate. The
fix was module-level `int` role constants, an early return for the sort role,
and a `(row, column) -> key` cache cleared in `set_rows()`.

**The lesson worth keeping: count the roles before caching anything.** Both of
this file's earlier claims here were wrong — that the cost was `DisplayRole`
derivation, and that a dict-lookup `data()` measured a 36.8 ms "ceiling". The
latter was measuring sort short-circuiting on `None`, not caching.

`history_model` measured the same shape and got the same fix: 5,132 calls for
262 rows, 100% sort role, 94.9% repeats, worst cell asked 124 times.
`set_rows()` 40.0 ms → 15.5 ms; History build 128 ms → 87 ms; refresh 151 ms →
70 ms.

`recognition_history_model` is **unmeasured** — its model holds zero rows on
this dataset. It has not been changed.

### Where startup stands

| | 2026-08-07 first sweep | after four fixes |
|---|---:|---:|
| total view build | 2,364 ms | **1,205 ms** |
| Models build | 1,155 ms | ~200 ms |
| Inventory build / refresh | 306 / 401 ms | 128 / 120 ms |
| History build / refresh | 128 / 151 ms | 87 / 70 ms |
| Operations refresh | 81 ms | 51 ms |

Also in refresh: `repositories.image_for()` and `trends.card_id_for` appear
expensive under cProfile but measure **0.45 ms per item** warm across all three
`gather()` queries (40 real items sampled). Fresh connection vs reused is
16.5 ms vs 14.3 ms for 60 items — not the bottleneck.

## Memory — 257 MB resident, 112 MB of it optional

| Stage | RSS |
|---|---:|
| interpreter | 20 MB |
| + QApplication | 38 MB |
| + view modules imported | 94 MB |
| + Dashboard | +21 MB |
| + Inventory | +16 MB |
| + **Recognition (`prewarm=True`)** | **+112 MB** |
| + the other eight views | +11 MB combined |
| **all views built** | **257 MB** |

Isolated in a fresh process, to be sure the async allocation was billed
correctly:

| `RecognitionView(conn, prewarm=…)` | build | RSS |
|---|---:|---|
| `prewarm=True` | 198 ms | 96 → **198 MB** (lands ~1 s later) |
| `prewarm=False` | 182 ms | 96 → **95 MB** |

The DINOv2 ONNX session costs ~103 MB and is paid at startup by every user
whether or not they open Recognition. Build time barely moves, so **the latency
harness cannot see this** — which is the argument for measuring memory
separately.

## Storage — 8.7 GB of app data

| Location | Size | Files |
|---|---:|---:|
| `training-env/…/torch/lib` | 4,570 MB | 59 |
| **`card_images/`** | **2,212 MB** | **50,546** |
| `training-env/…/onnxruntime/capi` | 301 MB | 13 |
| databases at the root | 249 MB | 14 |
| `backups/` | 211 MB | 19 |
| `training-env/…/_polars_runtime_32` | 185 MB | 3 |
| `training-env/…/cv2` | 118 MB | 12 |
| `vision/images` | 76 MB | 44 |
| **Total** | **8,688 MB** | |

### The databases

| File | Size | Dominant table | Payload |
|---|---:|---|---|
| vision.db | 104.3 MB | `card_embedding` | 78.8 MB / 49,551 rows = **1,589 B/row** |
| inventory.db | 21.1 MB | `card_catalog` | 7.2 MB / 53,289 rows = 136 B/row |
| pricehistory.db | 19.1 MB | `catalog_price_snapshot` | 8.0 MB / 223,893 rows = 36 B/row |
| auctions.db | 0.1 MB | — | — |

**Freelist is 0 in all four.** VACUUM reclaims nothing today — do not propose it
as a saving without re-checking `PRAGMA freelist_count`.

Measured facts that bound the obvious ideas:

- `card_embedding` stores **float32** (`data/vision.py:85`, 384 dims × 4 B).
  float16 halves it; int8 quarters it. Both are re-derivable but not cheaply —
  ADR 2026-08-06 records a ~68-minute rebuild of the 49,551 embeddings.
- `card_images/` is **PNG only** (`services/cardimages.py:33`), 200×279 RGB,
  48.8 KB average. On a 25-file sample: **WebP q85 → 19.7 KB (60% smaller,
  ~1,021 MB projected)**, JPEG q88 → 26.5 KB (46% smaller, ~1,369 MB).
- `backups/` keeps 10 full copies of `inventory.db`, and 7.2 MB of each copy is
  `card_catalog` — re-downloadable catalog data, backed up ten times over.
- `pricehistory.db` has 223,893 snapshots and nothing prunes them.
  `auctions.db` is documented in the app map as never pruned.

### Redundant indexes — none. (Corrected 2026-08-07.)

`perf.py storage` reports none, and the two it first reported on `card_catalog`
were **false positives worth remembering**:

- `idx_catalog_name(name)` is BINARY; `idx_card_catalog_name(name COLLATE
  NOCASE)` is NOCASE.
- `idx_card_catalog_set_name(set_name COLLATE NOCASE)` is NOCASE;
  `idx_catalog_set(set_name, number)` is BINARY.

SQLite chooses between them by the collation of the comparison, and query plans
confirm all four are live: `name = ?` → `idx_catalog_name`; `name = ? COLLATE
NOCASE` → `idx_card_catalog_name`; `set_name = ? COLLATE NOCASE` →
`idx_card_catalog_set_name`; `set_name = ? AND number = ?` → `idx_catalog_set`.
Dropping any one sends a real query to a full scan of 53,289 rows.

The detector used `PRAGMA index_info`, which omits collation and direction. It
now uses `PRAGMA index_xinfo`, which carries both. **Never propose dropping an
index on column names alone — check the collation and run the plan.**

### Connection pragmas

`data/db.py:29-32` sets `journal_mode = WAL` and `foreign_keys = ON`, and
nothing else. No `synchronous`, `cache_size`, `mmap_size` or `temp_store`.

## Capabilities — what is available without a new dependency

From `perf.py capabilities`. Re-run it; these move with the venv.

**SQLite 3.49.1** (Python 3.12.10 binding):

| Feature | |
|---|---|
| FTS5 full-text | yes |
| R*Tree | yes |
| JSON functions | yes |
| generated columns | yes |
| `WITHOUT ROWID` | yes |
| expression indexes | yes |
| incremental BLOB I/O (`blobopen`) | yes |
| `dbstat` virtual table | **no** — per-table page accounting unavailable, so the harness estimates payload with `LENGTH()` |
| STAT4 | **no** — `ANALYZE` gives counts, not histograms |

**Already hard dependencies, free to use:** numpy 2.5.1, Pillow 12.3.0,
PySide6 6.11.1, httpx 0.28.1, onnxruntime 1.27.0.

**Image formats are two-sided.** Pillow writes WebP, JPEG and AVIF. Qt reads PNG,
JPEG and WebP but **cannot read AVIF or JPEG-XL**. Only a format both sides
handle is usable — AVIF would produce smaller files the UI cannot display.

## Worked spike

Against the staged 53,289-row catalog: 300 single-row `WHERE card_id = ?`
lookups measured **1.35 ms**; the same work as one `IN (…)` query measured
**0.35 ms**. 3.8× faster, one millisecond saved. A correct optimization that is
not worth shipping — which is the result the sweep exists to be able to state.

## Query plans

Every hot `card_catalog` query is index-backed. The two `SCAN`s are both
defensible:

| Query | Plan |
|---|---|
| `image_for` set / name / number gathers | `SEARCH … USING INDEX` |
| catalog search by set+number | `SEARCH … USING INDEX idx_catalog_set` |
| `image_for` LIKE fallback | `SCAN card_catalog` — deliberate last resort, `LIMIT 50` |
| latest price per item | `SCAN price_snapshot USING COVERING INDEX` — a covering scan of a `GROUP BY`, which is the right plan |
