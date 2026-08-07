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

from dataclasses import dataclass, field, replace

# How far a model may drift from the corpus before the window says so.
#
# "Stale" means one thing and only one thing: **there is a significant amount of
# labelled data no training run has seen**. It is never a judgement about how the
# model scored — a model that has read everything you have is not stale, however
# it performs, and one that hasn't is stale even if it performs beautifully.
# Folding performance into it is what produced a flag that could not be cleared
# (2026-08-07).
#
# Significance is both absolute and proportional. Four new photos matter on a
# corpus of thirty; the same four are noise on a corpus of four hundred, and a
# flag that fires on every session's labelling is a flag you learn to ignore.
STALE_IMAGE_DRIFT = 4
STALE_IMAGE_SHARE = 0.10


def stale_threshold(corpus_images: int) -> int:
    """How many unseen images count as a corpus worth retraining on."""
    return max(STALE_IMAGE_DRIFT, int(corpus_images * STALE_IMAGE_SHARE))


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
    trained_images: int  # the corpus the *served* model trained on
    trained_boxes: int
    corpus_images: int
    corpus_boxes: int
    runs: list[dict]
    epochs: list[dict]
    state: str  # 'served' | 'stale' | 'none'
    # What the newest *finished* run saw, promoted or not. Drift is measured
    # against this: a run that read every photo and then lost on score has still
    # read every photo, and there is nothing left to show it.
    evaluated_images: int = 0
    evaluated_boxes: int = 0
    # A later run that did see the whole corpus but scored worse, so it was not
    # promoted. Reported so the screen can say why the served model is older than
    # the newest run, instead of calling it stale and sending you round the loop.
    declined: dict | None = None
    # Why that run lost, in the gate's own words, and the two comparable scores
    # behind it. Empty when the served model is the newest run.
    declined_reason: str = ""
    declined_fitness: float | None = None
    served_fitness: float | None = None

    @property
    def unseen_images(self) -> int:
        if not self.installed:
            return 0  # nothing has been trained, so nothing is behind
        return max(0, self.corpus_images - self.evaluated_images)

    @property
    def unseen_boxes(self) -> int:
        return max(0, self.corpus_boxes - self.evaluated_boxes)

    @property
    def declined_scored_higher(self) -> bool:
        """A run sitting unpromoted that the current gate would have promoted.

        Every run refused under the old mAP50-95 comparison is still in the log,
        and on this corpus the best of them beats the serving model on fitness by
        a point and a half. Retraining now *will* promote it, so the window says
        so — unlike the old staleness flag, this one clears the moment you act on
        it.
        """
        if self.declined is None or self.declined_fitness is None:
            return False
        if self.served_fitness is None:
            return True
        return self.declined_fitness > self.served_fitness


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
    # Whether the grader in use actually improves on always guessing the
    # commonest grade. A trained model that does not is not "serving" in any
    # sense worth the word, and the screen has to be able to say so.
    beats_guessing: bool = False
    # Graded cards the fitted grader was trained on, for the same corpus-drift
    # reading the detector gets.
    trained_cards: int = 0

    @property
    def ok(self) -> bool:
        return not self.reasons

    @property
    def unseen_cards(self) -> int:
        if not self.trained:
            return 0
        return max(0, self.graded_cards - self.trained_cards)

    @property
    def state(self) -> str:
        """As with the detector, ``stale`` is about the corpus and nothing else.

        A grader that has not learned to beat a constant guess is ``weak``, which
        is a different problem with a different fix — more cards in the thin
        grades, not a rerun over the same ones.
        """
        if not self.trained:
            return "blocked" if self.reasons else "none"
        if self.unseen_cards >= stale_threshold(self.graded_cards):
            return "stale"
        return "served" if self.beats_guessing else "weak"

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
    # The benchmark this stack is measured against. Identity has no single
    # artifact, so a version pins the *measurement* rather than a file — it is
    # what makes a regression detectable at all.
    version: int | None = None
    regressed: bool = False
    verdict: str = ""
    noise_floor: float = 0.0

    @property
    def state(self) -> str:
        """Identity has no corpus of its own to fall behind, so it is never
        ``stale`` — a benchmark that scored below the one in use is ``weak``."""
        if not self.pool:
            return "none"
        return "weak" if self.regressed else "served"


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


