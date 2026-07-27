# Decision Log

Lightweight ADR-style record of significant decisions. Newest first.

---

### 2026-07-27 — Dashboard collection-value chart reconciles with the tile

- The "Collection Value" **tile** uses current `item.market_value_cents` (incl.
  reviewed/manual/graded), while the **chart line** reconstructed value from raw
  poll `price_snapshot`s only — so the line excluded never-polled/graded/manual
  cards and used NM polls for reviewed cards, ending well below the tile
  (observed ~$1.25k gap). Confusing: two "Collection Value" figures disagreeing.
- **Fix 1 (reconcile):** `collection_value_series` now values each held lot at
  each date using the recorded poll for polled cards and the card's **current**
  value (reviewed/manual) for reviewed/never-polled cards; ripping/missing = $0;
  samples at acquisition + poll dates; and **appends a final point = the tile
  figure** (`collection_value_cents`) so the endpoint always equals the tile.
  Historical values for manual/reviewed cards are back-projected flat (honest:
  we lack their past prices).
- **Fix 2 (accurate going forward):** new `collection_value_snapshot` table
  (migration 0015) records the daily effective tile value per intent
  (`record_collection_value`, deduped per day), written on startup, after a
  price refresh (GUI + headless). The series prefers recorded points and
  back-fills older history with the reconstruction.
- Also clarified for the user: **net cash falls when buying** (purchases are
  cash out) — the money-movement line trending down is correct, not a bug.

### 2026-07-27 — Stolen / missing items: reversible $0 impairment (not a transaction)

- Right-click an Inventory row → **mark as stolen / missing** (and unmark). Backed
  by a reversible per-lot flag `lot.missing` (migration `0014`), **not** a
  transaction — the user wanted a lightweight toggle.
- **Accounting = impairment, mirroring ripping** (chosen over a realized
  write-off, confirmed by owner): a missing lot is valued at **$0 market** while
  **keeping its cost basis**, so unrealized P/L = `0 − cost` → it shows a loss
  equal to its purchase price, and portfolio collection value / unrealized drop
  accordingly. The realized-profit line is untouched. Picked for consistency with
  the existing $0-lot pattern and a small, contained code footprint; a stricter
  realized write-off (hitting the realized series, needing a loss date) is a
  possible later refinement.
