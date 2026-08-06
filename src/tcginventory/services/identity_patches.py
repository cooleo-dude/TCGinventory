"""Tell reprints apart by the strip along the bottom of the card (identity Layer 1).

A global embedding recognises **artwork**, and reprints share it: measured on the
catalog, 33% of cards have a same-named card as their nearest neighbour. What
separates a Base Set Charizard from its 2023 reprint is the collector number, the
set symbol and the rarity — a few hundred pixels along the bottom edge, which a
224x224 whole-card embedding averages into nothing.

So this compares that strip directly. The embedding proposes a shortlist; this
re-ranks it on the one region where printings actually differ.

**Why a band rather than templates.** Number and symbol placement moved between
eras — bottom-right on Base, bottom-left on modern, different again on promos and
Japanese cards — and per-era templates would need validating against printings I
cannot enumerate. The bottom band contains all of it in every era, so the layer
needs no era detection to be correct.

**Why phase correlation rather than plain NCC.** Measured on synthetic printings
(``scratchpad/band_probe.py``), direct correlation of the two strips needs
sub-1%-accurate framing to say anything: a 2px shift in a 627px-tall canonical
card, or 1% of scale error, cuts the own-vs-reprint margin from 0.023 to 0.006,
and searching over translations does not rescue it — the search locks onto the
big pale strip that both printings share. Phase correlation is translation-
invariant by construction, and whitening the spectrum discards exactly that
shared low-frequency bulk, leaving the high-frequency structure (digits, symbol)
where printings actually differ. Same misframing, margin +0.13 to +0.46.

**What it is worth.** Benchmarked over 58 labelled crops against the full
49,551-card index, top-1 goes 20.7% -> 44.8% and top-5 74.1% -> 77.6%. The split
says the layer did what it was built to do rather than something incidental:
wrong-*printing* errors fall 36 -> 21 while wrong-*card* errors stay put at
10-11. See ``BAND_WEIGHT`` for the sweep behind that setting.

**What this cannot do.** The query is a photograph and the candidate is a clean
render, so the comparison spans a domain gap; it leans on the digits being
high-contrast. Under heavy misframing the truth's peak sinks into the impostor
distribution, so this is a **tie-breaker, not a decider** — hence the bounded
weight and the centred score below. It does nothing for *wrong card*: those 10
need a better encoder (Layer 3), which is exactly why the benchmark reports the
two failure kinds separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from tcginventory.services import condition_dataset as cds

# The strip is the bottom eighth of the card: enough to hold the number, set
# symbol, rarity and illustrator line on every era's layout, tight enough that
# artwork doesn't dominate the comparison.
BAND_TOP = 0.86
BAND_W, BAND_H = 224, 40

# How much the band is allowed to move the ranking, chosen by sweeping it against
# the corpus (58 labelled crops, 49,551-card pool):
#
#   weight   top-1   top-5   wrong printing / wrong card
#   0 (off)  20.7%   74.1%   36 / 10
#   0.3      37.9%   77.6%   26 / 10
#   0.5      44.8%   77.6%   21 / 11
#   1.0      48.3%   75.9%   18 / 12
#   1.5      50.0%   75.9%   17 / 12
#
# Top-1 keeps rising past 0.5, but top-5 falls back under the baseline and wrong-
# *card* errors start climbing — the band is no longer breaking ties, it is
# overruling the embedding and dragging up cards that merely have a similar strip.
# 0.5 is the last point where both metrics improve, and top-5 is what the UI
# actually shows, so the shortlist is the thing worth protecting.
BAND_WEIGHT = 0.5

# The truth's median rank is 2 and 74% of answers are already top-5, so a deep
# shortlist buys little; 50 costs a few seconds over the whole corpus and picks up
# the stragglers that sit just outside the first page.
SHORTLIST = 50


@dataclass(frozen=True)
class Scored:
    card_id: str
    embed: float
    band: float
    total: float


def canonical_front(image: Image.Image | str | Path) -> Image.Image:
    """A card face in a fixed frame, so the same pixels mean the same thing.

    Tighten to the card (a photo's crop includes mat, and how much varies), flatten
    exposure (two lightings must not read as two cards), then warp to the canonical
    rectangle — the same normalisation the condition grader depends on.
    """
    if isinstance(image, (str, Path)):
        with Image.open(image) as opened:
            source = opened.convert("RGB")
    else:
        source = image.convert("RGB")
    tightened = cds.tighten_to_card(source)
    flattened = cds.flatten_exposure(tightened)
    return flattened.resize((cds.CANONICAL_W, cds.CANONICAL_H), Image.LANCZOS)


def bottom_band(face: Image.Image) -> np.ndarray:
    """The identifying strip as a zero-mean unit-norm image, ready to correlate.

    Kept two-dimensional: phase correlation needs the 2-D spectrum to be
    translation-invariant, which is the whole reason this comparison survives
    imperfect framing.
    """
    width, height = face.size
    band = face.crop((0, int(height * BAND_TOP), width, height))
    grey = band.convert("L").resize((BAND_W, BAND_H), Image.LANCZOS)
    arr = np.asarray(grey, dtype=np.float32)
    arr -= arr.mean()  # ignore brightness: only the pattern matters
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 1e-6 else arr


def band_for_card(card_id: str, cache: dict[str, np.ndarray] | None = None):
    """The band of a catalog card's art, or None when we have no local copy."""
    if cache is not None and card_id in cache:
        return cache[card_id]
    from tcginventory.services import catalog_index

    source = catalog_index.source_for(card_id)
    band = None
    if source is not None:
        try:
            band = bottom_band(canonical_front(source))
        except (OSError, ValueError):  # pragma: no cover - unreadable art
            band = None
    if cache is not None:
        cache[card_id] = band
    return band


def spectrum(band: np.ndarray) -> np.ndarray:
    """The band's FFT, so a query can be prepared once and reused per candidate."""
    return np.fft.rfft2(band)


def similarity(
    query: np.ndarray, candidate: np.ndarray | None, *, query_fft=None
) -> float:
    """Phase-correlation peak: how well two strips agree at their best alignment.

    Whitening each frequency to unit magnitude throws away *how much* of each
    frequency is present and keeps only *where* it sits, so the comparison is
    driven by fine structure and is unmoved by translation. 1.0 for identical
    strips, and around 0.1-0.2 for unrelated ones.
    """
    if candidate is None or query.shape != candidate.shape:
        return 0.0
    cross = (spectrum(query) if query_fft is None else query_fft) * np.conj(
        spectrum(candidate)
    )
    magnitude = np.abs(cross)
    whitened = np.where(magnitude > 1e-9, cross / np.maximum(magnitude, 1e-9), 0)
    surface = np.fft.irfft2(whitened, s=query.shape)
    return max(0.0, float(surface.max()))


def rescore(
    query_image: Image.Image,
    candidates: list[tuple[str, float]],
    *,
    weight: float = BAND_WEIGHT,
    cache: dict[str, np.ndarray] | None = None,
) -> list[Scored]:
    """Re-rank ``(card_id, embedding score)`` pairs using the bottom band.

    Peaks are **centred on the shortlist's median** before they are applied. The
    absolute peak level says more about the photograph — its focus, glare and
    framing — than about any candidate, and it moves every candidate together, so
    only the spread around it carries information. Centring also makes ``weight``
    mean one fixed thing across queries instead of drifting with image quality.

    Cards with no local art keep their embedding score untouched — an unknown band
    must not push a candidate down relative to one we could measure.
    """
    try:
        query_band = bottom_band(canonical_front(query_image))
    except (OSError, ValueError):  # pragma: no cover - unreadable query
        return [Scored(cid, score, 0.0, score) for cid, score in candidates]

    cache = {} if cache is None else cache
    query_fft = spectrum(query_band)
    peaks: dict[str, float] = {}
    for card_id, _embed in candidates:
        band = band_for_card(card_id, cache)
        if band is not None:
            peaks[card_id] = similarity(query_band, band, query_fft=query_fft)

    baseline = float(np.median(list(peaks.values()))) if peaks else 0.0
    scored = [
        Scored(card_id, embed, peaks.get(card_id, 0.0), embed)
        if card_id not in peaks
        else Scored(
            card_id,
            embed,
            peaks[card_id],
            embed + weight * (peaks[card_id] - baseline),
        )
        for card_id, embed in candidates
    ]
    scored.sort(key=lambda s: s.total, reverse=True)
    return scored


def reranker(shortlist: int = SHORTLIST, weight: float = BAND_WEIGHT):
    """A ``rerank`` callable for :mod:`identity_eval`.

    Only the top ``shortlist`` are re-scored: reading and canonicalising art costs
    milliseconds per candidate, and the answer is virtually never outside the first
    twenty of a 53k pool (the truth's median rank is 1).
    """
    cache: dict[str, np.ndarray] = {}

    def rerank(query, order, ids, scores):
        head = list(order[:shortlist])
        pairs = [(ids[i], float(scores[i])) for i in head]
        crop = getattr(query, "crop_image", None)
        if crop is None:
            return order  # nothing to compare against; leave the ranking alone
        ranked = rescore(crop, pairs, weight=weight, cache=cache)
        position = {ids[i]: i for i in head}
        reordered = [position[s.card_id] for s in ranked]
        return np.array(reordered + list(order[shortlist:]), dtype=int)

    return rerank
