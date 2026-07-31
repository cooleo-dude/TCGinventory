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

1. **Detector (find the cards) — two box proposers, toggled per method.** Both
   propose class-agnostic "card" boxes as overlays the user accepts/edits:
   - **Classic CV** (contour/quadrilateral detection + perspective correction) —
     zero training, available immediately; strong for cards on a blank
     background.
   - **YOLO-nano** (single "card" class, ONNX/CPU) — trained on the drawn boxes;
     stronger on clutter/overlap; available once trained.

   The detection canvas carries a **checkbox per method** to toggle that
   method's proposed boxes on/off, so the user can view either, compare both,
   and promote the good ones into regions (or draw their own). The same
   proposers feed Part 2's inference autofill.
   - **Bootstrapping:** classic CV gives usable proposals from day one; YOLO
     adds a better layer as the corpus grows — so auto-boxing isn't gated on
     training, it just improves with it.
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
- **M-V1.5** — the card **detector** (two box proposers). **Phase A ✅ done:**
  a dependency-free **classic-CV proposer** (`services/detector.py`,
  `propose_card_boxes` — projection-profile segmentation over a background-diff
  mask) wired to the window's **Classic-CV boxes** toggle; proposals overlay as
  editable boxes an **Accept proposals** action promotes into regions. **Phase B
  scaffolding ✅ done (training pending a corpus):** the full trained-detector
  path exists without pulling torch into the app — `detector_dataset.py`
  exports the labeled regions to a YOLO dataset; `training/train_detector.py`
  (offline, dev-only `ultralytics`/`torch`) fine-tunes yolov8n and exports
  `card_yolo.onnx`; `OnnxCardDetector` + `yolo_model_path()` load it (bundled via
  the `models/*.onnx` glob, like DINOv2) and the **YOLO boxes** toggle lights up
  and overlays proposals once a model is present. Postprocessing (`_decode_yolo`,
  `_nms`) is pure + unit-tested. **Remaining:** label a corpus → run the trainer
  → drop the `.onnx` in (LFS).
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
  boxes (YOLO if trained, else classic-CV) and identifies each crop against the
  exemplar corpus, returning per-card confidence + a confident flag. An
  **"Autofill from photo…"** button in both windows opens `AutofillDialog`
  (scene + numbered boxes + editable guesses, ⚠ low-confidence, per-row
  include/qty) and adds the confirmed cards as rows (reusing `apply_catalog_card`
  + `_request_price`). Detect-only with an empty corpus; sharpens as it grows.
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
- **M-Market** — liquidity / markup analytics (ledger + price history; no vision).

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
