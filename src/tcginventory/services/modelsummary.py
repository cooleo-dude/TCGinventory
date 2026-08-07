"""What the Models window says about each model, assembled without Qt.

One function per section, each returning a small frozen record the view renders
verbatim. Keeping the assembly here means the screen holds no SQL and no
judgement about what "stale" or "blocked" means — and it means every one of
those judgements can be tested headless.

The three models are deliberately *not* forced into one shape. A detector has
epochs and a promoted artifact; the condition grader has a gate it has not yet
passed; identification has no single artifact at all — it is a stack of parts,
each contributing separately. A common record would have to lie about two of
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# How far a model may drift from the corpus before the window says so. Retraining
# is minutes, and a detector that has never seen a third of your photos is the
# most actionable thing the screen can tell you.
STALE_IMAGE_DRIFT = 4


@dataclass(frozen=True)
class Metric:
    label: str
    value: str
    good: bool = False


@dataclass(frozen=True)
class DetectorSummary:
    installed: bool
    version: int | None
    metrics: list[Metric]
    trained_at: str | None
    trained_images: int
    trained_boxes: int
    corpus_images: int
    corpus_boxes: int
    runs: list[dict]
    epochs: list[dict]
    state: str  # 'served' | 'stale' | 'none'
    # A later run that did see the whole corpus but scored worse, so it was not
    # promoted. Without this the screen keeps saying "stale" after a retrain and
    # you click Retrain forever.
    declined: dict | None = None

    @property
    def unseen_images(self) -> int:
        if not self.installed:
            return 0  # nothing has been trained, so nothing is behind
        return max(0, self.corpus_images - self.trained_images)

    @property
    def unseen_boxes(self) -> int:
        return max(0, self.corpus_boxes - self.trained_boxes)


@dataclass(frozen=True)
class GradeCoverage:
    grade: str
    cards: int
    images: int
    cards_needed: int
    images_needed: int

    @property
    def ok(self) -> bool:
        return self.cards_needed == 0 and self.images_needed == 0

    @property
    def shortfall(self) -> str:
        if self.ok:
            return "Ready"
        parts = []
        if self.cards_needed:
            parts.append(f"{self.cards_needed} more")
        if self.images_needed:
            parts.append(
                f"{self.images_needed} photo{'s' if self.images_needed > 1 else ''}"
            )
        return ", ".join(parts)


@dataclass(frozen=True)
class ConditionSummary:
    trained: bool
    graded_cards: int
    coverage: list[GradeCoverage]
    reasons: list[str]
    metrics: list[Metric]
    min_cards: int
    min_images: int

    @property
    def ok(self) -> bool:
        return not self.reasons

    @property
    def state(self) -> str:
        return "served" if self.trained else "blocked" if self.reasons else "none"

    @property
    def cards_needed(self) -> int:
        return sum(c.cards_needed for c in self.coverage)


@dataclass(frozen=True)
class Component:
    name: str
    what: str
    state: str
    contribution: str
    action: str | None = None


@dataclass(frozen=True)
class Layer:
    label: str
    top1: float
    delta: float | None


@dataclass(frozen=True)
class IdentitySummary:
    components: list[Component]
    layers: list[Layer]
    metrics: list[Metric]
    queries: int
    pool: int
    measured_at: str | None
    wrong_printing: int
    wrong_card: int
    # Set when the photo-matching map was fitted on the very crops the benchmark
    # scores. The figures downstream of it are then in-sample, and the screen has
    # to say so rather than quote a number that flatters itself.
    in_sample_from: str | None = None
    held_out_top1: float | None = None

    @property
    def state(self) -> str:
        return "served" if self.pool else "none"


@dataclass(frozen=True)
class EnvironmentSummary:
    ready: bool
    flavour: str | None
    detail: str
    ultralytics: str | None
    gpu_name: str | None
    gpu_vram_mb: int
    gpu_driver: str | None
    size_bytes: int
    path: str


@dataclass(frozen=True)
class Feed:
    model: str
    state: str  # 'ok' | 'warn' | 'no'
    detail: str


@dataclass(frozen=True)
class CorpusSummary:
    captures: int
    images: int
    boxes: int
    cards: int
    graded: int
    exemplars: int
    indexed: int
    per_capture: list[int] = field(default_factory=list)
    feeds: list[Feed] = field(default_factory=list)


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def _pct(value) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def detector_summary(repo, runs) -> DetectorSummary:
    """The detector: what is served, what it scored, and how far the corpus has
    moved since it trained."""
    from tcginventory.data.vision import KIND_DETECTOR
    from tcginventory.services import training

    status = training.detector_status(repo, runs)
    history = [dict(r) for r in runs.recent(KIND_DETECTOR, limit=12)]
    current = runs.current_model(KIND_DETECTOR)
    epochs = [dict(r) for r in runs.epochs(current["id"])] if current else []

    metrics = [
        Metric(
            "mAP50", _pct(status.map50), good=bool(status.map50 and status.map50 > 0.9)
        ),
        Metric("mAP50-95", _pct(status.map50_95)),
        Metric("precision", _pct(status.precision)),
        Metric("recall", _pct(status.recall)),
    ]
    # No promoted model means no snapshot to compare against — a corpus cannot
    # have drifted from a model that was never trained.
    trained_on = status.trained_on
    drifted = 0 if trained_on is None else status.corpus.images - trained_on.images
    state = (
        "none"
        if not status.installed or trained_on is None
        else "stale"
        if drifted >= STALE_IMAGE_DRIFT
        else "served"
    )
    # The newest finished run, when it is not the one being served.
    declined = None
    for row in history:
        if row["status"] != "ok" or row["promoted"]:
            continue
        if current is not None and row["id"] > current["id"]:
            declined = row
        break

    return DetectorSummary(
        installed=status.installed,
        version=status.version,
        metrics=metrics,
        trained_at=status.trained_at,
        trained_images=trained_on.images if trained_on else 0,
        trained_boxes=trained_on.boxes if trained_on else 0,
        corpus_images=status.corpus.images,
        corpus_boxes=status.corpus.boxes,
        runs=history,
        epochs=epochs,
        state=state,
        declined=declined,
    )


def condition_summary(repo, runs) -> ConditionSummary:
    """The grader, and — while it is blocked — exactly which grades are short.

    The gate is the content here: "not trained" is useless on its own, and the
    number of cards still needed is the only thing that changes it.
    """
    from tcginventory.data.vision import KIND_CONDITION
    from tcginventory.services import condition_dataset as cds

    samples = cds.collect_samples(repo)
    gate = cds.check_gate(samples)
    current = runs.current_model(KIND_CONDITION)

    coverage = []
    for grade in ("NM", "LP", "MP", "HP", "DMG"):
        cards = int(gate.per_grade.get(grade, 0))
        images = int(gate.images_per_grade.get(grade, 0))
        coverage.append(
            GradeCoverage(
                grade=grade,
                cards=cards,
                images=images,
                cards_needed=max(0, cds.MIN_PER_GRADE - cards),
                images_needed=max(0, cds.MIN_IMAGES_PER_GRADE - images),
            )
        )

    metrics: list[Metric] = []
    if current is not None:
        metrics = [
            Metric("exact", _pct(current["map50"])),
            Metric("baseline", _pct(current["precision"])),
        ]
    return ConditionSummary(
        trained=current is not None,
        graded_cards=len(samples),
        coverage=coverage,
        reasons=list(gate.reasons),
        metrics=metrics,
        min_cards=cds.MIN_PER_GRADE,
        min_images=cds.MIN_IMAGES_PER_GRADE,
    )


def identity_summary(conn, repo, runs) -> IdentitySummary:
    """Identification as what it is: an encoder plus three corrections.

    The per-layer figures come from the last benchmark that recorded a
    breakdown; without one the components still report their own state, because
    "what is installed" is knowable without re-measuring anything.
    """
    import json

    from tcginventory.data.vision import KIND_IDENTITY
    from tcginventory.services import catalog_index as ci
    from tcginventory.services import identity_patches as ip
    from tcginventory.services import identity_projection as ipj
    from tcginventory.services.recognizer import model_path

    rows = [
        dict(r) for r in runs.recent(KIND_IDENTITY, limit=40) if r["status"] == "ok"
    ]
    bench = next((r for r in rows if r["source"] == "benchmark"), None)

    index = ci.index_status(conn, repo)
    encoder = model_path()
    fitted = ipj.matrix_path().is_file()

    layers: list[Layer] = []
    breakdown = {}
    if bench and bench.get("metrics_json"):
        try:
            breakdown = json.loads(bench["metrics_json"]).get("layers") or {}
        except (ValueError, TypeError):  # pragma: no cover - corrupt payload
            breakdown = {}
    order = [
        ("encoder", "DINOv2-small encoder"),
        ("scope", "+ language scope"),
        ("projection", "+ photo matching"),
        ("band", "+ bottom band"),
    ]
    previous = None
    for key, label in order:
        if key not in breakdown:
            continue
        value = float(breakdown[key])
        layers.append(
            Layer(label, value, None if previous is None else value - previous)
        )
        previous = value

    components = [
        Component(
            "encoder",
            "Frozen image model behind every suggestion",
            encoder.stem if encoder else "not installed",
            _pct(layers[0].top1) if layers else "—",
        ),
        Component(
            "catalog index",
            "Every card it can possibly name",
            f"{index.indexed:,} of {index.catalog:,}",
            "—",
            action="identity.index_catalog",
        ),
        Component(
            "language scope",
            "English / Japanese, chosen per card",
            "per box, in Recognition",
            _delta(layers, "+ language scope"),
        ),
        Component(
            "photo matching",
            "Corrects for the gap between a photo and catalog art",
            "fitted" if fitted else "not fitted",
            _delta(layers, "+ photo matching"),
            action="identity.fit_projection",
        ),
        Component(
            "bottom band",
            f"Separates reprints, weight {ip.BAND_WEIGHT:g}",
            "always on",
            _delta(layers, "+ bottom band"),
        ),
    ]

    metrics: list[Metric] = []
    if bench:
        metrics = [
            Metric(
                "top-1", _pct(bench["map50"]), good=bool((bench["map50"] or 0) > 0.6)
            ),
            Metric(
                "top-5",
                _pct(bench["map50_95"]),
                good=bool((bench["map50_95"] or 0) > 0.8),
            ),
        ]
    payload = {}
    if bench and bench.get("metrics_json"):
        try:
            payload = json.loads(bench["metrics_json"])
        except (ValueError, TypeError):  # pragma: no cover
            payload = {}
    held_out = None
    fit = next((r for r in rows if r["source"] == "projection"), None)
    if fit is not None:
        try:
            held_out = json.loads(fit["metrics_json"] or "{}").get("fitted_top1")
        except (ValueError, TypeError):  # pragma: no cover - corrupt payload
            held_out = None

    return IdentitySummary(
        components=components,
        layers=layers,
        metrics=metrics,
        in_sample_from=payload.get("in_sample_from"),
        held_out_top1=held_out,
        queries=int(payload.get("queries") or (bench["corpus_boxes"] if bench else 0)),
        pool=index.indexed,
        measured_at=bench["started_at"] if bench else None,
        wrong_printing=int(payload.get("wrong_printing") or 0),
        wrong_card=int(payload.get("wrong_card") or 0),
    )


def _delta(layers: list[Layer], label: str) -> str:
    for layer in layers:
        if layer.label == label and layer.delta is not None:
            return f"+{layer.delta * 100:.1f} pts"
    return "—"


def environment_summary() -> EnvironmentSummary:
    """The training environment, described the way the tiles used to."""
    from tcginventory.services import training, trainingenv

    env = training.training_env_summary()
    flavour = training.training_env_flavour() if env.ready else None
    gpu = trainingenv.detect_gpu()
    return EnvironmentSummary(
        ready=env.ready,
        flavour=flavour,
        detail=env.detail,
        ultralytics=env.version,
        gpu_name=gpu.name if gpu else None,
        gpu_vram_mb=gpu.vram_mb if gpu else 0,
        gpu_driver=gpu.driver if gpu else None,
        size_bytes=trainingenv.env_size(),
        path=str(trainingenv.env_dir()),
    )


def corpus_summary(conn, repo, runs) -> CorpusSummary:
    """The corpus, and what each model is still short of.

    The feeds are the point: counts alone do not tell you that the detector has
    never seen a third of your photos, or that three grades are blocking the
    grader.
    """
    from tcginventory.services import catalog_index as ci
    from tcginventory.services import condition_dataset as cds

    detector = detector_summary(repo, runs)
    condition = condition_summary(repo, runs)
    index = ci.index_status(conn, repo)

    rows = repo.conn.execute(
        "SELECT i.capture_id AS cap, COUNT(*) AS n "
        "FROM region r JOIN image i ON i.id = r.image_id "
        "GROUP BY i.capture_id ORDER BY i.capture_id"
    ).fetchall()
    per_capture = [int(r["n"]) for r in rows]

    feeds = [
        Feed(
            "Region detection",
            "warn" if detector.unseen_images else "ok" if detector.installed else "no",
            f"{detector.unseen_images} images drawn since it trained"
            if detector.unseen_images
            else "trained on everything you have"
            if detector.installed
            else "no model yet",
        ),
        Feed(
            "Condition",
            "ok" if condition.ok else "no",
            "ready to train"
            if condition.ok
            else f"{condition.cards_needed} more cards across "
            + ", ".join(c.grade for c in condition.coverage if not c.ok),
        ),
        Feed(
            "Card identity",
            "ok" if index.indexed else "no",
            f"{len(cds.collect_samples(repo))} graded · {index.indexed:,} cards indexed"
            if index.indexed
            else "catalog not indexed yet",
        ),
    ]
    return CorpusSummary(
        captures=len(per_capture),
        images=detector.corpus_images,
        boxes=detector.corpus_boxes,
        cards=repo.labeled_card_count(),
        graded=len(cds.collect_samples(repo)),
        exemplars=repo.region_embedding_count(),
        indexed=index.indexed,
        per_capture=per_capture,
        feeds=feeds,
    )
