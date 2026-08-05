"""Format the graded-card corpus for condition training (spec 06, M-V3).

**The unit is a physical card, not a region.** A card can appear twice — once on a
front photo and once on a back photo — and it has *one* condition. Faces are joined
by ``(pair_group, which pair within it, region.number)``: the Recognition window
copies the drawn boxes onto the back shot, so box 3 on the front is box 3 on the
back. Note ``pair_group`` is allocated **per session, not per photo pair** — one
group here holds four front/back pairs — so the front/back photos inside a group
are matched in order before their boxes are joined.

Which face's grade is the label matters: in the corpus this was written against,
every front was ``NM`` while its paired back read MP/LP/DMG/HP — because
``NM`` is the *default* on a new box. So a back's grade wins when present
(:data:`LABEL_FROM_BACK`), the front is a fallback, and any disagreement is
reported rather than silently averaged.

Two normalisations happen before anything is measured, because without them a model
learns the photograph instead of the card:

* **tighten to the card** — a drawn box usually includes mat, and *how much* mat
  varies per photo. The shipped whitening heuristic scores 33% (against 76% for
  always guessing NM) precisely because it reads that mat;
* **flatten exposure** — the same card under two lights must not look like two
  grades.

Then each face is warped to a canonical 2.5 x 3.5 rectangle so corners and edges
land in the same pixels every time, and the corner/edge patches where wear actually
lives are cut at native resolution.

Qt-free; the split is grouped **by capture** so two crops from one photo can never
straddle train and val.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from tcginventory.data.vision import VisionRepository
from tcginventory.services.condition import CONDITION_SCALE

# Best -> worst, as integers: the ordinal target the regressor predicts.
GRADES = [c.value for c in CONDITION_SCALE]
ORDINAL = {grade: i for i, grade in enumerate(GRADES)}

# A card is 2.5 x 3.5 inches; every face is warped to this before measurement.
CANONICAL_W, CANONICAL_H = 448, 627
CORNER_PX = 112  # corner patch, cut from the canonical face
EDGE_PX = 56  # edge strip depth

LABEL_FROM_BACK = "back"
LABEL_FROM_FRONT = "front"

# Training gates. Non-NM grades appearing in a single photo is the failure mode
# this guards: the model would learn that photo's background, not wear.
MIN_PER_GRADE = 8
MIN_IMAGES_PER_GRADE = 3


@dataclass(frozen=True)
class Face:
    """One photographed side of a card, and where its canonical crop was written."""

    region_id: int
    image_id: int
    capture_id: int
    side: str  # 'front' | 'back'
    source_path: str
    box: tuple[float, float, float, float]
    grade: str | None  # the grade recorded on *this* face
    canonical_path: str | None = None


@dataclass(frozen=True)
class Sample:
    """One physical card: its grade, and the faces available to judge it from."""

    key: str
    grade: str
    capture_id: int
    faces: dict[str, Face]
    label_source: str
    disagreement: str | None = None  # the other face's grade, when it differs

    @property
    def ordinal(self) -> int:
        return ORDINAL[self.grade]

    @property
    def has_both(self) -> bool:
        return "front" in self.faces and "back" in self.faces


@dataclass
class GateReport:
    """Whether the corpus can support training, and what's missing if not."""

    ok: bool
    per_grade: dict[str, int] = field(default_factory=dict)
    images_per_grade: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.ok:
            counts = " · ".join(f"{g} {self.per_grade.get(g, 0)}" for g in GRADES)
            return f"ready — {counts}"
        return "; ".join(self.reasons)


@dataclass
class ConditionDatasetSummary:
    samples: int
    both_faces: int
    front_only: int
    back_only: int
    train: int
    val: int
    per_grade: dict[str, int]
    disagreements: int
    manifest: Path
    gate: GateReport


# ---------------------------------------------------------------------------
# collecting samples (pure DB work — no images touched)
# ---------------------------------------------------------------------------


