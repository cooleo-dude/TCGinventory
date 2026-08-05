# 06 — Card Recognition & Learning Engine

Status: **drafting** (architecture ratified 2026-07-23; model choice under
discussion). Builds on the vision-corpus foundation in
[`docs/decisions.md`](../docs/decisions.md).

A reactive, local, offline system that learns to recognise cards from images —
the specific card (identity), later its condition, and (separately, from ledger
data) its monetary significance. This spec covers **identity/detection**; the
market-analytics side is tracked separately.

## Two-part system

**Part 1 — the Recognition window (the training baseline).** Its own nav
destination. It does **not** produce ledger data; its purpose is to **improve
the model** by capturing human-annotated scenes:

- Load an image **scene of cards on a blank background** (and/or images pulled
  from online listings). Multiple images per session, sharing one card set.
- **Modes:** *Transaction* — the canvas is split into **"I Give"** and **"They
  Give"** zones by an **adjustable divider** (default 50/50 vertical); a card's
  side is the zone its box centre falls in. *Listing* — no split; one lot.
- The user **draws segment boxes** around each card; each gets an
  **auto-assigned number**.
- A linked **segments panel** (rows keyed by number) captures each card's
  identity via the existing catalog autofill, assisted by the matcher's ranked
  suggestions as they come online.
- Saving records **regions** (box geometry + identity + side) — localized,
  labeled ground truth that trains both the detector and the identifier.

**Part 2 — inference in Transaction / Auction.** Once a model exists, the entry
windows accept an image and **auto-detect + autofill every card**, surfacing
per-card **confidence / error thresholds** and flagging **possible missed
detections** for correction. Corrections flow back into Part 1's corpus. (Built
after Part 1 has accumulated data and a model is trained.)

## Data model (`vision.db`, separate from the ledger)

- `capture` — one annotation session.
- `image` — a photo in a session (content-addressed on disk; optional
  `side`/`pair_group` for front/back).
- `region` — **new**: a drawn box on an image: `image_id, number, x, y, w, h,
  side, + card identity (card_id, name, set, number, language, condition,
  grade…)`. Regions carry per-box localization + identity and **supersede** the
  image-level `capture_card`.

Bulky image files stay on disk; `vision.db` is excluded from the ledger's
exports/backups. Everything local; no cloud, no keys.

## Model approach (recommended — to be finalised in the model discussion)

**This is supervised learning with a human in the loop, not reinforcement
learning.** The data stream is *labeled* examples (a drawn box = a bounding-box
label; the assigned card = a class label), so the right tools are **supervised
metric learning + active learning + continual (batch) fine-tuning** — far more
sample-efficient and stable than RL for detection/recognition. RL-style thinking
maps only to the *acquisition* policy (which uncertain cases to ask the human to
label) and to tuning the auto-accept vs. flag **thresholds**.

**Decomposition — a class-agnostic detector + an embedding identifier
(both decided 2026-07-23):**

1. **Detector (find the cards) — one trained box proposer.**
   **YOLO-nano** (single "card" class, ONNX/CPU), trained on the drawn boxes,
   proposes class-agnostic "card" boxes as overlays the user accepts/edits. The
   canvas carries a **toggle** for it, so proposals can be shown, compared with
   the boxes already drawn, and promoted into regions. The same proposer feeds
   Part 2's inference autofill.
   - **No untrained fallback (revised 2026-08-04).** A classic-CV
     projection-profile proposer filled this slot until it was removed: it only
     worked on cards laid out with gaps on a plain mat, merged anything
     touching, and correcting its proposals cost more than drawing boxes by
     hand. Detection is therefore **dark until the detector is trained** — the
     toggle and photo-autofill propose nothing — which is the intended trade:
     hand-drawn boxes feed the corpus, and the corpus feeds the detector.
2. **Identifier (name the card) — embedding + nearest-neighbour retrieval**
   (not a fixed-class classifier: the ~47k catalog grows constantly, so
   open-vocabulary retrieval — add a reference vector, no retraining — is
   essential).
   - M-V1: **frozen DINOv2-small** (ONNX) embeddings + cosine NN against a
     narrowed/growing candidate pool.
   - M-V2: **fine-tune the embedder** (ArcFace / contrastive) on the accumulated
     `(crop, identity)` pairs — how the "flow of data" improves it — as periodic
     **offline batch** fine-tuning (not unstable per-sample online updates).

**Feedback loop:** model proposes → human confirms/corrects/adds-missed →
corrections (especially low-confidence / disagreements) are the highest-value
labels → periodic offline retrain of detector + embedder. Inference runs via
`onnxruntime`; training via offline scripts. All on-device.

## Milestones