def _payload(row) -> dict:
    """A run's ``metrics_json``, or an empty dict if it has none or it is broken.

    The kind-specific metrics live here rather than in the shared columns, which
    mean four different things depending on ``kind``.
    """
    import json

    if row is None:
        return {}
    try:
        raw = row["metrics_json"]
    except (KeyError, IndexError):  # pragma: no cover - row without the column
        return {}
    try:
        return json.loads(raw or "{}") or {}
    except (ValueError, TypeError):  # pragma: no cover - corrupt payload
        return {}


def detector_summary(repo, runs) -> DetectorSummary:
    """The detector: what is served, what it scored, and how far the corpus has
    moved since it trained."""
    import json

    from tcginventory.data.vision import KIND_DETECTOR
    from tcginventory.services import fitness, training

    status = training.detector_status(repo, runs)
    history = [dict(r) for r in runs.recent(KIND_DETECTOR, limit=12)]
    current = runs.current_model(KIND_DETECTOR)
    epochs = [dict(r) for r in runs.epochs(current["id"])] if current else []

    served_fitness = None
    if current is not None:
        served_fitness = current["fitness"]
        if served_fitness is None:
            served_fitness = fitness.detector_fitness(
                {
                    "map50": current["map50"],
                    "map50_95": current["map50_95"],
                    "precision": current["precision"],
                    "recall": current["recall"],
                }
            )
    metrics = [
        Metric(
            "fitness",
            _pct(served_fitness),
            good=bool(served_fitness and served_fitness > 0.9),
        ),
        Metric("recall", _pct(status.recall)),
        Metric("precision", _pct(status.precision)),
        Metric("mAP50", _pct(status.map50)),
        Metric("mAP50-95", _pct(status.map50_95)),
    ]
    # What training has already been shown, promoted or not. A corpus cannot have
    # drifted from a model that was never trained.
    seen = status.baseline
    drifted = 0 if seen is None else status.corpus.images - seen.images
    # The newest finished run, when it is not the one being served.
    declined = None
    for row in history:
        if row["status"] != "ok" or row["promoted"]:
            continue
        if current is not None and row["id"] > current["id"]:
            declined = row
        break

    declined_reason = ""
    declined_fitness = None
    if declined is not None:
        declined_fitness = declined["fitness"]
        try:
            payload = json.loads(declined["metrics_json"] or "{}")
        except (ValueError, TypeError):  # pragma: no cover - corrupt payload
            payload = {}
        declined_reason = str(payload.get("verdict") or "")
        # When the run re-scored the incumbent on its own split, that is the
        # number to quote against it — not the incumbent's own historical score,
        # which was measured on a validation set that no longer exists.
        if payload.get("baseline_fitness") is not None:
            served_fitness = payload["baseline_fitness"]
        if declined_fitness is None:
            declined_fitness = fitness.detector_fitness(
                {
                    "map50": declined["map50"],
                    "map50_95": declined["map50_95"],
                    "precision": declined["precision"],
                    "recall": declined["recall"],
                }
            )

    summary = DetectorSummary(
        installed=status.installed,
        version=status.version,
        metrics=metrics,
        trained_at=status.trained_at,
        trained_images=status.trained_on.images if status.trained_on else 0,
        trained_boxes=status.trained_on.boxes if status.trained_on else 0,
        corpus_images=status.corpus.images,
        corpus_boxes=status.corpus.boxes,
        evaluated_images=seen.images if seen else 0,
        evaluated_boxes=seen.boxes if seen else 0,
        runs=history,
        epochs=epochs,
        state="served",  # replaced below, once `declined` is known
        declined=declined,
        declined_reason=declined_reason,
        declined_fitness=declined_fitness,
        served_fitness=served_fitness,
    )
    # Stale is a statement about the *corpus*, not about the model: labels no
    # training run has seen yet, and enough of them to be worth six minutes. How
    # the served model scored has no bearing on it — a run that beats the model in
    # use is reported separately (`declined_scored_higher`), because that is news
    # about the model rather than about the data, and mixing the two gives you a
    # flag that retraining cannot clear.
    state = (
        "none"
        if not status.installed or status.trained_on is None
        else "stale"
        if drifted >= stale_threshold(status.corpus.images)
        else "served"
    )
    return replace(summary, state=state)


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
    beats_guessing = False
    if current is not None:
        # The baseline lives in metrics_json under its own name. It used to be
        # read off the `precision` column, which for this kind holds macro-F1 —
        # so the window compared exact accuracy against an unrelated quantity and
        # announced the result as "beats guessing Near Mint" (2026-08-07).
        payload = _payload(current)
        baseline = payload.get("baseline_accuracy")
        score = current["fitness"]
        beats_guessing = bool(score is not None and score > 0)
        metrics = [
            Metric("exact", _pct(current["map50"]), good=beats_guessing),
            Metric("always-NM", _pct(baseline)),
            Metric("grades off", f"{current['map50_95']:.2f}")
            if current["map50_95"] is not None
            else Metric("grades off", "—"),
        ]
    return ConditionSummary(
        trained=current is not None,
        graded_cards=len(samples),
        coverage=coverage,
        reasons=list(gate.reasons),
        metrics=metrics,
        min_cards=cds.MIN_PER_GRADE,
        min_images=cds.MIN_IMAGES_PER_GRADE,
        beats_guessing=beats_guessing,
        trained_cards=int(current["corpus_cards"] or 0) if current is not None else 0,
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

    promoted = runs.current_model(KIND_IDENTITY, source="benchmark")
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
        version=int(promoted["version"]) if promoted and promoted["version"] else None,
        regressed=bool(payload.get("regressed")),
        verdict=str(payload.get("verdict") or ""),
        noise_floor=float(payload.get("noise_floor") or 0.0),
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


def corpus_summary(conn, repo, runs, *, detector=None, condition=None) -> CorpusSummary:
    """The corpus, and what each model is still short of.

    The feeds are the point: counts alone do not tell you that the detector has
    never seen a third of your photos, or that three grades are blocking the
    grader.

    ``detector`` and ``condition`` are accepted ready-made because the window
    has already built both by the time it asks for this. Rebuilding them here
    re-ran a corpus fingerprint, three passes over the graded samples and a second
    catalog-index probe on every refresh.
    """
    from tcginventory.services import catalog_index as ci

    detector = detector if detector is not None else detector_summary(repo, runs)
    condition = condition if condition is not None else condition_summary(repo, runs)
    index = ci.index_status(conn, repo)
    graded = condition.graded_cards

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
            f"{graded} graded · {index.indexed:,} cards indexed"
            if index.indexed
            else "catalog not indexed yet",
        ),
    ]
    return CorpusSummary(
        captures=len(per_capture),
        images=detector.corpus_images,
        boxes=detector.corpus_boxes,
        cards=repo.labeled_card_count(),
        graded=graded,
        exemplars=repo.region_embedding_count(),
        indexed=index.indexed,
        per_capture=per_capture,
        feeds=feeds,
    )


