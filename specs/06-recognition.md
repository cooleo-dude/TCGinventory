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
  (`services/recognizer.py`). Embedder is pluggable: **DINOv2-small via
  onnxruntime** when the optional `vision` extra is installed and the model file
  is present at `<data_dir>/vision/models/dinov2_small.onnx`; otherwise a
  dependency-free **PixelEmbedder** fallback (down-scaled grayscale) so the
  pipeline runs and is testable before the model is provisioned. Deps added:
  `numpy`, `Pillow` (core); `onnxruntime` (optional `vision` extra).
- **M-V1b** — the Recognition window: annotation canvas (adjustable zones,
  numbered boxes, multiple images) + segments panel + region storage.
  Model-agnostic; collects training data before any model exists.
- **M-V1.5** — trained single-class card detector (YOLO-nano) from drawn boxes.
- **M-V2** — fine-tuned identifier on the corpus.
- **Inference** — Part 2 autofill in Transaction/Auction with thresholds +
  missed-detection flags.
- **M-V3** — condition assessment (uses card **backs**; identity uses **fronts**).
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