- **M-V1a** — matcher engine ✅ **done**: `Recognizer.suggest(image, candidates)`
  (cosine-NN) + reference-embedding cache (`vision.db.card_embedding`, keyed by
  card + embedder version), pool-agnostic and headless
  (`services/recognizer.py`). Embedder is pluggable and defaults to
  **DINOv2-small via onnxruntime** — the fp32 ONNX encoder (~88 MB) now ships as
  package data (`tcginventory/models/dinov2_small.onnx`) via **Git LFS** in the
  repo and is **bundled into the frozen exe** by PyInstaller, so the identifier
  is active out of the box (CLS-token embedding, 384-dim, ImageNet preprocess).
  `model_path()` resolves the bundled package copy first, then a user-dropped
  file under the data dir. A dependency-free **PixelEmbedder** fallback
  (down-scaled grayscale) still kicks in if the model/onnxruntime are absent.
  Deps: `numpy`, `Pillow`, `onnxruntime` (all core).
- **M-V1b** — the Recognition window: annotation canvas (adjustable zones,
  numbered boxes, multiple images) + segments panel + region storage.
  Model-agnostic; collects training data before any model exists.
- **M-V1c** — matcher wired into the window ✅ **done**: a **Suggest labels**
  action crops each drawn box and ranks catalog matches with the DINOv2
  identifier against a **candidate pool** (the saved-region corpus +
  current-session labels — `VisionRepository.labeled_card_ids()`). Each segment
  row gains a **Suggestion** dropdown (score %, low-confidence marker); picking
  one labels the box and fills the name field. Embedders accept in-memory crops
  (no temp files); reference embeddings cache in `vision.db`. Frozen-model
  crop-vs-render is a weak baseline that the M-V2 fine-tune sharpens without UI
  changes.
- **M-V1.5** — the card **detector**. **Trained-only as of 2026-08-04:** the
  classic-CV proposer was deleted (see the decomposition note above), so
  `services/detector.py` is now `yolo_model_path()` / `available()`, the pure
  postprocessing (`_decode_yolo`, `_nms`, unit-tested without a model) and
  `OnnxCardDetector`. `detector_dataset.py` exports the labeled regions to a YOLO
  dataset; `training/train_detector.py` (offline, dev-only `ultralytics`/`torch`)
  fine-tunes yolov8n and exports `card_yolo.onnx`, bundled via the `models/*.onnx`
  glob like DINOv2. The window's **YOLO** toggle lights up once a model is
  present; `build_pipeline` uses it when installed and proposes nothing when it
  isn't. **No model has been trained yet**, so detection is inactive.