- Missing lots are excluded from on-hand, the sell/give picker, and FIFO
  disposal (can't sell what's gone); the **Sold** column reads "Missing". Each
  toggle writes an append-only `audit_log` `edit` entry on the lot (money moved,
  no transaction). New invariant 9 in [spec 02](../specs/02-data-model.md#invariants-must-always-hold).

### 2026-07-24 — M-V1c: identity suggestions wired into the Recognition window

- A **Suggest labels** button crops each drawn box (in-memory, via the box's
  normalised rect against the full-res image) and ranks catalog matches with
  `Recognizer.suggest`. Each segment row gains a **Suggestion** dropdown; picking
  an entry copies that identity into the box + name field.
- **Candidate pool = the labeled corpus** (`labeled_card_ids()` over saved
  regions) **+ current-session labels** — open-vocabulary retrieval that grows as
  you annotate (active learning), not a fixed classifier. Empty pool → a notice
  that suggestions improve as you label; references cache in `vision.db`.
- Embedders now accept a `PIL.Image` as well as a path, so crops never spill to
  disk. **Known limitation:** frozen DINOv2 on photo-crop-vs-catalog-render is a
  weak baseline (advisory ⚠ under 0.6 cosine); the M-V2 fine-tune fixes accuracy
  with no further UI work. The recognizer is injectable for headless UI tests.

### 2026-07-23 — DINOv2 identifier provisioned: LFS-tracked, bundled, active by default

- The fp32 **DINOv2-small ONNX** encoder (~88 MB) is now committed to the repo
  as package data (`src/tcginventory/models/dinov2_small.onnx`) via **Git LFS**
  and **bundled into the PyInstaller exe** (spec `datas` + `recognizer`/
  `onnxruntime` hidden imports). Rationale: LFS delivers the blob to clones and
  the build; bundling makes the frozen app self-contained and offline. So the
  DINOv2 identifier is **active out of the box**, not gated on a manual download.
- **onnxruntime promoted to a core dependency** (the `[vision]` extra is gone);
  `model_path()` resolves the bundled package copy first (via
  `importlib.resources`, mirroring the app icon), then a user-dropped data-dir
  file, else `None` → PixelEmbedder fallback.
- Fixed `OnnxEmbedder` to use the **CLS token** (`last_hidden_state[0,0]`,
  384-dim) as the image embedding rather than flattening the full patch grid.
- Verified: 384-dim unit-norm embeddings, identical-image cosine ≈ 1; frozen
  exe (now larger) boots and the model + onnxruntime ship in `_internal/`.

### 2026-07-23 — M-V1a matcher engine + pluggable embedder (DINOv2 / fallback)

- `services/recognizer.py`: `Recognizer.suggest(image, candidates)` embeds a
  query and ranks candidates by cosine similarity against per-card **reference
  embeddings** cached in `vision.db.card_embedding` (keyed by card + embedder
  version; open-vocabulary, no retrain to grow the catalog). Headless,
  pool-agnostic; reference-image resolution is injectable.
- **Embedder is pluggable & version-tagged:** **DINOv2-small via onnxruntime**
  (optional `vision` extra + model at `<data_dir>/vision/models/dinov2_small.onnx`)
  or a dependency-free **PixelEmbedder** fallback (down-scaled grayscale) so the
  engine runs/tests before the ~90 MB model is provisioned — changing embedders
  re-embeds (cache key includes the version).
- Core deps added: **numpy, Pillow**; **onnxruntime** kept optional. The DINOv2
  weights are a provisioning artifact (not committed). Next: M-V1c wires
  per-box suggestions into the Recognition window.

### 2026-07-23 — Detection: classic-CV + YOLO-nano as toggleable box proposers

- Both detectors are first-class **box proposers**, shown as canvas overlays the
  user accepts/edits into regions. A **checkbox per method** toggles that
  method's boxes on/off (view either, compare both). Classic CV needs no
  training (usable day one, blank backgrounds); YOLO-nano is trained on the
  drawn boxes and improves clutter/overlap coverage. Same proposers feed Part 2
  inference. So auto-boxing is **not gated** on training — it improves with it.

### 2026-07-23 — Recognition models chosen: DINOv2-small identifier + YOLO-nano detector

- **Identifier:** frozen **DINOv2-small** (ONNX) embeddings + cosine NN retrieval
  (open-vocabulary) for M-V1 → fine-tuned (ArcFace/contrastive) on the corpus for
  M-V2. **Detector:** a trained **single-class "card" YOLO-nano** (ONNX/CPU) from
  the Recognition window's drawn boxes (classic CV only an optional annotation
  assist). **Not RL** — supervised metric-learning + active learning.
- **Consequence:** the learned detector needs annotated boxes first, so the
  manual-annotation Recognition window (Part 1) is built before Part 2's
  auto-detect. See [`specs/06-recognition.md`](../specs/06-recognition.md).

### 2026-07-23 — Recognition engine architecture ratified (spec 06)

- **Two-part system** (see [`specs/06-recognition.md`](../specs/06-recognition.md)):
  a **Recognition window** = human-in-the-loop **training baseline** (annotate
  scenes: draw numbered boxes, adjustable I-Give/They-Give zones or a single
  listing lot, label each via catalog autofill; saves localized **regions**);
  and **inference autofill** in Transaction/Auction (image → auto-detect +
  autofill with confidence + missed-detection flags), built once a model exists.
- **Data model:** `vision.db` gains a `region` table (box geometry + identity +
  side), superseding the image-level `capture_card`. The interim per-entry 📷
  photo buttons are **superseded** (removed when the window is built).
- **Model paradigm:** **supervised metric-learning + active learning**, not RL
  (the data is labeled boxes/identities, not a reward signal). Decompose into a
  **class-agnostic card detector** (classic CV now → small trained detector on
  drawn boxes) + an **embedding identifier** (open-vocab NN retrieval; frozen
  DINOv2-small to start → fine-tune on the corpus). Exact model TBD in the
  model discussion; fully local/offline (onnxruntime inference, offline-script
  training).

### 2026-07-22 — Vision: optional front/back photo pairing within a capture

