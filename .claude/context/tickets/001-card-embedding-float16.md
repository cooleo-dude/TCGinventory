# 001 — Store card embeddings as float16

```
status:   done
kind:     optimization
source:   /optimization-sweep research pass (Step 7)
issue:    #2
axis:     storage
found:    2026-08-07
saving:   MEASURED: vision.db 104,292,352 -> 53,006,336 bytes (-49.2%); payload
          76.11 -> 38.06 MB. NOT a RAM saving — see the correction below.
risk:     medium — changes a stored binary format; needs a migration + a marker
branch:   ticket/001-card-embedding-float16
```

## Result

Landed 2026-08-09. Conversion of the real corpus took **2.1 s** for 49,733
vectors (49,551 card + 182 region), and the agreement check was **100.00% top-1
and 100.00% top-5** over 482 probes — 182 real photo crops plus 300 noised
catalog renders, all against the full index, compared against answers snapshotted
before the conversion.

Three things in this ticket were wrong, and are worth carrying forward:

1. **The impact table missed a reader.** `services/identity_eval.py:110-129`
   (`load_pool`) decoded `card_embedding` with a hard-coded float32 — and it is
   also the exact site step 6 describes as "where the pool is materialised".
2. **The RAM claim contradicts the plan.** Step 6 says keep the working matrix
   float32, so a loaded index is still 76.1 MB in RAM. This is a disk win only.
   `perf.py memory` will not move.
3. **VACUUM alone reclaims nothing here.** `open_vision` sets WAL, so the rebuilt
   pages sit in the `-wal` file and the main file is not truncated until a
   checkpoint. The conversion ends with `PRAGMA wal_checkpoint(TRUNCATE)`.

The file shrank further than the predicted ~66 MB because the VACUUM also
reclaimed pre-existing free pages, not only the 38 MB of halved payload.

**Also note the failure mode that made the marker mandatory:** a reader left on
float32 returns a half-length vector, and both scoring paths *skip* mismatched
vectors rather than raising. A missed reader would have produced an identifier
that silently suggested nothing. `vectors.unpack` now raises on a `dim`
disagreement, and a test asserts no module outside `services/vectors.py` decodes
an embedding.

## The finding

`card_embedding.vector` is float32. At 384 dimensions and 49,551 rows that is
**76.1 MB of payload in a 104.3 MB database** — the single largest thing in
`vision.db` by a wide margin, and second only to the PNG cache across the whole
app.

The vectors are L2-normalised DINOv2-small outputs used only for cosine ranking.
They do not need float32's dynamic range, and measurement says they do not use
it.

## The measurement

All 49,551 vectors loaded, 300 random queries run against the **full** index,
cosine top-k in float32 taken as ground truth:

| dtype | size | vs float32 top-1 | vs float32 top-5 overlap |
|---|---:|---:|---:|
| float32 (today) | 76.1 MB | — | — |
| **float16** | **38.1 MB (−50%)** | **100.00% identical** | **99.73%** |
| int8 (scale 127) | 19.0 MB (−75%) | 97.00% | 93.80% |

**float16 changed nothing.** Not one of 300 queries picked a different best
match.

**int8 is rejected.** 3% of queries change their top answer. Identification is
already at ~45% top-1 (ADR 2026-08-05); spending three points of agreement to
save a further 19 MB is not a trade worth making, and it would need a full
benchmark run to even quantify. Do not revisit without one.

## Area of impact

Everything reads these blobs through `np.frombuffer(..., dtype=np.float32)`, so
the dtype is currently implicit in five places at once.

| File | Line | Role |
|---|---|---|
| `data/vision.py` | 81-88 | `card_embedding` schema; comment says "float32 bytes, L2-normalised" |
| `data/vision.py` | 89-96 | `region_embedding` — same dtype, 182 rows, change together for consistency |
| `data/vision.py` | 382, 389, 442 | `get_embedding`, `put_embedding`, `region_vectors` — the blob accessors |
| `services/catalog_index.py` | 289 | **writer** — `np.asarray(vector, dtype=np.float32).tobytes()` |
| `services/recognizer.py` | 326, 336 | **readers** — `np.frombuffer(blob, dtype=np.float32)` |
| `services/identity_projection.py` | 299 | reader, when fitting the photo-matching matrix |
| `services/exemplars.py` | 67 | writes region exemplars |
| `services/catalog_index.py` | 199 | `candidate_pool` — reads ids only, unaffected |