def collect_samples(repo: VisionRepository) -> list[Sample]:
    """Every graded card in the corpus, with each face that shows it.

    Regions on paired images collapse into one sample per physical card; an
    unpaired graded region becomes a single-face sample.
    """
    rows = repo.conn.execute(
        "SELECT r.id AS region_id, r.number, r.condition, r.x, r.y, r.w, r.h, "
        "i.id AS image_id, i.capture_id, i.path, i.side, i.pair_group "
        "FROM region r JOIN image i ON i.id = r.image_id "
        "WHERE r.condition IS NOT NULL AND r.condition != '' "
        "ORDER BY i.pair_group, r.number, i.side"
    ).fetchall()

    # pair_group is per session: order the fronts and the backs inside a group and
    # match them up, so image -> which pair it belongs to.
    pair_index: dict[int, int] = {}
    group_images = repo.conn.execute(
        "SELECT id, pair_group, COALESCE(side,'front') AS side FROM image "
        "WHERE pair_group IS NOT NULL ORDER BY pair_group, id"
    ).fetchall()
    by_group: dict[int, dict[str, list[int]]] = {}
    for row in group_images:
        side = row["side"].lower()
        side = side if side in ("front", "back") else "front"
        by_group.setdefault(int(row["pair_group"]), {}).setdefault(side, []).append(
            int(row["id"])
        )
    for sides in by_group.values():
        for side, ids in sides.items():  # noqa: B007 - side kept for clarity
            for index, image_id in enumerate(ids):
                pair_index[image_id] = index

    grouped: dict[str, list[Face]] = {}
    for row in rows:
        side = (row["side"] or "front").lower()
        if side not in ("front", "back"):
            side = "front"  # an unlabelled face is treated as the front
        face = Face(
            region_id=int(row["region_id"]),
            image_id=int(row["image_id"]),
            capture_id=int(row["capture_id"]),
            side=side,
            source_path=row["path"],
            box=(row["x"], row["y"], row["w"], row["h"]),
            grade=row["condition"],
        )
        # Paired faces of one card share (group, pair-within-group, box number).
        # Without a pair group there's nothing to join on, so the region stands alone.
        if row["pair_group"] is not None:
            group = int(row["pair_group"])
            index = pair_index.get(face.image_id, 0)
            key = f"pair{group}.{index}:box{int(row['number'])}"
        else:
            key = f"region{face.region_id}"
        grouped.setdefault(key, []).append(face)

    samples: list[Sample] = []
    for key, faces in grouped.items():
        by_side: dict[str, Face] = {}
        for face in faces:
            # More than one face per side in a group means several photos of the
            # same side; the first is representative enough for a label.
            by_side.setdefault(face.side, face)
        back, front = by_side.get("back"), by_side.get("front")
        if back is not None and back.grade:
            grade, source, other = back.grade, LABEL_FROM_BACK, front
        elif front is not None and front.grade:
            grade, source, other = front.grade, LABEL_FROM_FRONT, back
        else:  # pragma: no cover - the query only returns graded regions
            continue
        if grade not in ORDINAL:
            continue
        disagreement = (
            other.grade if other is not None and other.grade != grade else None
        )
        samples.append(
            Sample(
                key=key,
                grade=grade,
                capture_id=(back or front).capture_id,
                faces=by_side,
                label_source=source,
                disagreement=disagreement,
            )
        )
    return samples


