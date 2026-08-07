# Declined register

Every optimization variant or approach that has been turned down, and why.

**Read this before proposing anything** — Step 2 of the sweep, and Step 7a
before the research pass. Re-proposing something already declined wastes the
user's attention and is the fastest way to make the whole pass worth ignoring.

Two kinds of entry, and they carry different weight:

- **user** — a decision. It does not expire because the numbers are good. Only
  the stated reopening condition brings it back, and even then you *ask*, you do
  not re-propose.
- **measured** — an empirical dead end. The mechanism was built and it was not
  better. Reopening needs new evidence, not a new opinion.

Append a new entry the moment a proposal is declined, before doing anything
else. Newest first.

**Declined by the user is not the same as dead.** Where the finding is real and
measured and the user has only said *not now*, it also gets a ticket at status
`deferred` so it stays visible and re-openable — the entry here keeps the
reasoning and the reopening condition, and names the ticket. A finding that
*measurement* killed gets no ticket: it is a permanent result and lives only
here.

---

### 2026-08-07 — Lazy view construction · **user**

`main_window.py:132-158` builds all eleven views eagerly. Was ~2.0 s of startup
when raised, ~1.2 s after the sweep's other fixes.

- **Declined:** out of scope. It is an architecture change touching the splash
  progress contract and every `_TRANSACTION_INDEX`-style nav constant.
- **Reopen if:** the user asks for it directly, or startup regresses past the
  point where the remaining per-view costs matter more than the change's risk.

### 2026-08-07 — A performance-regression test · **user**

A timing assertion in the suite to catch regressions.

- **Declined:** the suite runs `-n auto --dist loadfile`; a wall-clock assertion
  in it would be flaky. Results belong in `docs/decisions.md` and
  `references/baseline.md`, which is where they now go.
- **Reopen if:** never, unless the suite gains a serial, isolated benchmark lane.

### 2026-08-07 — Recognition's 112 MB startup prewarm · **user** · ticket 004 / issue #12

`RecognitionView(conn, prewarm=True)` loads the DINOv2 ONNX session at startup
for every user: **96 → 198 MB resident**, arriving ~1 s after construction.
Build time barely moves (198 ms vs 182 ms), so latency work cannot see it.
Making prewarm lazy or opt-in would return ~103 MB to users who never open
Recognition.

- **Declined:** "leave both" — prewarm exists so the first suggestion is not a
  freeze, and that tradeoff stands.
- **Reopen if:** the user asks, or resident memory becomes a reported problem.

### 2026-08-07 — PNG → WebP for the card image cache · **user** · ticket 005 / issue #13

`card_images/` is **2,212 MB across 50,546 PNGs** (`services/cardimages.py:33`
writes `.png` unconditionally), 200×279 RGB, 48.8 KB average. Measured on a
25-file sample: **WebP q85 averages 19.7 KB — 60% smaller, ~1.2 GB saved.** Both
Pillow and Qt read and write WebP, so it clears the two-sided format constraint.

- **Declined:** "leave both". The real cost is not the encoder, it is a
  migration path for the 50k files already on disk.
- **Reopen if:** the user asks, or disk pressure becomes a reported problem.
- **Note for whoever picks it up:** AVIF is smaller still and is *not* viable —
  Pillow writes it, **Qt cannot read it**.

### 2026-08-07 — Deferring the vision imports off startup · **measured**

`import main_window` costs 383 ms, 159 ms of it `services.inference` pulling
onnxruntime (78 ms) and numpy (70 ms) for a single eager `write_back` import in
`auctions.py:96` and `transaction.py:92`, plus a second numpy chain through
`history.py → corpus → exemplars`.

Deferring all of them does exactly what it promises to the import phase —
**383 → 230 ms**, with numpy, onnxruntime and PIL gone from startup entirely —
and makes total startup **worse**:

| median of 3 | eager | deferred |
|---|---:|---:|
| Transaction build | 185 ms | 23 ms |
| Recognition build | 35 ms | **260 ms** |
| **TOTAL BUILD** | **1,228 ms** | **1,324 ms** |

Without the eager import, `RecognitionView(prewarm=True)` loads onnxruntime
itself on a background thread, contending for the import lock. The cost does not
disappear; it relocates somewhere worse. Implemented, measured, reverted.

- **Reopen if:** the Recognition prewarm decision above changes. The two are
  coupled — deferring the imports only pays off if nothing at startup pulls the
  chain, and prewarm does.

### 2026-08-07 — Dropping "redundant" `card_catalog` indexes · **measured**

The harness reported `idx_catalog_name` as a duplicate of
`idx_card_catalog_name`, and `idx_card_catalog_set_name` as a prefix of
`idx_catalog_set`. **Both were false.** The pairs differ by *collation*, which
`PRAGMA index_info` does not report; SQLite chooses between a BINARY and a
NOCASE index by the collation of the comparison, and query plans confirm all
four are live. Dropping either sends a real query to a full scan of 53,289 rows.

- **Reopen if:** never for these four. The detector now reads
  `PRAGMA index_xinfo` and reports none.

### 2026-08-07 — `InventoryService.all_positions()` N+1 · **measured**

Two queries per item across 903 items — a textbook N+1, and the single most
tempting non-finding in the repo.

- **Not worth it:** **24 ms**, and no view calls it. Only `item_position()` (one
  item) is used, by `ui/views/item_detail.py:107`.
- **Reopen if:** a view starts calling it, or the item count grows by an order
  of magnitude.

### 2026-08-07 — `VACUUM` on any of the four databases · **measured**

- **Not worth it:** `PRAGMA freelist_count` is **0 in all four**. It reclaims
  nothing.
- **Reopen if:** the freelist is non-zero — re-check before proposing, do not
  assume this stays true after a large delete or a retention policy lands.