`vision.db`'s schema is **created in-module, not by `data/migrator.py`** (see
`.claude/context/app-map.json` → `databases.vision.db`). There is no numbered SQL
migration for this; the upgrade has to live in `data/vision.py`'s schema setup.

## Approach

**Chosen: add a `dtype` column, cast the existing blobs once, in place.**

```sql
ALTER TABLE card_embedding ADD COLUMN dtype TEXT NOT NULL DEFAULT 'float32';
```

Readers honour the column. New writes are `float16`. A one-time pass casts the
49,551 existing rows and flips their `dtype`. No re-embedding, no model load —
it is a pure numpy cast of bytes already on disk, so it runs in seconds.

The column is what makes this safe: a half-finished migration leaves a mixed
table that still reads correctly, and a rollback is a cast in the other
direction.

### Rejected alternatives

- **Bump `model_version` to force a re-embed.** Correct, and costs a **~68 minute**
  rebuild of all 49,551 embeddings (ADR 2026-08-06). Absurd for a lossless-in-
  practice dtype change that needs no new inference.
- **Cast in place with no marker.** Nothing then distinguishes an old float32 row
  from a new float16 one, and every reader would guess by blob length — which
  works right up until a model with a different `dim` lands.
- **int8.** See above. Rejected on accuracy.
- **A numbered SQL migration.** Wrong database. `data/migrator.py` owns
  `inventory.db` only.

## Plan

1. `data/vision.py` — add the `dtype` column to `card_embedding` and
   `region_embedding` in the schema setup, defaulting to `'float32'` so existing
   files keep reading correctly. Update the two schema comments.
2. `data/vision.py` — give the blob accessors a dtype-aware helper, e.g.
   `vector_of(row) -> np.ndarray`, so no caller does `frombuffer` with a
   hard-coded dtype ever again. This is the change that stops it regressing.
3. Point `services/recognizer.py:326,336`, `services/identity_projection.py:299`
   and `services/exemplars.py` at that helper.
4. `services/catalog_index.py:289` — write `astype(np.float16).tobytes()` and set
   `dtype='float16'`.
5. One-time upgrade, run from the existing `exemplars.reindex_all` /
   catalog-index Operations path or a small dedicated runner: read each
   `dtype='float32'` row, cast, write back, set `dtype='float16'`. Do it in one
   transaction with `executemany`.
6. **Dequantise once at index load, not per query.** A naive per-query
   `astype(np.float32)` measured **111 ms against 74 ms** — slower than doing
   nothing. Cast where the pool is materialised, and keep the in-memory matrix
   float32.
7. Tests: a round-trip test per dtype, and one that a mixed table (some rows
   float32, some float16) reads correctly.

## Verification

- `perf.py storage` — `vision.db` should fall from 104.3 MB to roughly 66 MB, and
  `card_embedding` payload from 78.76 MB to ~39 MB.
- Re-run the agreement check from the sweep: 300 random queries against the full
  index, top-1 against the pre-migration answers. **It must be 100%.** Anything
  less means the cast is happening in the wrong place.
- `perf.py latency --views` — Recognition build must not regress.
- Full suite offscreen, `ruff format`, `ruff check`.

## Notes

- Storage win, **not** a speed win. Do not sell it as one.
- RAM matters as much as disk here: a loaded index is 76 MB today and would be
  38 MB, and `RecognitionView` prewarm already holds 112 MB at startup
  (declined separately — see `declined.md`).
- `region_embedding` is only 182 rows / 0.29 MB. It is in scope for consistency
  of the accessor, not for the bytes.