def check_gate(
    samples: list[Sample],
    *,
    min_per_grade: int = MIN_PER_GRADE,
    min_images_per_grade: int = MIN_IMAGES_PER_GRADE,
) -> GateReport:
    """Refuse to train on a corpus that cannot produce an honest model.

    Two conditions, and the second is the one that bites: a grade seen in only one
    photo teaches the model that photo's lighting and background. Counting
    *distinct images* rather than samples is what catches it.
    """
    per_grade: dict[str, int] = {}
    images: dict[str, set[int]] = {}
    for sample in samples:
        per_grade[sample.grade] = per_grade.get(sample.grade, 0) + 1
        images.setdefault(sample.grade, set()).update(
            face.image_id for face in sample.faces.values()
        )
    images_per_grade = {g: len(v) for g, v in images.items()}

    reasons: list[str] = []
    for grade in GRADES:
        count = per_grade.get(grade, 0)
        seen_in = images_per_grade.get(grade, 0)
        if count < min_per_grade:
            reasons.append(f"{grade}: {count} of {min_per_grade} cards")
        elif seen_in < min_images_per_grade:
            reasons.append(
                f"{grade}: only {seen_in} photo(s), needs {min_images_per_grade}"
            )
    return GateReport(
        ok=not reasons,
        per_grade=per_grade,
        images_per_grade=images_per_grade,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# normalising a face
# ---------------------------------------------------------------------------


def _card_span(profile: np.ndarray, floor: float = 0.5) -> tuple[int, int]:
    """The contiguous run where ``profile`` stays above ``floor`` — the card."""
    above = profile >= floor
    if not above.any():
        return 0, len(profile)
    idx = np.where(above)[0]
    return int(idx[0]), int(idx[-1]) + 1


def tighten_to_card(image: Image.Image, min_area_frac: float = 0.35) -> Image.Image:
    """Trim the mat around a card so measurements read the card, not the table.

    The outer ring of the crop is assumed to be background; pixels differing from
    its median colour are card. Row and column profiles of that mask give the
    card's extent. If the result is implausibly small (a dark card on a dark mat,
    say) the original crop is kept — an over-tight crop would be worse than a
    loose one.
    """
    rgb = image.convert("RGB")
    arr = np.asarray(rgb, dtype=np.float32)
    h, w = arr.shape[:2]
    if h < 8 or w < 8:
        return rgb
    band = max(1, int(min(h, w) * 0.03))
    ring = np.concatenate(
        [
            arr[:band].reshape(-1, 3),
            arr[-band:].reshape(-1, 3),
            arr[:, :band].reshape(-1, 3),
            arr[:, -band:].reshape(-1, 3),
        ]
    )
    background = np.median(ring, axis=0)
    distance = np.linalg.norm(arr - background, axis=2)
    # Otsu-free threshold: halfway between the background and the furthest pixel
    # is stable enough here, and cheap.
    threshold = max(24.0, float(distance.max()) * 0.35)
    mask = distance > threshold
    if mask.mean() < 0.05:
        return rgb

    top, bottom = _card_span(mask.mean(axis=1))
    left, right = _card_span(mask.mean(axis=0))
    if (right - left) * (bottom - top) < min_area_frac * w * h:
        return rgb
    return rgb.crop((left, top, right, bottom))


def flatten_exposure(image: Image.Image, target: float = 128.0) -> Image.Image:
    """Scale brightness to a constant so lighting stops being a feature.

    Hue is preserved (a blue back stays blue); only the overall level moves, which
    is what differs between two photographs of the same card.
    """
    arr = np.asarray(image.convert("RGB"), dtype=np.float32)
    mean = float(arr.mean())
    if mean <= 1.0:
        return image
    scaled = np.clip(arr * (target / mean), 0, 255).astype(np.uint8)
    return Image.fromarray(scaled)


def canonical_face(
    image_path: str | Path, box: tuple[float, float, float, float]
) -> Image.Image:
    """A card face as the model should see it: tightened, exposure-flattened, and
    warped to the canonical card rectangle."""
    from tcginventory.services.exemplars import crop_region

    crop = crop_region(image_path, box)
    tightened = tighten_to_card(crop)
    flattened = flatten_exposure(tightened)
    return flattened.resize((CANONICAL_W, CANONICAL_H), Image.LANCZOS)


def face_patches(face: Image.Image) -> dict[str, Image.Image]:
    """The regions graders actually inspect, cut from a canonical face.

    Four corners and four edge strips — where whitening and fraying show — plus the
    inner area, which is where a front's surface (scratches, print lines) lives.
    """
    w, h = face.size
    c, e = CORNER_PX, EDGE_PX
    return {
        "corner_tl": face.crop((0, 0, c, c)),
        "corner_tr": face.crop((w - c, 0, w, c)),
        "corner_bl": face.crop((0, h - c, c, h)),
        "corner_br": face.crop((w - c, h - c, w, h)),
        "edge_top": face.crop((c, 0, w - c, e)),
        "edge_bottom": face.crop((c, h - e, w - c, h)),
        "edge_left": face.crop((0, c, e, h - c)),
        "edge_right": face.crop((w - e, c, w, h - c)),
        "surface": face.crop((e * 2, e * 2, w - e * 2, h - e * 2)),
    }


# ---------------------------------------------------------------------------
# writing the dataset
# ---------------------------------------------------------------------------

_MANIFEST_COLUMNS = (
    "key",
    "grade",
    "ordinal",
    "split",
    "capture_id",
    "label_source",
    "disagreement",
    "front_path",
    "front_region",
    "back_path",
    "back_region",
)


def export_condition_dataset(
    repo: VisionRepository,
    out_dir: str | Path,
    *,
    val_frac: float = 0.25,
    seed: int = 0,
    min_per_grade: int = MIN_PER_GRADE,
    min_images_per_grade: int = MIN_IMAGES_PER_GRADE,
) -> ConditionDatasetSummary:
    """Write canonical faces + a manifest, and report whether it can be trained on.

    The split is grouped **by capture**: an annotation session's photos share
    camera, lighting and mat, so letting one straddle train and val would flatter
    the model. Grades are balanced across the split as far as whole captures allow.
    """
    out = Path(out_dir)
    faces_dir = out / "faces"
    faces_dir.mkdir(parents=True, exist_ok=True)

    samples = collect_samples(repo)
    gate = check_gate(
        samples,
        min_per_grade=min_per_grade,
        min_images_per_grade=min_images_per_grade,
    )

    # Group whole captures into train/val, largest first, so the smaller side
    # fills toward val_frac without splitting a session.
    captures: dict[int, list[Sample]] = {}
    for sample in samples:
        captures.setdefault(sample.capture_id, []).append(sample)
    order = sorted(captures, key=lambda c: (-len(captures[c]), c))
    random.Random(seed).shuffle(order)
    target_val = len(samples) * val_frac
    val_captures: set[int] = set()
    in_val = 0
    for capture_id in order:
        if in_val < target_val:
            val_captures.add(capture_id)
            in_val += len(captures[capture_id])

    written: list[dict] = []
    counts = {"both": 0, "front": 0, "back": 0}
    per_grade: dict[str, int] = {}
    disagreements = 0
    for sample in samples:
        row = {
            "key": sample.key,
            "grade": sample.grade,
            "ordinal": sample.ordinal,
            "split": "val" if sample.capture_id in val_captures else "train",
            "capture_id": sample.capture_id,
            "label_source": sample.label_source,
            "disagreement": sample.disagreement or "",
        }
        for side in ("front", "back"):
            face = sample.faces.get(side)
            if face is None:
                row[f"{side}_path"] = ""
                row[f"{side}_region"] = ""
                continue
            try:
                canonical = canonical_face(face.source_path, face.box)
            except (FileNotFoundError, OSError):
                row[f"{side}_path"] = ""
                row[f"{side}_region"] = ""
                continue
            name = f"{sample.key.replace(':', '_')}_{side}.png"
            canonical.save(faces_dir / name)
            row[f"{side}_path"] = f"faces/{name}"
            row[f"{side}_region"] = face.region_id
        if row["front_path"] and row["back_path"]:
            counts["both"] += 1
        elif row["front_path"]:
            counts["front"] += 1
        elif row["back_path"]:
            counts["back"] += 1
        else:
            continue  # no readable face: nothing to learn from
        per_grade[sample.grade] = per_grade.get(sample.grade, 0) + 1
        disagreements += 1 if sample.disagreement else 0
        written.append(row)

    manifest = out / "manifest.csv"
    with open(manifest, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(_MANIFEST_COLUMNS))
        writer.writeheader()
        writer.writerows(written)
    (out / "dataset.json").write_text(
        json.dumps(
            {
                "grades": GRADES,
                "canonical_size": [CANONICAL_W, CANONICAL_H],
                "corner_px": CORNER_PX,
                "edge_px": EDGE_PX,
                "val_frac": val_frac,
                "seed": seed,
                "gate_ok": gate.ok,
                "gate": gate.reasons,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return ConditionDatasetSummary(
        samples=len(written),
        both_faces=counts["both"],
        front_only=counts["front"],
        back_only=counts["back"],
        train=sum(1 for r in written if r["split"] == "train"),
        val=sum(1 for r in written if r["split"] == "val"),
        per_grade=per_grade,
        disagreements=disagreements,
        manifest=manifest,
        gate=gate,
    )


def samples_fingerprint(samples: list[Sample]) -> str:
    """Hash of the exact cards and grades a model trained on.

    The condition analogue of the detector's corpus fingerprint: it changes when a
    card is added, removed **or regraded**, since a regrade is precisely what should
    make a condition model stale.
    """
    import hashlib

    digest = hashlib.sha256()
    for sample in sorted(samples, key=lambda s: s.key):
        faces = ",".join(
            f"{side}:{sample.faces[side].region_id}" for side in sorted(sample.faces)
        )
        digest.update(f"{sample.key}|{sample.grade}|{faces};".encode())
    return digest.hexdigest()
