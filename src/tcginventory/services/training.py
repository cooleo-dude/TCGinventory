"""Detector training: corpus snapshots, run status, and metric parsing.

The training itself runs **outside the app** (``training/train_detector.py`` needs
ultralytics/torch, deliberately not shipped). This module is the part both sides
share and that the Operations window reads:

* :func:`corpus_snapshot` — what the corpus currently holds, plus a
  **fingerprint** of the exact (image, box) set, so a model can be compared with
  the data as it stands now;
* :func:`detector_status` — is a model installed, which version, what it trained
  on, how it scored, and has the corpus moved on since (``stale``);
* :func:`parse_results_csv` — ultralytics' per-epoch metrics, so a run's curve and
  best epoch land in the run log;
* :func:`should_promote` — the gate that stops a worse model replacing a better
  one just because it trained later.

Qt-free and no SQL of its own (reads through the repositories), so it tests
headless.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from tcginventory.data.vision import DetectorRunRepository, VisionRepository
from tcginventory.services import detector

# A box is fingerprinted at this precision — enough that nudging a box by a pixel
# counts as a change, loose enough that float noise doesn't.
_BOX_PLACES = 5


@dataclass(frozen=True)
class CorpusSnapshot:
    """What the detector could train on right now."""

    images: int  # images carrying at least one box
    boxes: int
    cards: int  # distinct identified cards (identifier reach, not detector)
    fingerprint: str  # hash of the (image, box) set

    @property
    def trainable(self) -> bool:
        """Whether there is anything at all to train on."""
        return self.images > 0 and self.boxes > 0


@dataclass(frozen=True)
class DetectorStatus:
    """The detector as the Operations window reports it."""

    installed: bool  # a model file is present and will be used
    model_path: str | None
    version: int | None  # promoted model version ("detector v3")
    trained_at: str | None
    trained_on: CorpusSnapshot | None  # the snapshot behind the current model
    corpus: CorpusSnapshot  # the corpus as it stands now
    map50: float | None
    map50_95: float | None
    precision: float | None
    recall: float | None
    last_status: str | None  # last attempt's outcome, even if not promoted
    last_notes: str | None

    @property
    def never_trained(self) -> bool:
        return self.version is None

    @property
    def stale(self) -> bool:
        """True when the corpus has changed since the model was trained."""
        if self.trained_on is None:
            return self.corpus.trainable  # nothing trained yet, but data waiting
        return self.trained_on.fingerprint != self.corpus.fingerprint

    @property
    def new_images(self) -> int:
        base = self.trained_on.images if self.trained_on else 0
        return max(0, self.corpus.images - base)

    @property
    def new_boxes(self) -> int:
        base = self.trained_on.boxes if self.trained_on else 0
        return max(0, self.corpus.boxes - base)


def corpus_snapshot(repo: VisionRepository) -> CorpusSnapshot:
    """Count the trainable corpus and fingerprint its exact contents."""
    images = list(repo.images_with_regions())
    boxes = 0
    digest = hashlib.sha256()
    for img in sorted(images, key=lambda r: int(r["id"])):
        regions = repo.regions_for_image(img["id"])
        digest.update(f"i{int(img['id'])}:".encode())
        for r in regions:
            boxes += 1
            digest.update(
                (
                    f"{round(r['x'], _BOX_PLACES)},{round(r['y'], _BOX_PLACES)},"
                    f"{round(r['w'], _BOX_PLACES)},{round(r['h'], _BOX_PLACES)};"
                ).encode()
            )
    return CorpusSnapshot(
        images=len(images),
        boxes=boxes,
        cards=repo.labeled_card_count(),
        fingerprint=digest.hexdigest(),
    )


def detector_status(
    repo: VisionRepository, runs: DetectorRunRepository | None = None
) -> DetectorStatus:
    """Assemble the detector's current state for display."""
    runs = runs or DetectorRunRepository(repo.conn)
    now = corpus_snapshot(repo)
    current = runs.current_model()
    last = runs.last_run()
    model = detector.yolo_model_path()

    trained_on = None
    if current is not None:
        trained_on = CorpusSnapshot(
            images=int(current["corpus_images"] or 0),
            boxes=int(current["corpus_boxes"] or 0),
            cards=int(current["corpus_cards"] or 0),
            fingerprint=current["corpus_fingerprint"] or "",
        )
    return DetectorStatus(
        installed=model is not None,
        model_path=str(model) if model else None,
        version=int(current["version"]) if current and current["version"] else None,
        trained_at=current["finished_at"] if current else None,
        trained_on=trained_on,
        corpus=now,
        map50=current["map50"] if current else None,
        map50_95=current["map50_95"] if current else None,
        precision=current["precision"] if current else None,
        recall=current["recall"] if current else None,
        last_status=last["status"] if last else None,
        last_notes=last["notes"] if last else None,
    )


def should_promote(
    candidate: float | None, incumbent: float | None, *, margin: float = 0.0
) -> bool:
    """Whether a new model should replace the one in use.

    Compares mAP50-95. A first model always wins (nothing to lose against); a
    candidate with no measured score never does. ``margin`` demands a real
    improvement rather than noise.
    """
    if candidate is None:
        return False
    if incumbent is None:
        return True
    return candidate >= incumbent + margin


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    """Content hash of a model artifact — the run log's identity for it."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


# ultralytics results.csv column → our field. Names carry a "metrics/" prefix and
# a "(B)" box-task suffix; the loss columns are per-split.
_METRIC_COLUMNS = {
    "metrics/mAP50(B)": "map50",
    "metrics/mAP50-95(B)": "map50_95",
    "metrics/precision(B)": "precision",
    "metrics/recall(B)": "recall",
    "val/box_loss": "box_loss",
    "val/cls_loss": "cls_loss",
}


def parse_results_csv(path: str | Path) -> list[dict]:
    """ultralytics' ``results.csv`` → one dict per epoch.

    Unknown or missing columns are simply absent, so a change in their CSV layout
    degrades to fewer metrics rather than a crash.
    """
    import csv

    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            clean = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
            epoch = clean.get("epoch")
            if not epoch:
                continue
            row: dict = {"epoch": int(float(epoch))}
            for column, field in _METRIC_COLUMNS.items():
                value = clean.get(column)
                if value not in (None, ""):
                    try:
                        row[field] = float(value)
                    except ValueError:
                        pass
            rows.append(row)
    return rows


def best_epoch(epochs: list[dict], key: str = "map50_95") -> dict | None:
    """The epoch with the highest ``key`` — what the exported weights represent
    (ultralytics exports ``best.pt``, not the last epoch)."""
    scored = [e for e in epochs if e.get(key) is not None]
    return max(scored, key=lambda e: e[key]) if scored else None
