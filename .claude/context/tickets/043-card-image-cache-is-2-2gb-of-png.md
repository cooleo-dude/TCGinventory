# 43# — The card image cache is 2.2 GB of PNG

```
status:   deferred
kind:     optimization
source:   /optimization-sweep grounded pass, 2026-08-07
mr:       43
axis:     storage
found:    2026-08-07
saving:   ~1.2 GB (2,212 MB -> ~1,021 MB as WebP q85)
risk:     medium — the encoder is trivial; migrating 50,546 existing files is not
```

## The finding

`services/cardimages.py:33` writes `.png` unconditionally. The cache is
**2,212 MB across 50,546 files** — 200×279 RGB, 48.8 KB average. It is the
largest app-owned item on disk by a factor of ten, and second only to the
`training-env` (4.6 GB, not ours) across the whole 8.7 GB data directory.

Re-encoded on a 25-file random sample:

| format | average | projected total | saving |
|---|---:|---:|---:|
| PNG (today) | 48.8 KB | 2,212 MB | — |
| **WebP q85** | **19.7 KB** | **~1,021 MB** | **~1.2 GB (60%)** |
| JPEG q88 | 26.5 KB | ~1,369 MB | 46% |

## The constraint that decides the format

**An image format is only usable if both Pillow and Qt handle it.** From
`perf.py capabilities`:

- Pillow writes WebP, JPEG **and AVIF**.
- Qt reads PNG, JPEG and WebP — **it cannot read AVIF or JPEG-XL**.

So AVIF, which would be smaller still, is out: it would produce files the UI
cannot display. A benchmark comparing compression ratios alone walks straight
into that. WebP clears both sides.

## Why it is deferred, not open

The user said to leave it. The encoder change is a one-liner; **the real cost is
a migration path for the 50,546 files already on disk**, and that is the part
worth doing deliberately rather than opportunistically.

**Reopen if:** the user asks, or disk pressure becomes a reported problem.

## If it is ever picked up

The migration is the ticket, not the encoding. Options, roughly in order of how
much they respect the existing cache:

1. **Write WebP for new art, leave PNG in place, resolve either extension on
   read.** Zero migration, saving accrues only for cards fetched from now on.
2. **Convert in the background**, driven from the existing Operations
   reclaim/index job, which already walks this directory and already reports
   progress. Deletes each PNG only after its WebP is written and re-read.
3. **Convert eagerly on next launch.** Fastest to full saving, worst first-run
   experience — 50k re-encodes.

(2) is the shape that fits: the job, the progress reporting and the run log all
exist. Note `catalog_index` globs `*.png` in four places
(`catalog_index.py:93,105,359,418`) and `cardimages.cache_dir()` keys on stem, so
the extension assumption is spread wider than the writer.

## Verification, if implemented

- `perf.py storage` — `card_images/` should fall from 2,212 MB toward ~1,021 MB.
- Every card that rendered before still renders: the read path must resolve both
  extensions during and after migration.
- `perf.py latency --views` — Inventory and History refresh must not regress;
  WebP decode is not free, and `ui/thumbcache.py` caches the *decoded* pixmap, so
  the first paint is where any cost would show.

## Notes

- Sampling was enough to size this to within a few percent and cost seconds; do
  not convert everything to find out whether it is worth converting.
- The saving is disk only. It is not a latency win, and should not be sold as one.

## Filed before the numbering change

This was issue #13 under the old scheme, where a ticket carried three unrelated numbers (ours, the issue's, the PR's). It now carries one — the merge request's — so `X#` and `X!` are the same work. The old issue is closed and points here; nothing about the finding below changed.