- A photo can be tagged **front**/**back** and two photos **linked** as the two
  faces of the same cards; pairing is optional (not every photo has a partner).
- Stored on `image` as `side` ('front'|'back'|null) + `pair_group` (a small int
  shared by a linked front+back within the capture; additive migration on open).
  `services/vision.ingest_capture` now takes `(path, side, pair_group)` photo
  specs; the `ImageCaptureButton` dialog links a selected pair (topmost = front).

### 2026-07-22 — Vision M-V0 flow: capture-grouped scene photos (supersedes same-day entry)

- **Reframe:** a supplied photo is a **scene of one or more cards**, and a single
  transaction/auction typically has **several photos** documenting the *same*
  purchase — not one photo per card. So photos attach to the **whole entry**
  (one `📷 Photos` button in the Transaction/Auction footer), never per row.
- **Model — the `capture`:** on commit, all the entry's photos + all its cards
  are recorded as one **capture** in `vision.db`
  (`capture` → many `image`, many `capture_card`). This keeps a lot's photos
  grouped and documents *which cards the capture shows* without falsely claiming
  every card appears in every photo (image-level multi-label at the capture
  granularity; per-photo bounding boxes are a later enhancement). Replaces the
  earlier per-image `image_label` schema; `services/vision.ingest_capture`
  replaces `ingest`. `ImageLabel` → `CardLabel`.
- **Labels:** transaction captures document the **received** cards (the "photograph
  what you acquired" flow; a pure sale ingests nothing); auction captures
  document all the auction's cards. Files stay content-addressed on disk; the
  experimental pre-capture schema is dropped-and-recreated on open (throwaway).
- **Verified:** ruff clean; vision tests cover capture grouping, multiple photos
  per entry, on-disk dedup, and ingest-on-commit for both screens; full suite green.

### 2026-07-22 — Vision engine, M-V0: image ingestion + labeling corpus

- **Goal (multi-phase):** a reactive engine that learns from listings/sales to
  determine a card's **identity** and **condition** from an image, plus
  **monetary significance** (liquidity, average markup, volatility). These are
  three different problems: identity (image matching — has a no-training
  baseline vs the catalog reference images), condition (hard, data-hungry), and
  monetary (pure analytics on data we already have — parallelizable).
- **M-V0 (this increment):** let the user attach card **photos** on the
  Transaction (receive) and Auction (card) rows; on commit each photo + the
  identity/condition the user assigned = one labeled training example. No ML
  yet — this builds the corpus everything else learns from.
- **Decisions (ratified with owner):**
  - **Storage:** a separate **`vision.db`** beside the ledger (`data/vision.py`)
    for image + label metadata, with image files **content-addressed** on disk
    at `<data_dir>/vision/images/<sha256><ext>` (identical photos dedup to one
    file/row). Keeps bulky ML data out of the ledger's exports/keep-10 backup —
    mirrors `pricehistory.db` / `auctions.db`.
  - **Compute:** **fully local / offline** (no cloud vision APIs, no keys),
    consistent with the app. Model options to investigate for M-V1+ (all
    local): perceptual hashing → pretrained embedding + NN via `onnxruntime` →
    fine-tuned on the collected corpus.
  - **Ingest on commit:** photos are held in memory on the row
    (`ui/image_capture.py` `ImageCaptureButton`) and ingested (`services/vision.py`)
    when the transaction is submitted / auction saved, labelled from the row's
    final identity. Never fails the ledger op; auction rows clear after ingest
    so repeated saves don't duplicate labels. Qt-free data/service layers.
- **Milestones ahead:** M-V1 baseline identity matcher (no training, vs catalog
  images) → M-V2 learned identity (train on corpus) → M-V3 condition → M-Market
  liquidity/markup analytics (buildable now, independent of vision).
- **M-V0 limitation:** re-opening a saved transaction/auction doesn't re-load
  previously-attached photos (ingest is one-way into the corpus); a corpus
  browser/manager is a later increment.

### 2026-07-16 — Auction editor: multi-card display reuses Price Lookup's gallery

- **Decision:** The auction editor's preview is a **`CardGallery`** (the reusable
  widget the Price Lookup window uses), showing **every card in the auction** as
  a wrapping thumbnail grid; hovering a card row focuses that single card and
  leaving reverts to the full gallery (the same `show_single` / `show_gallery`
  toggle). The gallery is rebuilt (`set_cards`) **only when the card set
  changes** — pick / remove / load — so thumbnails load once and persist, per the
  Price Lookup pattern. Entry table and gallery are a **50/50** horizontal split.
- **Why:** the earlier single-`CardImagePane` preview showed one card at a time;
  an auction is a bundle, so seeing all its cards at once is the point. Reusing
  `CardGallery` inherits the fix for rapid-rebuild thumbnail pile-up
  (synchronous `setParent(None)` before `deleteLater`).
- **Note:** only catalog-matched cards (with a `card_id` + image URL) get a
  thumbnail; manually-typed cards without a match simply don't appear in the
  gallery (they still count toward value / max bid).

### 2026-07-16 — eBay auction max-bid planner (new screen, ephemeral store)

- **Decision:** Add an **eBay Auctions** nav screen for planning max bids on
  auctions you're watching. Two stacked panes: a **watchlist** table (title,
  link, total card value, and a max-bid breakdown at **70/80/90/100%**, plus
  shipping/tax and delete) over a **transaction-adjacent editor** (title + URL,
  catalog-autofilled cards with auto-priced-but-editable market values, flat
  shipping, and an editable sales-tax rate defaulting to **6%**). Clicking a
  watchlist row loads it into the editor; Save upserts.
- **Max-bid math** (`services/auctions.py`, pure/integer-cents, Qt-free): the
  percentage is an *all-in* ceiling — bid + shipping + tax ≤ `p%` of the cards'
  value. Tax is charged on **bid + shipping** (standard eBay base), so
  `max_bid(p) = p·V/(1+rate) − shipping`, clamped ≥ 0. Tax rate stored as
  integer **basis points** (600 = 6.00%) to preserve the integer-money rule.
- **Storage:** a **separate, ephemeral `auctions.db`** beside the main DB
  (`data/auctions.py`, mirroring the `pricehistory.db` precedent). Kept out of
  the ledger entirely — no audit/soft-void, and never in the ledger's CSV
  export / `import_all` / keep-10 backup. Persists across sessions; **manual
  delete only** (nothing prunes it). `auction` + child `auction_card` (cascade).
- **Reuse:** the Transaction window's inline catalog-autofill row pattern,
  `CardImagePane` hover preview, `CardCatalogRepository.search`, and
  price-on-pick — but sourced **history-first** (`pricehistory.current_price`,
  instant/offline) with a live tcgcsv fetch only as a fallback for un-swept
  cards. Verified: ruff clean, 13 new tests (math + repo + offscreen view),
  full suite green, and an end-to-end MainWindow smoke.
- **Not stored as inventory:** these are scratch bid estimates, *not* lots — no
  linkage to holdings. Winning an auction is still entered as a normal purchase
  in the Transaction window.

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

### 2026-07-14 — Card images: lazy on-disk cache, not a bulk pre-download
- **Decision:** Store each catalog card's `images.small` URL (from the
  pokemontcg.io dataset, migration 0009 + `card_catalog.image_small`) and fetch
  the PNG on demand the first time a card is hovered, caching it under
  `<data_dir>/card_images/<card_id>.png`. A single shared frameless preview
  popup (`ui/card_preview.py`) shows it while hovering autofill matches, the
  inventory grid, and transaction rows.
- **Context / why:** Measured `images.small` at ~165–170 KB each; caching all
  ~20k cards would be ~3.3 GB on disk + a 20k-request download. You only ever
  touch a small subset, so lazy caching gives the same "hover shows a local
  image" result for tens of MB.
- **Alternatives considered:** Bulk pre-download (rejected: 3.3 GB, hammers the
  free CDN); storing image bytes as SQLite BLOBs (rejected: bloats the DB and
  slows every query — files on disk keyed by card_id instead).
- **Consequences:** First hover of a new card needs network (fetched off the UI
  thread; silently skipped when offline); afterwards it's instant from disk.
  Inventory/given cards resolve their image by identity via
  `CardCatalogRepository.image_for`.

### 2026-07-14 — Japanese cards, Phase 1: catalog + images via TCGdex
- **Decision:** Add Japanese-card support in phases. Phase 1 pulls a Japanese
  catalog from **TCGdex** (`api.tcgdex.net/v2/ja`) into `card_catalog` alongside
  the English (pokemontcg.io) rows, tagged by a new `language` column (migration
  0012). Images use TCGdex's asset CDN (`{card.image}/low.png`); collector number
  = `localId`; printed total = `cardCount.official`; set code = TCGdex set id.
- **Context / why:** pokemontcg.io is English-only. TCGdex is the multilingual
  equivalent — free, no key, per-set JSON — so it mirrors our English pull.
  Catalogs coexist via `replace_for_language`, so re-pulling one language doesn't
  wipe the other. File menu now has separate English/Japanese update actions.
- **Alternatives considered:** JustTCG/paid APIs (rejected: not the catalog need,
  and condition pricing was a separate, rate-limited dead end); TCGdex's built-in
  `pricing` field (Cardmarket EUR, per-card — deferred to tcgcsv cat 85 in USD).
- **Consequences:** ~6.2k JP cards load today; ~53% have TCGdex images (older JP
  sets lack assets). Japanese names are Japanese script, so entry is by JP name
  and/or set-code+number (search now also matches `set_code`). Phase 2 = JP
  pricing via tcgcsv category 85 (link by set code → number). Phase 3 = romaji /
  cross-language search.

### 2026-07-14 — Japanese catalog source: tcgcsv cat 85, not TCGdex
- **Decision:** Source the Japanese catalog + images from **tcgcsv "Pokemon
  Japan" (category 85)** instead of TCGdex. Products with a collector Number
  become catalog cards (sealed boxes/packs skipped); `image_small` = the
  TCGplayer-CDN `imageUrl`; set_code = group abbreviation; card_id = `jp-<pid>`.
- **Context / why:** TCGdex JP was thin (~6.2k cards, ~53% with images) and used
  Japanese-script names. tcgcsv cat 85 has 448 sets → **~27k cards (more than
  English), 100% with images**, **English/romaji names** (searchable without a
  Japanese IME), full collector numbers, and it's the *same* source we'll use for
  JP pricing — so catalog + images + prices come from one bulk free feed.
  (Evaluated artofpkm.com per request: it's a Rails art gallery — no API, signed
  ephemeral ActiveStorage images, ~70 sets — not usable as a data source.)
- **Consequences:** Names are English/romaji, not Japanese script (fine for a
  US inventory app; Japanese names could be layered in from TCGdex later).
  Phase 2 (JP pricing) now just parameterizes TcgcsvProvider to category 85.

### 2026-07-14 — Japanese cards Phase 2: JP pricing via tcgcsv category 85
- **Decision:** Make `TcgcsvProvider` category-aware so it prices both English
  (category 3) and Japanese (category 85) cards. Caches are keyed by category;
  `_category_for(item)` selects by `item.language`; `_resolve_group` and
  `price_for_card(..., category=)` take the category. The transaction
  price-on-pick passes 85 for Japanese picks.
- **Context / why:** The JP catalog was pulled from tcgcsv cat 85, so a card's
  stored set name matches the tcgcsv group name exactly — group resolution is
  trivial, no set-code bridge needed. One provider, one code path, both markets.
- **Consequences:** JP items priced-on-pick get a TCGplayer JP product URL, so
  the daily refresh joins them by product id like English. Prices are USD market
  (≈ NM). Verified live.

### 2026-07-16 — Catalog-wide price history (market outlook)
- **Decision:** Record a daily market-price series for every catalog card in a
  **separate SQLite file** (`pricehistory.db`, beside the main DB), populated by
  a per-set sweep of tcgcsv (EN cat 3 via products number-join, JP cat 85 via the
  productId embedded in `jp-<id>`). **Dedup-on-change** (append a snapshot only
  when the price moves; `catalog_price_latest` holds the current price + drives
  dedup). Live retention ~1 year (rolling prune); the history is backed up
  separately, **trimmed to ~3 months, keep 3**.
- **Context / why:** A full daily table for ~47.7k cards would be ~17M rows and
  ~1.7 GB/yr, and the ledger's keep-10 auto-backup would balloon to ~19 GB. The
  separate file keeps the ledger backup small/fast (history is excluded from it),
  dedup + 1yr prune keep the live file to hundreds of MB, and the trimmed 3-month
  history backup caps its own footprint. Total ~1–2 GB.
- **Wiring:** the daily headless task runs the sweep after the owned-item refresh
  and writes a trimmed history backup; File ▸ "Update market prices" triggers it
  manually. Owned-card sets are swept first. Foundation for the market-trend
  feature (surfacing per-card history in the UI is a follow-on).