- **M-V1.5b — the training loop and its metrics (in progress).** Training is
  never triggered by the app: supplying images or saving a capture only *grows
  the corpus* (and re-embeds crop exemplars, which is what makes the identifier
  improve live). Retraining the detector is an explicit act, run from the
  **Operations** window ("Models & signals" group) and executed by
  `training/train_detector.py` outside the frozen app, because ultralytics/torch
  are deliberately not shipped.
  - **`model_run` + `model_epoch` (vision.db)** log every attempt, tagged by
    **`kind`** (`detector` | `condition`) with versions running per kind, plus a
    `metrics_json` column for the metrics one kind has and the other doesn't
    (a confusion matrix means nothing to a box detector). Each row holds: the
    corpus snapshot it trained on (images / boxes / cards + a **fingerprint** of
    the exact (image, box) set), the split, the hyperparameters, the scores
    (mAP50, mAP50-95, precision, recall, best epoch), the artifact (path,
    sha256, size) and whether it was **promoted** — promotion assigning the
    incrementing **model version** the UI shows as "detector v*N*". A run is
    recorded *before* training starts, so a crashed attempt leaves a trace.
  - **`services/training.py`** is the Qt-free layer both sides read:
    `corpus_snapshot` (counts + fingerprint), `detector_status` (installed?
    which version? trained on what? **stale** — the fingerprint no longer matches
    the corpus), `parse_results_csv` / `best_epoch` (ultralytics metrics → the
    run log), and `should_promote` — the gate that stops a worse model replacing
    a better one just because it trained later.
  - **Operations shows** the current version and its provenance ("trained on 20
    images / 88 boxes", stale when the corpus has moved on), the last run's
    accuracy, the per-run accuracy trend, and the **Train** trigger.
  - **Operations ▸ Models & signals (done 2026-08-04)** — three tiles:
    - **Set up model training** discovers a Python on the machine (py launcher,
      registry, PATH), builds `<data_dir>/training-env` and installs the training
      stack into it, then records what it validated so status reads never shell
      out. When no interpreter is found the tile states that plainly — a frozen
      build ships no `python.exe`, so this is the one case it cannot fix itself.
    - **Train the card detector (regions)** runs the whole orchestration in a
      worker thread. Its value line is the **last-trained marker**: version, when,
      and mAP50-95, with the corpus it trained on in the tooltip.
    - **Train the condition grader** is registered and reports its own
      last-trained marker and how many graded backs exist, but explains that the
      training path isn't defined yet rather than pretending to run.
    - Both trainers carry `stale_after_days=14`, so a model left alone for more
      than a fortnight reads **Stale** even when the corpus hasn't moved; a corpus
      that *has* moved shows Stale immediately, with "new since" in the tooltip.
  - **Condition grader ✅ done (2026-08-05), and refusing to train.** Ordinal
    ridge regression over named wear measurements from *both* faces
    (`condition_features.py`), fitted in-process by `condition_train.py` — no
    training environment, since numpy does it in milliseconds, and the artifact is
    readable JSON coefficients rather than an ONNX. One grade per **card**: faces
    join on `(pair_group, pair index, box number)`, a back's grade is the label and
    the Recognition window now mirrors condition edits across faces so fronts can't
    drift to their `NM` default. Validation is leave-one-capture-out; promotion
    compares **mean grade distance, lower wins**. The gate needs 8+ cards per grade
    across 3+ photos: on 44 graded cards the fit scores 14% exact / 1.52 grades off
    against a 0.84 baseline, so the tile greys its button and says what's missing.

- **M-V2** — learning from the ingested corpus. **Crop-exemplar retrieval ✅
  done (usable now):** every labeled region is embedded and stored
  (`region_embedding`), and `Recognizer.suggest` scores each card by its best
  matching **crop exemplar** (same visual domain as the query — accurate from
  the first labels, better with every ingested card), falling back to the
  catalog render only for unlabeled cards. Saving indexes crops automatically;
  `exemplars.reindex_all` backfills. **Fine-tune scaffold ✅ (train later):**
  `exemplars.export_identity_crops` + `training/train_identifier.py` (offline,
  dev-only torch; ArcFace on DINOv2 → `identifier_finetuned.onnx`, which
  `model_path()` auto-prefers and bundles like DINOv2). torch never enters the
  app; the exemplar path already delivers the "flow of data improves it" without
  it.
- **Inference** — Part 2 autofill in Transaction/Auction ✅ **done (read
  path):** `services/inference.py` (`run_inference` / `build_pipeline`) detects
  boxes (YOLO when a model is installed, nothing when not) and identifies each
  crop against the exemplar corpus, returning per-card confidence + a confident flag. An
  **"Autofill from photo…"** button in both windows opens `AutofillDialog` and
  adds the confirmed cards as rows (reusing `apply_catalog_card` +
  `_request_price`). Detect-only with an empty corpus; sharpens as it grows.
  The dialog is themed from the Ember Duo tokens with the Recognition window's
  vocabulary: a framed **scene canvas** (fit-to-view, dashed accent boxes,
  fixed-size numbered badges) beside one `surface2` **identity card** per
  detection (number badge, autocompleting name, set/no./qty, a confidence pill —
  green / amber ⚠ / outlined "no match"), hover cross-highlighting between a row
  and its box, a live "N to add" count, and dimmed-dashed rows for unchecked
  (skipped) detections.
  Both windows also carry a **"From clipboard"** button beside it — the same flow
  fed by the clipboard (copied image file(s), or a raw bitmap/screenshot spooled
  to a temp PNG via `ui/clipboard_images.py`, since the pipeline reads scenes
  from disk). Each window's file/clipboard pair shares one `_autofill_paths`
  runner, so the two entry points can't drift.
  **Feedback loop closed:** confirmed cards flow back into the corpus
  (`services/corpus.py` `ingest_labeled_scene` → capture + region per box +
  crop exemplar per identified card), and the dialog's Name field autocompletes
  so **corrections re-pick a real card_id** (disagreements captured as exemplars,
  not lost). So every autofilled purchase/auction also trains the models.
- **M-V3** — condition assessment (card **backs**) ✅ **done (heuristic +
  scaffold):** `services/condition.py` `assess_condition(back)` grades on the
  NM/LP/MP/HP/DMG scale. The usable-now **heuristic** counts near-white,
  low-saturation border pixels (so the yellow card border isn't read as wear);
  an `OnnxConditionModel` supersedes it when `condition_model.onnx` is present
  (bundled like DINOv2). The Recognition window's **"Card backs (condition)"**
  mode tags images `side='back'`, an **"Assess condition"** action grades each
  box, and a Condition column is saved onto the region — building the corpus.
  `export_condition_crops` + `training/train_condition.py` (offline, dev-only)
  train the classifier once graded backs exist.
- **M-Market** — liquidity / markup / volatility analytics ✅ **done**:
  `services/market_signal.py` `MarketSignalService` computes, per resale card,
  **liquidity** (sell-through, quantity-weighted days-to-sell), **markup**
  (realized margin + unrealized markup of on-hand stock vs basis), and
  **volatility** (price coefficient of variation over a window, from per-item
  price snapshots) — from the ledger + snapshots, **no vision, no feeds**. A
  sortable read-only **Market** nav screen (`ui/views/market_signal.py`) surfaces
  them with a portfolio summary. Complements `trends.py` (price direction), not a
  duplicate.

## Superseded

The interim per-entry **📷 photo-attach buttons** on Transaction/Auction are
replaced by: the Recognition window (training) + Part 2 inference autofill
(entry-form images become inference-only). They will be removed when the window
is built.

## Open questions

Model sizes/variants (DINOv2-small vs base; YOLO-nano vs -small); local training
cadence & footprint; auto-accept/flag confidence thresholds; overlap/pile
handling; whether/when to build a full open-set catalog embedding index (bulk
image download).