# ---------------------------------------------------------------------------
# what a finished job has to report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """The outcome of one training run, ready to put in front of someone.

    Assembled from the run log rather than from the job's return sentence: the
    log already holds the metrics, the promotion decision and the corpus it
    trained on, and a sentence would have to be parsed back apart to show any of
    it.
    """

    kind: str
    title: str
    outcome: str  # 'Promoted', 'Not promoted', 'Trained', 'Failed'…
    tone: str  # 'served' | 'stale' | 'blocked'
    headline: list[Metric]
    lines: list[str]
    layers: list[Layer] = field(default_factory=list)
    caveat: str | None = None
    notes: str = ""

    @property
    def succeeded(self) -> bool:
        return self.tone != "blocked"


def run_result(runs, kind: str) -> RunResult | None:
    """The most recent run of ``kind``, described for the results window."""
    import json

    from tcginventory.data.vision import KIND_CONDITION, KIND_DETECTOR, KIND_IDENTITY

    rows = [dict(r) for r in runs.recent(kind, limit=1)]
    if not rows:
        return None
    row = rows[0]
    payload = {}
    if row.get("metrics_json"):
        try:
            payload = json.loads(row["metrics_json"])
        except (ValueError, TypeError):  # pragma: no cover - corrupt payload
            payload = {}

    if row["status"] != "ok":
        return RunResult(
            kind=kind,
            title=_KIND_TITLES.get(kind, "Model"),
            outcome="Failed" if row["status"] == "failed" else row["status"].title(),
            tone="blocked",
            headline=[],
            lines=[],
            notes=(row["notes"] or "").strip(),
        )

    if kind == KIND_DETECTOR:
        return _detector_result(runs, row, payload)
    if kind == KIND_CONDITION:
        return _condition_result(row, payload)
    if kind == KIND_IDENTITY:
        return _identity_result(row, payload)
    return None  # pragma: no cover - a kind with no result view


_KIND_TITLES = {
    "detector": "Region detection",
    "condition": "Condition grader",
    "identity": "Card identification",
}


def _detector_result(runs, row: dict, payload: dict) -> RunResult:
    from tcginventory.data.vision import KIND_DETECTOR

    promoted = bool(row["promoted"])
    current = runs.current_model(KIND_DETECTOR)
    score = row["fitness"]
    lines = [
        f"Trained for {row['epochs'] or '—'} epochs on {row['corpus_images']} "
        f"images and {row['corpus_boxes']} boxes.",
    ]
    if promoted:
        outcome, tone = f"Promoted as v{row['version']}", "served"
        lines.append(f"Promoted because {payload.get('verdict') or 'it scored best'}.")
        lines.append("This is the model Recognition uses from now on.")
    else:
        outcome, tone = "Not promoted", "stale"
        version = f"v{current['version']}" if current else "the model in use"
        lines.append(
            f"{version} keeps serving — {payload.get('verdict') or 'it scored higher'}."
        )
        if payload.get("baseline_fitness") is not None:
            # Both numbers came off the same validation split, which is the whole
            # point of re-scoring the incumbent.
            lines.append(
                f"Both were scored on this run's own validation split: "
                f"{_pct(score)} against {_pct(payload['baseline_fitness'])}. "
                "That is a like-for-like comparison, so the gap is real rather "
                "than an artefact of the two runs sitting different exams."
            )
        elif not payload.get("comparable", True):
            lines.append(
                "The model in use was scored on a different validation split, so "
                "the two figures were never directly comparable — the corpus each "
                "one saw decided it instead."
            )
        else:
            lines.append(
                "More labelled photos will change this; training the same corpus "
                "again will not."
            )

    headline = [
        Metric("fitness", _pct(score), good=bool(score and score > 0.9)),
        Metric("recall", _pct(row["recall"])),
        Metric("precision", _pct(row["precision"])),
        Metric("mAP50", _pct(row["map50"]), good=(row["map50"] or 0) > 0.9),
        Metric("mAP50-95", _pct(row["map50_95"])),
    ]
    caveat = None
    if payload.get("noise_floor"):
        caveat = (
            f"Fitness weights recall and precision above box tightness, and this "
            f"run's own epochs vary by ±{_pct(payload['noise_floor'])} — anything "
            "smaller than that is measurement noise, not a better model."
        )
    return RunResult(
        kind=KIND_DETECTOR,
        title="Region detection",
        outcome=outcome,
        tone=tone,
        headline=headline,
        lines=lines,
        caveat=caveat,
        notes=(row["notes"] or "").strip(),
    )


def _condition_result(row: dict, payload: dict) -> RunResult:
    from tcginventory.data.vision import KIND_CONDITION

    # `precision` on a condition run is macro-F1, not the baseline; the baseline
    # is in metrics_json. Reading the column instead compared exact accuracy with
    # macro-F1 and called the difference "beats guessing" (2026-08-07).
    exact = row["map50"]
    baseline = payload.get("baseline_accuracy")
    score = row["fitness"]
    beats = bool(score is not None and score > 0)
    lines = [
        f"Fitted over {row['corpus_cards'] or payload.get('samples', 0)} graded "
        "cards, each read from both faces."
    ]
    if beats:
        lines.append(
            f"It gets {_pct(exact)} exactly right against {_pct(baseline)} for "
            "always guessing the commonest grade, so it is worth using."
        )
    else:
        lines.append(
            f"It gets {_pct(exact)} exactly right, against {_pct(baseline)} for "
            "simply guessing the commonest grade every time — so it has learned "
            "nothing a constant could not do. The grades in the corpus are still "
            "too lopsided for it to learn wear rather than lighting; more cards "
            "in the thin grades is the fix, and it will not be promoted until "
            "then."
        )
    return RunResult(
        kind=KIND_CONDITION,
        title="Condition grader",
        outcome="Trained" if beats else "Trained, but no better than guessing",
        tone="served" if beats else "stale",
        headline=[
            Metric("exact", _pct(exact), good=beats),
            Metric("always-NM", _pct(baseline)),
            Metric("lift", f"{score:+.1%}" if score is not None else "—", good=beats),
        ],
        lines=lines,
        notes=(row["notes"] or "").strip(),
    )


def _identity_result(row: dict, payload: dict) -> RunResult:
    from tcginventory.data.vision import KIND_IDENTITY

    layers: list[Layer] = []
    previous = None
    for key, label in (
        ("encoder", "DINOv2-small encoder"),
        ("scope", "+ language scope"),
        ("projection", "+ photo matching"),
        ("band", "+ bottom band"),
    ):
        value = (payload.get("layers") or {}).get(key)
        if value is None:
            continue
        value = float(value)
        layers.append(
            Layer(label, value, None if previous is None else value - previous)
        )
        previous = value

    queries = int(payload.get("queries") or row["corpus_boxes"] or 0)
    pool = int(payload.get("pool") or row["corpus_cards"] or 0)
    lines = [
        f"Scored {queries} labelled crops against {pool:,} indexed cards.",
    ]
    if layers:
        gain = layers[-1].top1 - layers[0].top1
        lines.append(
            f"The corrections are worth {gain * 100:+.1f} points of top-1 over the "
            "encoder on its own."
        )
    misses = []
    if payload.get("wrong_printing"):
        misses.append(f"{payload['wrong_printing']} wrong printing")
    if payload.get("wrong_card"):
        misses.append(f"{payload['wrong_card']} wrong card")
    if misses:
        lines.append(
            "Remaining misses: "
            + " and ".join(misses)
            + " — the wrong-card ones need a better encoder, not another layer."
        )

    if payload.get("noise_floor"):
        lines.append(
            f"With {queries} queries the error bar on top-1 is about "
            f"±{_pct(payload['noise_floor'])}, so treat anything smaller than "
            "that as the same result."
        )
    if payload.get("verdict"):
        lines.append(str(payload["verdict"]).capitalize() + ".")

    caveat = None
    if payload.get("in_sample_from") == "projection":
        caveat = (
            "Photo matching was fitted partly on these same crops, so the figures "
            "below it are in-sample and read higher than they should. The honest "
            "measure of that layer is its own held-out score."
        )
    regressed = bool(payload.get("regressed"))
    return RunResult(
        kind=KIND_IDENTITY,
        title="Card identification",
        outcome="Benchmarked — down on the one in use"
        if regressed
        else f"Benchmarked as v{row['version']}"
        if row["promoted"] and row["version"]
        else "Benchmarked",
        tone="stale" if regressed else "served",
        headline=[
            Metric("top-1", _pct(row["map50"]), good=(row["map50"] or 0) > 0.6),
            Metric("top-5", _pct(row["map50_95"]), good=(row["map50_95"] or 0) > 0.8),
        ],
        lines=lines,
        layers=layers,
        caveat=caveat,
        notes=(row["notes"] or "").strip(),
    )
