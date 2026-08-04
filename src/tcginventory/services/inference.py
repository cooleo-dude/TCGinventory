"""Image → detected + identified cards (spec 06, Part 2 inference autofill).

Ties the two halves together: a **detector** finds card boxes in a scene, and the
**identity matcher** names each crop against the labeled corpus. The result is a
list of :class:`Detection` the entry windows (Transaction / Auction) turn into
pre-filled rows, flagging low-confidence guesses and leaving unidentified boxes
for the user to complete.

Detection needs the trained detector: with no model installed nothing is proposed
and the caller sees an empty list (see ``services.detector.available``).
Identification fills in as the corpus grows (empty pool → boxes with no name,
flagged for review). Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from tcginventory.data.vision import CardLabel
from tcginventory.services import exemplars
from tcginventory.services.recognizer import Suggestion

Box = tuple[float, float, float, float]  # normalised (x, y, w, h) in 0..1

# Cosine at/above which a top guess is auto-accepted rather than review-flagged.
DEFAULT_CONFIDENCE = 0.75


@dataclass
class Detection:
    number: int
    box: Box
    label: CardLabel | None  # top guess enriched from the catalog (None = unknown)
    score: float  # cosine of the top guess (0.0 when nothing matched)
    confident: bool
    alternatives: list[Suggestion] = field(default_factory=list)


def run_inference(
    image_path: str | Path,
    *,
    detect,
    recognizer,
    pool,
    resolve=None,
    conf_threshold: float = DEFAULT_CONFIDENCE,
    top: int = 5,
) -> list[Detection]:
    """Detect card boxes in ``image_path`` and identify each against ``pool``.

    ``detect(image_path) -> list[Box]`` is a box proposer; ``recognizer`` is a
    :class:`~tcginventory.services.recognizer.Recognizer`; ``resolve(card_id) ->
    CardLabel | None`` enriches a guess with catalog identity for display.
    """
    boxes = detect(image_path)
    detections: list[Detection] = []
    with Image.open(image_path) as raw:
        full = raw.convert("RGB")
    for i, box in enumerate(boxes, start=1):
        crop = exemplars.crop_box(full, box)
        ranked = recognizer.suggest(crop, pool, top=top) if pool else []
        best = ranked[0] if ranked else None
        label = resolve(best.card_id) if (best and resolve) else None
        detections.append(
            Detection(
                number=i,
                box=box,
                label=label,
                score=best.score if best else 0.0,
                confident=bool(best and best.score >= conf_threshold),
                alternatives=ranked,
            )
        )
    return detections


def write_back(pipeline, image_path, source, accepted_cards) -> None:
    """Feed confirmed autofill results back into the corpus (best-effort).

    ``accepted_cards`` carry ``box`` + ``card_id`` + ``name``/``set_name``/
    ``card_number``. No-op when ``pipeline`` doesn't expose a ``repo`` (e.g. an
    injected test fake) or nothing was accepted. Never raises — a feedback-write
    failure must not disrupt the entry it came from.
    """
    repo = getattr(pipeline, "repo", None)
    if repo is None or not accepted_cards:
        return
    embedder = getattr(getattr(pipeline, "recognizer", None), "embedder", None)
    entries = [
        (
            card.box,
            CardLabel(
                card_id=card.card_id,
                name=card.name,
                set_name=card.set_name,
                card_number=card.card_number,
            ),
            None,
        )
        for card in accepted_cards
    ]
    try:
        from tcginventory.services import corpus

        corpus.ingest_labeled_scene(repo, embedder, image_path, source, entries)
    except Exception:  # feedback is best-effort; never disrupt the entry
        pass


def catalog_label_resolver(conn):
    """A ``card_id -> CardLabel`` resolver over the local catalog (for display)."""
    from tcginventory.data.repositories import CardCatalogRepository

    catalog = CardCatalogRepository(conn)

    def resolve(card_id: str) -> CardLabel | None:
        card = catalog.get(card_id)
        if card is None:
            return CardLabel(card_id=card_id)
        return CardLabel(
            card_id=card.card_id,
            name=card.name,
            set_name=card.set_name,
            card_number=card.full_number or card.number,
            language=getattr(card, "language", None),
        )

    return resolve


class Pipeline:
    """A callable ``image_path -> list[Detection]`` that also exposes the pieces
    the corpus write-back needs (``repo`` + ``recognizer`` for exemplar indexing)."""

    def __init__(
        self, detect, recognizer, resolve, repo, conf_threshold=DEFAULT_CONFIDENCE
    ):
        self.detect = detect
        self.recognizer = recognizer
        self.resolve = resolve
        self.repo = repo
        self.conf_threshold = conf_threshold

    def __call__(self, image_path: str | Path) -> list[Detection]:
        return run_inference(
            image_path,
            detect=self.detect,
            recognizer=self.recognizer,
            pool=self.repo.labeled_card_ids(),
            resolve=self.resolve,
            conf_threshold=self.conf_threshold,
        )


def _no_detector(_image_path) -> list[Box]:
    """Stand-in when no trained detector is installed: proposes nothing, so the
    caller's empty-result path handles it instead of crashing."""
    return []


def build_pipeline(conn, vision_repo=None) -> Pipeline:
    """Assemble a ready-to-call :class:`Pipeline`.

    Uses the trained YOLO detector when a model is installed and proposes nothing
    when one isn't (there is no untrained fallback — see ``services/detector.py``);
    identifies against the labeled corpus (crop exemplars + catalog fallback).
    Building loads the ONNX model(s), so call this lazily.
    """
    from tcginventory.data.vision import VisionRepository, open_vision
    from tcginventory.services import detector as det
    from tcginventory.services.recognizer import (
        Recognizer,
        catalog_reference_resolver,
    )

    repo = vision_repo or VisionRepository(open_vision())

    detect = _no_detector
    model = det.yolo_model_path()
    if model is not None:
        try:
            detect = det.OnnxCardDetector(model).detect
        except Exception:  # pragma: no cover - bad/incompatible model
            detect = _no_detector

    recognizer = Recognizer(repo, reference_image=catalog_reference_resolver(conn))
    return Pipeline(detect, recognizer, catalog_label_resolver(conn), repo)
