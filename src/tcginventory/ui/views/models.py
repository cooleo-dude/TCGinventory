"""Models — the recognition subsystem, on one screen.

These four sections used to be tiles in Operations, which put "train the
detector" beside "import a CSV" and gave a model nowhere to report its metrics,
its loss curve or its history. Here each model gets the room to say what it is
serving, what that scored, and what it is waiting for.

The screen renders and nothing more: every figure comes from
:mod:`tcginventory.services.modelsummary`, every job from
:mod:`tcginventory.services.modeljobs`, so no SQL and no judgement about
"stale" or "blocked" lives in the UI.

Deliberately **not** a tile grid. A detector has a hundred epochs of loss; the
grader has a gate it has not passed; identification has no single artifact at
all. One repeated tile shape would flatten exactly the differences worth seeing.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

from PySide6.QtCharts import QChart, QLineSeries, QValueAxis
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tcginventory.services import modeljobs
from tcginventory.services import modelsummary as summary
from tcginventory.services import operations as ops
from tcginventory.ui import theme
from tcginventory.ui.charts import HoverChartView
from tcginventory.ui.views._worker import TaskWorker

# Jobs that finish with something worth reading: a run row with metrics behind
# it. The maintenance jobs (indexing, reclaim, building the environment) report
# a sentence and nothing more, so they keep the plain message.
_RESULT_KINDS = {
    "models.detector.train": "detector",
    "models.condition.train": "condition",
    "identity.benchmark": "identity",
}

# A line under each heading, so a section says what it is for rather than
# leaving its contents to imply it.
_NOTES = {
    "overview": "What answers when you press Detect, Assess or Suggest.",
    "model_regions": (
        "Finds where the cards are in a photo. Trains on the boxes you draw."
    ),
    "model_condition": (
        "Grades wear from both faces of a card. Trains on the grades you set."
    ),
    "model_identity": (
        "Names a card from its picture. Assembled from four parts rather than "
        "trained as one."
    ),
    "training_env": (
        "The Python environment that trains the models. Built once, and removable."
    ),
    "corpus": "Everything the models learn from — and what each is still short of.",
}


def _mono(size: float, bold: bool = False) -> QFont:
    font = QFont(theme.mono_family())
    font.setPointSizeF(size)
    font.setBold(bold)
    return font


def _eyebrow(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("mdEyebrow")
    return label


def _pill(text: str, state: str) -> QLabel:
    """Status as a shape as well as a word, so a glance across the overview
    finds what needs attention without reading."""
    label = QLabel(text.upper())
    label.setObjectName("mdPill")
    label.setProperty("state", state)
    return label


def _button(
    text: str,
    on_click,
    *,
    primary: bool = False,
    enabled: bool = True,
    why: str = "",
):
    """An action. ``why`` explains a disabled one — a greyed button with no
    reason is indistinguishable from a broken one."""
    btn = QPushButton(text)
    btn.setObjectName("mdPrimary" if primary else "mdBtn")
    btn.setCursor(Qt.PointingHandCursor if enabled else Qt.ForbiddenCursor)
    btn.setEnabled(enabled)
    btn.setProperty("action", True)
    if why:
        btn.setToolTip(why)
    btn.clicked.connect(on_click)
    return btn


class _Section(QFrame):
    """One titled block, matching the Operations screen's frame so the two
    maintenance surfaces read as siblings."""

    def __init__(self, key: str, label: str) -> None:
        super().__init__()
        self.key = key
        self.setObjectName("mdSection")
        box = QVBoxLayout(self)
        box.setContentsMargins(15, 13, 15, 16)
        box.setSpacing(11)

        head = QVBoxLayout()
        head.setSpacing(2)
        title = QLabel(label)
        title.setObjectName("mdSectionTitle")
        head.addWidget(title)
        note = QLabel(_NOTES.get(key, ""))
        note.setObjectName("mdSectionNote")
        note.setWordWrap(True)
        head.addWidget(note)
        box.addLayout(head)

        rule = QFrame()
        rule.setObjectName("mdRule")
        rule.setFixedHeight(1)
        box.addWidget(rule)

        self.body = QVBoxLayout()
        self.body.setSpacing(11)
        box.addLayout(self.body)


class _SectionNav(QListWidget):
    """The list of sections down the left: click to jump, follows scrolling."""

    def __init__(self, on_pick) -> None:
        super().__init__()
        self.setObjectName("mdNav")
        self.setFrameShape(QFrame.NoFrame)
        self.setFixedWidth(196)
        self.setCursor(Qt.PointingHandCursor)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.currentRowChanged.connect(on_pick)

    def show_current(self, row: int) -> None:
        if row == self.currentRow() or not (0 <= row < self.count()):
            return
        self.blockSignals(True)
        try:
            self.setCurrentRow(row)
        finally:
            self.blockSignals(False)


class _Card(QFrame):
    """A served-now card: what is answering, how well, and what it needs."""

    def __init__(self, title: str, state: str, pill: str) -> None:
        super().__init__()
        self.setObjectName("mdCard")
        self.setProperty("state", state)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(14, 12, 14, 13)
        self.box.setSpacing(9)

        head = QHBoxLayout()
        head.setSpacing(8)
        name = QLabel(title)
        name.setObjectName("mdCardTitle")
        head.addWidget(name)
        head.addWidget(_pill(pill, state))
        head.addStretch(1)
        self.box.addLayout(head)

    def add(self, widget) -> None:
        self.box.addWidget(widget)

    def note(self, text: str, kind: str = "") -> QLabel:
        label = QLabel(text)
        label.setObjectName("mdCardNote" if not kind else f"mdCard{kind}")
        label.setWordWrap(True)
        self.box.addWidget(label)
        return label

    def metrics(self, items) -> None:
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(5)
        for i, metric in enumerate(items):
            key = QLabel(metric.label)
            key.setObjectName("mdMetricKey")
            value = QLabel(metric.value)
            value.setObjectName("mdMetricGood" if metric.good else "mdMetricValue")
            value.setFont(_mono(11.5, bold=True))
            grid.addWidget(key, i // 2, (i % 2) * 2)
            grid.addWidget(value, i // 2, (i % 2) * 2 + 1, Qt.AlignRight)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        self.box.addLayout(grid)

    def actions(self, buttons, hint: str = "") -> None:
        self.box.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(7)
        for btn in buttons:
            row.addWidget(btn)
        if hint:
            label = QLabel(hint)
            label.setObjectName("mdHint")
            row.addWidget(label)
        row.addStretch(1)
        self.box.addLayout(row)


class _Facts(QFrame):
    """The fact strip: a handful of figures, evenly weighted."""

    def __init__(self, facts) -> None:
        super().__init__()
        self.setObjectName("mdFacts")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(1)
        for caption, value, sub, good in facts:
            cell = QFrame()
            cell.setObjectName("mdFact")
            col = QVBoxLayout(cell)
            col.setContentsMargins(13, 10, 13, 11)
            col.setSpacing(2)
            col.addWidget(_eyebrow(caption))
            val = QLabel(value)
            val.setObjectName("mdFactGood" if good else "mdFactValue")
            val.setFont(_mono(12))
            col.addWidget(val)
            if sub:
                note = QLabel(sub)
                note.setObjectName("mdFactSub")
                col.addWidget(note)
            row.addWidget(cell, 1)


class _Bars(QWidget):
    """Horizontal bars — grade coverage, and the identity stack.

    Both answer "how far along is this", which a number alone makes you compute.
    """

    def __init__(self, rows, *, marker: float | None = None) -> None:
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(11)
        grid.setVerticalSpacing(5)
        for i, (label, fraction, tone, right) in enumerate(rows):
            name = QLabel(label)
            name.setObjectName("mdBarLabel")
            track = _Track(fraction, tone, marker)
            value = QLabel(right)
            value.setObjectName("mdBarValue")
            value.setFont(_mono(10.5))
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(name, i, 0)
            grid.addWidget(track, i, 1)
            grid.addWidget(value, i, 2)
        grid.setColumnStretch(1, 1)
        grid.setColumnMinimumWidth(0, 128)
        grid.setColumnMinimumWidth(2, 150)


class _Track(QFrame):
    """One bar. Painted rather than styled so the gate marker can sit on it."""

    def __init__(self, fraction: float, tone: str, marker: float | None) -> None:
        super().__init__()
        self._fraction = max(0.0, min(1.0, fraction))
        self._tone = tone
        self._marker = marker
        self.setFixedHeight(16)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        t = theme.active()
        colours = {"ok": t.pos, "no": t.neg, "accent": t.accent, "base": t.ink3}
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.setPen(QPen(QColor(t.line), 1))
        painter.setBrush(QColor(t.surface))
        painter.drawRoundedRect(rect, 3, 3)
        if self._fraction > 0:
            fill = QColor(colours.get(self._tone, t.accent))
            fill.setAlpha(150 if self._tone != "base" else 90)
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            width = max(2, int(rect.width() * self._fraction))
            painter.drawRoundedRect(rect.adjusted(0, 0, width - rect.width(), 0), 3, 3)
        if self._marker is not None:
            x = int(rect.width() * self._marker)
            painter.setPen(QPen(QColor(t.ink3), 2))
            painter.drawLine(x, 0, x, rect.height())
        painter.end()


class _LossChart(QWidget):
    """The detector's training curve, with a switch per line.

    Losses share the left axis and rescale to whichever are showing; mAP50 has
    the right axis and its own 0-1 scale. Every line can be hidden, including
    the last — an empty chart says something too.
    """

    SERIES = (
        ("train_box_loss", "box loss", 0, False),
        ("train_cls_loss", "class loss", 4, False),
        ("box_loss", "val box loss", 5, False),
        ("map50", "mAP50", 1, True),
    )

    def __init__(self, epochs: list[dict]) -> None:
        super().__init__()
        self._epochs = epochs
        self._shown = {"train_box_loss", "train_cls_loss", "map50"}
        # Runs recorded before the training losses were captured have only the
        # validation columns; show those rather than an empty chart.
        if not any(e.get("train_box_loss") is not None for e in epochs):
            self._shown = {"box_loss", "map50"}

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(7)

        self._chips = QHBoxLayout()
        self._chips.setSpacing(5)
        self._chips.addStretch(1)
        self._buttons: dict[str, QPushButton] = {}
        for key, label, _slot, _right in self.SERIES:
            if not any(e.get(key) is not None for e in epochs):
                continue
            chip = QPushButton(label)
            chip.setObjectName("mdChip")
            chip.setCheckable(True)
            chip.setChecked(key in self._shown)
            chip.setCursor(Qt.PointingHandCursor)
            chip.clicked.connect(lambda _c, k=key: self._toggle(k))
            self._buttons[key] = chip
            self._chips.addWidget(chip)
        col.addLayout(self._chips)

        self._view = HoverChartView()
        self._view.setMinimumHeight(216)
        # QChartView is a QGraphicsView: without this it paints its own pale
        # viewport behind the chart and frames it.
        self._view.setBackgroundBrush(Qt.transparent)
        self._view.setFrameShape(QFrame.NoFrame)
        self._view.setStyleSheet("background: transparent;")
        col.addWidget(self._view)
        self._rebuild()

    def _toggle(self, key: str) -> None:
        if key in self._shown:
            self._shown.discard(key)
        else:
            self._shown.add(key)
        self._buttons[key].setChecked(key in self._shown)
        self._rebuild()

    def _rebuild(self) -> None:
        t = theme.active()
        chart = QChart()
        chart.legend().setVisible(False)

        axis_x = QValueAxis()
        axis_x.setLabelFormat("%d")
        axis_x.setTitleText("epoch")
        axis_y = QValueAxis()
        axis_right = QValueAxis()
        axis_right.setRange(0, 1)
        axis_right.setLabelFormat("%.0'%'")
        chart.addAxis(axis_x, Qt.AlignBottom)
        chart.addAxis(axis_y, Qt.AlignLeft)
        chart.addAxis(axis_right, Qt.AlignRight)

        loss_max = 0.0
        for key, _label, _slot, right in self.SERIES:
            if key in self._shown and not right:
                for row in self._epochs:
                    value = row.get(key)
                    if value is not None:
                        loss_max = max(loss_max, float(value))
        axis_y.setRange(0, (loss_max * 1.08) if loss_max else 1.0)
        axis_y.setVisible(loss_max > 0)

        last = 1
        for key, label, slot, right in self.SERIES:
            if key not in self._shown:
                continue
            series = QLineSeries()
            series.setName(label)
            pen = QPen(QColor(t.series[slot % len(t.series)]))
            pen.setWidthF(1.9)
            series.setPen(pen)
            for row in self._epochs:
                value = row.get(key)
                if value is not None:
                    series.append(float(row["epoch"]), float(value))
                    last = max(last, int(row["epoch"]))
            if series.count() == 0:
                continue
            chart.addSeries(series)
            series.attachAxis(axis_x)
            series.attachAxis(axis_right if right else axis_y)
        axis_x.setRange(1, last)
        # Theme it after the axes exist — style_chart walks them — then match the
        # card it sits on rather than the page behind it.
        theme.style_chart(chart, t)
        chart.setBackgroundBrush(QColor(t.surface2))
        for axis in chart.axes():
            axis.setGridLineColor(QColor(t.line))
        chart.legend().setVisible(False)
        self._view.setChart(chart)


class _RunTable(QTableWidget):
    """Run history — every attempt, including the ones that did not win."""

    HEADERS = ("Run", "Started", "Status", "mAP50", "mAP50-95", "Epochs", "Outcome")

    def __init__(self, runs: list[dict]) -> None:
        super().__init__(len(runs), len(self.HEADERS))
        self.setObjectName("mdTable")
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.verticalHeader().setVisible(False)
        self.setSelectionMode(QTableWidget.NoSelection)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(len(self.HEADERS) - 1, QHeaderView.Stretch)

        for row, run in enumerate(runs):
            promoted = bool(run.get("promoted"))
            cells = [
                f"#{run['id']}",
                ops.when(run["started_at"]),
                run["status"],
                _pct(run.get("map50")),
                _pct(run.get("map50_95")),
                str(run.get("epochs") or "—"),
                self._outcome(run, promoted),
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column in (0, 1, 3, 4, 5):
                    item.setFont(_mono(10.5))
                if promoted:
                    item.setForeground(QColor(theme.active().ink))
                self.setItem(row, column, item)
        _fit_table(self)

    @staticmethod
    def _outcome(run: dict, promoted: bool) -> str:
        if run["status"] == "failed":
            return (run.get("notes") or "failed").splitlines()[0][:80]
        if promoted:
            return f"★ Promoted as v{run.get('version')}"
        return "Not promoted — no gain on the incumbent"


def _fit_table(table: QTableWidget) -> None:
    """Size a table to its contents so it never grows its own scrollbar.

    These are short, complete lists — every run, every component — and a table
    that scrolls inside a page that also scrolls is a small misery.
    """
    table.resizeRowsToContents()
    # sizeHint, not height: before the table is shown the header reports a
    # placeholder and the last row ends up clipped.
    height = table.horizontalHeader().sizeHint().height() + 2 * table.frameWidth() + 2
    for row in range(table.rowCount()):
        height += table.rowHeight(row)
    table.setFixedHeight(height)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)


def _pct(value) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


class ModelsView(QWidget):
    """The recognition subsystem. Emits ``changed`` when a job altered anything
    the read screens show."""

    changed = Signal()

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._workers: dict[str, QThread] = {}
        self.setObjectName("mdPage")
        self.setStyleSheet(_qss(theme.active()))

        self._body = QWidget()
        self._body.setObjectName("mdBody")
        self._col = QVBoxLayout(self._body)
        self._col.setContentsMargins(18, 15, 18, 18)
        self._col.setSpacing(14)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setWidget(self._body)

        self._nav = _SectionNav(self._jump)
        self._scroll.verticalScrollBar().valueChanged.connect(self._follow)

        nav_column = QVBoxLayout()
        nav_column.setContentsMargins(12, 16, 0, 16)
        nav_column.setSpacing(8)
        nav_column.addWidget(_eyebrow("On this screen"))
        nav_column.addWidget(self._nav, 1)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addLayout(nav_column)
        row.addWidget(self._scroll, 1)

        self._sections: list[_Section] = []
        # The op_id of the job in flight, if any. Training the detector takes
        # minutes, so the screen has to say so — silence reads as a dead button.
        self._running: str | None = None
        self.refresh()

    # -- rendering ----------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild every section from the current state of the models.

        A full rebuild rather than a patch: a training run changes the metrics,
        the history, the staleness, the corpus feed and the chart at once, and
        the screen is a few dozen widgets.
        """
        from tcginventory.data.vision import (
            ModelRunRepository,
            VisionRepository,
            open_vision,
        )

        while self._col.count():
            item = self._col.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Unparent before deleteLater: deletion waits for the event loop,
                # and until it runs the old widgets are still in the tree — so a
                # walk over the children would find last render's buttons too.
                widget.setParent(None)
                widget.deleteLater()
        self._sections = []

        title = QLabel("Models")
        title.setObjectName("mdTitle")
        sub = QLabel(
            "Three models, one training environment, one corpus. Everything here "
            "learns from the cards you label."
        )
        sub.setObjectName("mdSub")
        head = QWidget()
        heading = QVBoxLayout(head)
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(3)
        heading.addWidget(title)
        heading.addWidget(sub)
        self._col.addWidget(head)

        if self._running:
            self._col.addWidget(_running_banner(self._running))

        vision = None
        try:
            vision = open_vision()
            repo = VisionRepository(vision)
            runs = ModelRunRepository(vision)
            detector = summary.detector_summary(repo, runs)
            condition = summary.condition_summary(repo, runs)
            identity = summary.identity_summary(self._conn, repo, runs)
            # Hand over what has already been built: assembling it twice cost a
            # second fingerprint of the corpus and three more passes over the
            # graded samples, per refresh.
            corpus = summary.corpus_summary(
                self._conn, repo, runs, detector=detector, condition=condition
            )
        except Exception as exc:  # pragma: no cover - vision DB unavailable
            warning = QLabel(f"The recognition corpus is unavailable — {exc}")
            warning.setObjectName("mdSub")
            self._col.addWidget(warning)
            self._col.addStretch(1)
            return
        finally:
            if vision is not None:
                vision.close()
        env = summary.environment_summary()

        self._overview(detector, condition, identity)
        self._regions(detector, env)
        self._condition(condition)
        self._identity(identity)
        self._environment(env)
        self._corpus(corpus)
        self._col.addStretch(1)

        self._nav.clear()
        for section in self._sections:
            self._nav.addItem(section.property("navLabel"))
        self._nav.show_current(0)

        if self._running:
            # One job at a time: they share the corpus, the run log and the GPU.
            for button in self.findChildren(QPushButton):
                if button.property("action"):
                    button.setEnabled(False)
                    button.setToolTip("Another job is running")

    def _show_running(self) -> None:
        """Put the running banner up and lock the actions, touching nothing else.

        The cheap half of :meth:`refresh` — no vision DB, no corpus fingerprint,
        no catalog probe. Starting a job is the one moment where the screen has
        to react instantly and has nothing new to say.
        """
        if self._running:
            self._col.insertWidget(1, _running_banner(self._running))
        for button in self.findChildren(QPushButton):
            if button.property("action"):
                button.setEnabled(False)
                button.setToolTip("Another job is running")

    def _section(self, key: str, label: str, nav: str | None = None) -> _Section:
        section = _Section(key, label)
        section.setProperty("navLabel", nav or label)
        self._col.addWidget(section)
        self._sections.append(section)
        return section

    # -- sections -----------------------------------------------------------

    def _overview(self, detector, condition, identity) -> None:
        section = self._section("overview", "Serving right now", "Overview")
        cards = QWidget()
        row = QHBoxLayout(cards)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(11)

        # detector
        pill = {"served": "Serving", "stale": "Stale", "none": "No model"}
        card = _Card("Region detection", detector.state, pill[detector.state])
        if detector.installed:
            card.note(f"Serving card_yolo v{detector.version}", "Who")
            card.metrics(detector.metrics)
            card.note(
                f"Trained {ops.when(detector.trained_at)} on "
                f"{detector.trained_images} images · {detector.trained_boxes} boxes"
            )
            if detector.unseen_images:
                card.note(
                    f"⚠ {detector.unseen_images} images and {detector.unseen_boxes} "
                    "boxes drawn since anything trained",
                    "Flag",
                )
            elif detector.declined_scored_higher:
                card.note(
                    f"⚠ Run #{detector.declined['id']} scores "
                    f"{_pct(detector.declined_fitness)} against this model's "
                    f"{_pct(detector.served_fitness)} — retrain to promote it.",
                    "Flag",
                )
            elif detector.declined:
                # Everything has been trained on; a newer run simply lost. Saying
                # "stale" here, with a highlighted Retrain beside it, is what sent
                # this corpus round the same run five times.
                card.note(
                    f"A newer run (#{detector.declined['id']}) has seen the whole "
                    "corpus and did not beat this one — nothing to retrain until "
                    "more photos are labelled."
                )
        else:
            card.note("No detector yet — draw boxes in Recognition, then train.")
        card.actions(
            [
                _button(
                    "Retrain" if detector.installed else "Train",
                    self._train_detector,
                    primary=detector.state == "stale",
                )
            ],
            "~6 min" if _gpu_ready() else "",
        )
        row.addWidget(card, 1)

        # condition
        card = _Card(
            "Condition",
            condition.state,
            "Serving"
            if condition.beats_guessing
            else "No better than guessing"
            if condition.trained
            else "No model",
        )
        if condition.trained:
            card.note(
                "Serving a trained grader"
                if condition.beats_guessing
                else "A grader is fitted, but it does not beat always guessing "
                "the commonest grade — so it is not promoted",
                "Who",
            )
            card.metrics(condition.metrics)
        else:
            card.note("Serving a <b>heuristic</b>, not a trained model", "Who")
            card.note(
                "It reads the mat and the lighting as much as the card, so treat "
                "its grades as a starting point."
            )
            card.note(
                f"{condition.graded_cards} graded cards · "
                f"{len([c for c in condition.coverage if not c.ok])} of 5 grades "
                "below the gate"
            )
        card.actions(
            [
                _button(
                    "Train",
                    self._train_condition,
                    enabled=condition.ok,
                    why=_gate_reason(condition),
                ),
                _button("What's missing?", lambda: self._jump_key("model_condition")),
            ]
        )
        row.addWidget(card, 1)
        section.body.addWidget(cards)

        # identity spans the width: it is a stack, not a single figure
        card = _Card(
            "Card identity",
            identity.state,
            "Down on last"
            if identity.regressed
            else f"Serving v{identity.version}"
            if identity.version
            else "Serving",
        )
        card.note("Not one model — a stack. Each layer's measured contribution:", "Who")
        if identity.regressed:
            card.note(
                f"⚠ The last benchmark scored below the one in use — "
                f"{identity.verdict}",
                "Flag",
            )
        if identity.layers:
            rows = []
            for layer in identity.layers:
                delta = "" if layer.delta is None else f"  {layer.delta * 100:+.1f}"
                rows.append(
                    (
                        layer.label,
                        layer.top1,
                        "base" if layer.delta is None else "accent",
                        f"top-1 {layer.top1 * 100:.1f}%{delta}",
                    )
                )
            card.add(_Bars(rows))
            card.note(
                f"Measured on {identity.queries} labelled crops against "
                f"{identity.pool:,} cards · "
                f"top-5 {identity.metrics[1].value if identity.metrics else '—'}"
            )
            if identity.in_sample_from:
                card.note(
                    "⚠ Photo matching was fitted on these same crops, so the two "
                    "figures below it are in-sample. Its held-out score is "
                    f"{_pct(identity.held_out_top1)}.",
                    "Flag",
                )
        else:
            card.note("No benchmark yet — run one to see what each layer is worth.")
        card.actions([_button("Re-run benchmark", self._benchmark_identity)])
        section.body.addWidget(card)

    def _regions(self, detector, env) -> None:
        section = self._section("model_regions", "Region detection")
        if detector.declined_scored_higher:
            # A run refused under the old mAP50-95 comparison that the current
            # gate would promote. Retraining clears this, so it is worth pressing.
            declined = detector.declined
            section.body.addWidget(
                _banner(
                    f"<b>A run in the log already beats the model in use.</b> Run "
                    f"#{declined['id']} scored {_pct(detector.declined_fitness)} "
                    f"against v{detector.version}'s "
                    f"{_pct(detector.served_fitness)} — it was refused under the "
                    "old comparison, which weighed box tightness above finding "
                    "cards. Training again will promote it.",
                    _button(
                        "Retrain and promote", self._train_detector, primary=True
                    ),
                    tone="warn",
                )
            )
        elif detector.declined and not detector.unseen_images:
            # Everything labelled has been trained on; the newest run simply did
            # not win. That is a finished state, not a chore — the banner says so
            # once, calmly, and does not push Retrain.
            declined = detector.declined
            reason = detector.declined_reason or (
                "it did not beat the model already in use"
            )
            section.body.addWidget(
                _banner(
                    f"<b>The whole corpus has been trained on.</b> Run "
                    f"#{declined['id']} saw all {detector.evaluated_images} images "
                    f"and scored {_pct(detector.declined_fitness)} against v"
                    f"{detector.version}'s {_pct(detector.served_fitness)} — "
                    f"{reason}. More labelled photos will move this; training the "
                    "same corpus again will not.",
                    _button("Train anyway", self._train_detector),
                    tone="calm",
                )
            )
        elif detector.unseen_images:
            section.body.addWidget(
                _banner(
                    f"<b>There are labels no training run has seen.</b> The last "
                    f"run covered {detector.evaluated_images} images / "
                    f"{detector.evaluated_boxes} boxes; the corpus now holds "
                    f"{detector.corpus_images} / {detector.corpus_boxes} — the "
                    f"{detector.unseen_images} newest have never been trained on.",
                    _button(
                        "Retrain on the full corpus", self._train_detector, primary=True
                    ),
                    tone="warn",
                )
            )
        if not env.ready:
            section.body.addWidget(
                _banner(
                    "Training needs the environment below, which is not set up yet.",
                    _button("Set it up", lambda: self._jump_key("training_env")),
                )
            )
        if detector.epochs:
            wrap = QFrame()
            wrap.setObjectName("mdChartCard")
            box = QVBoxLayout(wrap)
            box.setContentsMargins(13, 11, 13, 9)
            box.setSpacing(6)
            caption = QLabel(
                f"Run #{detector.runs[0]['id'] if detector.runs else '—'} · "
                f"{len(detector.epochs)} epochs"
                + (f" · promoted as v{detector.version}" if detector.version else "")
            )
            caption.setObjectName("mdChartTitle")
            box.addWidget(caption)
            box.addWidget(_LossChart(detector.epochs))
            note = QLabel(
                "Losses share the left axis and rescale to whichever lines are "
                "showing; mAP50 uses the right. Validation figures repeat on "
                "alternate epochs — ultralytics does not validate on every one."
            )
            note.setObjectName("mdCaption")
            note.setWordWrap(True)
            box.addWidget(note)
            section.body.addWidget(wrap)
        if detector.runs:
            section.body.addWidget(_eyebrow("Run history"))
            section.body.addWidget(_RunTable(detector.runs))

    def _condition(self, condition) -> None:
        section = self._section("model_condition", "Condition")
        section.body.addWidget(
            _banner(
                f"Training needs <b>{condition.min_cards} cards</b> and "
                f"<b>{condition.min_images} separate photos</b> per grade, so the "
                "model learns wear rather than one afternoon's lighting."
                + ("" if condition.ok else " Some grades are short."),
                None,
                tone="calm",
            )
        )
        biggest = max((c.cards for c in condition.coverage), default=1) or 1
        rows = [
            (
                c.grade,
                c.cards / max(biggest, condition.min_cards),
                "ok" if c.ok else "no",
                f"{c.cards} cards · {c.images} photos   {c.shortfall}",
            )
            for c in condition.coverage
        ]
        section.body.addWidget(
            _Bars(rows, marker=condition.min_cards / max(biggest, condition.min_cards))
        )
        marker = QLabel("The marker on each bar is the gate.")
        marker.setObjectName("mdCaption")
        section.body.addWidget(marker)

        buttons = QWidget()
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        row.addWidget(
            _button(
                "Train",
                self._train_condition,
                enabled=condition.ok,
                primary=condition.ok,
                why=_gate_reason(condition),
            )
        )
        if not condition.ok:
            hint = QLabel(
                f"{condition.cards_needed} more cards across "
                + ", ".join(c.grade for c in condition.coverage if not c.ok)
                + " unlocks training"
            )
            hint.setObjectName("mdHint")
            row.addWidget(hint)
        row.addStretch(1)
        section.body.addWidget(buttons)

    def _identity(self, identity) -> None:
        section = self._section("model_identity", "Card identity")
        table = QTableWidget(len(identity.components), 4)
        table.setObjectName("mdTable")
        table.setHorizontalHeaderLabels(("Component", "What it is", "State", "Worth"))
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for i, component in enumerate(identity.components):
            cells = (
                component.name,
                component.what,
                component.state,
                component.contribution,
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column in (0, 2, 3):
                    item.setFont(_mono(10.5))
                table.setItem(i, column, item)
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        _fit_table(table)
        section.body.addWidget(table)

        if identity.metrics:
            section.body.addWidget(
                _Facts(
                    [
                        (
                            "Top-1",
                            identity.metrics[0].value,
                            "of the whole stack",
                            True,
                        ),
                        (
                            "Top-5",
                            identity.metrics[1].value,
                            "the shortlist you see",
                            True,
                        ),
                        (
                            "Wrong printing",
                            str(identity.wrong_printing),
                            "same card, other version",
                            False,
                        ),
                        (
                            "Wrong card",
                            str(identity.wrong_card),
                            "needs a better encoder",
                            False,
                        ),
                        ("Benchmark", f"{identity.queries}", "labelled crops", False),
                    ]
                )
            )
        buttons = QWidget()
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        row.addWidget(_button("Re-run benchmark", self._benchmark_identity))
        row.addWidget(_button("Index the catalog", self._index_catalog))
        row.addWidget(_button("Fit photo matching", self._fit_projection))
        row.addWidget(_button("Re-index labelled crops", self._reindex))
        row.addStretch(1)
        section.body.addWidget(buttons)

    def _environment(self, env) -> None:
        section = self._section("training_env", "Training environment")
        if env.ready:
            section.body.addWidget(
                _Facts(
                    [
                        (
                            "Flavour",
                            (env.flavour or "cpu").upper(),
                            env.detail.split("·")[0].strip(),
                            env.flavour == "gpu",
                        ),
                        (
                            "Device",
                            env.gpu_name or "CPU only",
                            f"{env.gpu_vram_mb // 1024} GB" if env.gpu_vram_mb else "",
                            False,
                        ),
                        (
                            "Ultralytics",
                            env.ultralytics or "—",
                            "detector trainer",
                            False,
                        ),
                        (
                            "On disk",
                            ops.human_bytes(env.size_bytes),
                            "in your data folder",
                            False,
                        ),
                    ]
                )
            )
            path = QLabel(env.path)
            path.setObjectName("mdPath")
            path.setFont(_mono(10))
            section.body.addWidget(path)
        else:
            section.body.addWidget(
                _banner(
                    "Not set up. The detector needs it; the condition grader and "
                    "photo matching do not.",
                    None,
                    tone="calm",
                )
            )
        gpu = _gpu()
        buttons = QWidget()
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        if not env.ready:
            row.addWidget(
                _button("Set up (CPU)", lambda: self._setup_env("cpu"), primary=True)
            )
            row.addWidget(
                _button(
                    "Set up with GPU", lambda: self._setup_env("gpu"), enabled=bool(gpu)
                )
            )
        else:
            other = "cpu" if env.flavour == "gpu" else "gpu"
            row.addWidget(
                _button(
                    f"Switch to {other.upper()}",
                    lambda f=other: self._setup_env(f),
                    enabled=bool(gpu) or other == "cpu",
                )
            )
            row.addWidget(_button("Remove", self._remove_env))
        if not gpu:
            hint = QLabel("No suitable GPU detected")
            hint.setObjectName("mdHint")
            row.addWidget(hint)
        row.addStretch(1)
        section.body.addWidget(buttons)

    def _corpus(self, corpus) -> None:
        section = self._section("corpus", "Data corpus", "Data corpus")
        section.body.addWidget(
            _Facts(
                [
                    ("Captures", f"{corpus.captures:,}", "", False),
                    ("Images", f"{corpus.images:,}", "with boxes", False),
                    ("Boxes", f"{corpus.boxes:,}", "", False),
                    ("Cards named", f"{corpus.cards:,}", "", False),
                    ("Graded", f"{corpus.graded:,}", "", False),
                    ("Indexed", f"{corpus.indexed:,}", "catalog cards", False),
                ]
            )
        )
        section.body.addWidget(_eyebrow("What the corpus is feeding"))
        for feed in corpus.feeds:
            line = QWidget()
            row = QHBoxLayout(line)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(9)
            name = QLabel(feed.model)
            name.setObjectName("mdFeedName")
            name.setFixedWidth(132)
            state = QLabel(
                {"ok": "Current", "warn": "Behind", "no": "Blocked"}[feed.state]
            )
            state.setObjectName(f"mdFeed{feed.state.capitalize()}")
            detail = QLabel(feed.detail)
            detail.setObjectName("mdFeedDetail")
            row.addWidget(name)
            row.addWidget(state)
            row.addWidget(detail, 1)
            section.body.addWidget(line)

    # -- navigation ---------------------------------------------------------

    def _jump(self, row: int) -> None:
        if 0 <= row < len(self._sections):
            bar = self._scroll.verticalScrollBar()
            bar.setValue(max(0, self._sections[row].y() - 8))

    def _jump_key(self, key: str) -> None:
        for index, section in enumerate(self._sections):
            if section.key == key:
                self._nav.setCurrentRow(index)
                self._jump(index)
                return

    def _follow(self, value: int) -> None:
        current = 0
        for index, section in enumerate(self._sections):
            if section.y() - 24 <= value:
                current = index
            else:
                break
        self._nav.show_current(current)

    # -- jobs ---------------------------------------------------------------

    def _train_detector(self) -> None:
        from tcginventory.services import training

        if not training.training_env_summary().ready:
            QMessageBox.information(
                self,
                "Train the detector",
                "The training environment isn't set up yet — build it in the "
                "Training environment section below, then train.",
            )
            self._jump_key("training_env")
            return
        self._task("models.detector.train", modeljobs.train_detector)

    def _train_condition(self) -> None:
        self._task("models.condition.train", modeljobs.train_condition)

    def _benchmark_identity(self) -> None:
        self._task("identity.benchmark", modeljobs.benchmark_identity)

    def _fit_projection(self) -> None:
        self._task("identity.fit_projection", modeljobs.fit_photo_projection)

    def _reindex(self) -> None:
        self._task("vision.reindex", modeljobs.reindex_corpus)

    def _index_catalog(self) -> None:
        """Ask how much of the catalog to take on: the scopes differ by orders of
        magnitude, from nothing to most of an evening."""
        from tcginventory.services import catalog_index as ci

        counts = {}
        for scope in ci.SCOPES:
            try:
                counts[scope] = len(ci.scope_card_ids(self._conn, scope))
            except Exception:  # pragma: no cover - catalog unavailable
                counts[scope] = 0
        choices = [
            f"{ci.SCOPE_LABELS[scope]} — {counts[scope]:,} cards" for scope in ci.SCOPES
        ]
        chosen, ok = QInputDialog.getItem(
            self,
            "Index the card catalog",
            "Recognition can only name cards it has embeddings for.\n"
            "How much of the catalog should it learn?",
            choices,
            0,
            False,
        )
        if not ok:
            return
        scope = ci.SCOPES[choices.index(chosen)]
        if scope != ci.SCOPE_CACHED:
            gb = counts[scope] * 0.045 / 1000  # ~45 KB of art per card, measured
            if (
                QMessageBox.question(
                    self,
                    "Index the card catalog",
                    f"This downloads art for up to {counts[scope]:,} cards — roughly "
                    f"{gb:.1f} GB — and keeps each one so a future model can "
                    "re-learn offline.\n\nIt can be stopped and resumed; nothing is "
                    "downloaded twice. Continue?",
                )
                != QMessageBox.Yes
            ):
                return
        self._task(
            "identity.index_catalog",
            lambda conn, s=scope: modeljobs.index_catalog(conn, s),
        )

    def _setup_env(self, flavour: str) -> None:
        from tcginventory.services import training, trainingenv

        installed = (
            training.training_env_flavour()
            if training.training_env_summary().ready
            else None
        )
        if installed and installed != flavour:
            other = "GPU" if flavour == trainingenv.FLAVOUR_GPU else "CPU"
            size = (
                "about 2.5 GB" if flavour == trainingenv.FLAVOUR_GPU else "about 280 MB"
            )
            if (
                QMessageBox.question(
                    self,
                    f"Switch to the {other} environment?",
                    f"This removes the {installed.upper()} environment and builds "
                    f"the {other} one in its place, downloading {size}.\n\nTrained "
                    "models, the labelled corpus and the training history are all "
                    "kept.",
                )
                != QMessageBox.Yes
            ):
                return
        op_id = "models.training_env_gpu" if flavour == "gpu" else "models.training_env"
        self._task(op_id, lambda conn, f=flavour: modeljobs.build_training_env(conn, f))

    def _remove_env(self) -> None:
        from tcginventory.services import trainingenv

        prompt = (
            f"Remove the training environment?\n\nDeletes {trainingenv.env_dir()} "
            f"and frees {ops.human_bytes(trainingenv.env_size())}.\n\nKept: your "
            "trained models, the labelled corpus, and every training run.\n\n"
            "Setting it up again re-downloads the training stack."
        )
        if QMessageBox.question(self, "Remove the training environment", prompt) != (
            QMessageBox.Yes
        ):
            return
        self._task("models.training_env_remove", modeljobs.remove_training_env)

    # -- running them -------------------------------------------------------

    def _task(self, op_id: str, job) -> None:
        if op_id in self._workers:
            return  # already running
        db_file = self._db_file()
        if not db_file:
            QMessageBox.information(
                self, "Models", "Not available for an in-memory database."
            )
            return
        worker = TaskWorker(db_file, job)
        self._workers[op_id] = worker
        worker.done.connect(lambda payload, oid=op_id: self._finished(oid, payload))
        worker.start()
        self._running = op_id
        # Deliberately *not* a full refresh. Nothing about the models has changed
        # yet — the job has only just started — and rebuilding every section to
        # add one banner froze the window for seconds at exactly the moment the
        # user pressed a button and wanted to see it respond.
        self._show_running()

    def _finished(self, op_id: str, payload) -> None:
        self._running = None
        worker = self._workers.pop(op_id, None)
        if worker is not None:
            worker.deleteLater()
        ok, sentence = payload
        if op_id in ops.BY_ID:
            ops.record(self._conn, op_id, ok=ok, summary=sentence)
        title = ops.BY_ID[op_id].title if op_id in ops.BY_ID else "Models"

        # Report first, rebuild after. The outcome is one query against the run
        # log; the rebuild reads the whole corpus, and putting it in front meant
        # a run finished into several seconds of nothing before saying so.
        kind = _RESULT_KINDS.get(op_id)
        result = self._run_result(kind) if (ok and kind) else None
        if result is not None:
            ResultDialog(result, self).exec()
        elif not ok:
            QMessageBox.warning(self, title, sentence)
        else:
            QMessageBox.information(self, title, sentence)
        self.refresh()
        if ok:
            self.changed.emit()

    def _run_result(self, kind: str):
        """Read back what the run recorded, so the window shows the run rather
        than the sentence the job happened to return."""
        from tcginventory.data.vision import ModelRunRepository, open_vision

        vision = None
        try:
            vision = open_vision()
            return summary.run_result(ModelRunRepository(vision), kind)
        except Exception:  # pragma: no cover - vision DB unavailable
            return None
        finally:
            if vision is not None:
                vision.close()

    def _db_file(self) -> str | None:
        for _seq, name, file in self._conn.execute("PRAGMA database_list"):
            if name == "main":
                return file or None
        return None


@lru_cache(maxsize=1)
def _gpu():
    """The GPU, probed once per session.

    ``detect_gpu`` shells out to nvidia-smi, and the overview asked for it on
    every rebuild — twice, since the environment section asks too. A graphics
    card does not come and go while the window is open.
    """
    from tcginventory.services import trainingenv

    try:
        return trainingenv.detect_gpu()
    except Exception:  # pragma: no cover - probing is best-effort
        return None


def _gpu_ready() -> bool:
    from tcginventory.services import training

    return bool(_gpu()) and training.training_env_summary().ready


class ResultDialog(QDialog):
    """What a finished training run actually produced.

    A one-line toast is the wrong shape for this: a run has metrics, a
    promotion decision, and often a reason it went the way it did. Those are
    the things worth reading once, at the moment the run lands — after that
    they live in the section behind it.
    """

    def __init__(self, result, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{result.title} — {result.outcome}")
        self.setObjectName("mdDialog")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setStyleSheet(_qss(theme.active()))

        col = QVBoxLayout(self)
        col.setContentsMargins(20, 17, 20, 18)
        col.setSpacing(13)

        head = QHBoxLayout()
        head.setSpacing(9)
        title = QLabel(result.title)
        title.setObjectName("mdDialogTitle")
        head.addWidget(title)
        head.addWidget(_pill(result.outcome, result.tone))
        head.addStretch(1)
        col.addLayout(head)

        if result.headline:
            col.addWidget(
                _Facts([(m.label, m.value, "", m.good) for m in result.headline])
            )

        if result.layers:
            col.addWidget(_eyebrow("What each layer is worth"))
            col.addWidget(
                _Bars(
                    [
                        (
                            layer.label,
                            layer.top1,
                            "base" if layer.delta is None else "accent",
                            f"{layer.top1 * 100:.1f}%"
                            + (
                                ""
                                if layer.delta is None
                                else f"  {layer.delta * 100:+.1f}"
                            ),
                        )
                        for layer in result.layers
                    ]
                )
            )

        for line in result.lines:
            label = QLabel(line)
            label.setObjectName("mdDialogLine")
            label.setWordWrap(True)
            col.addWidget(label)

        if result.caveat:
            col.addWidget(_banner(result.caveat, None, tone="warn"))

        # No stretch: the dialog is sized to its content, and a stretch would
        # only open a gap above the button.
        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch(1)
        close = _button("Close", self.accept, primary=True)
        close.setDefault(True)
        footer.addWidget(close)
        col.addLayout(footer)


def _gate_reason(condition) -> str:
    """Why the grader cannot be trained yet, in one line on the button."""
    if condition.ok:
        return ""
    short = ", ".join(c.grade for c in condition.coverage if not c.ok)
    return (
        f"Needs {condition.cards_needed} more graded cards across {short} "
        f"({condition.min_cards} per grade, over {condition.min_images} photos)"
    )


def _running_banner(op_id: str) -> QFrame:
    """Say a job is under way, and roughly how long to expect.

    Training the detector runs for minutes on a worker thread. Without this the
    screen looks identical before and after the click, which reads as a button
    that does nothing.
    """
    titles = {
        "models.detector.train": (
            "Training the region detector",
            "Runs in the background — a few minutes. The screen updates when it "
            "finishes.",
        ),
        "models.condition.train": (
            "Training the condition grader",
            "Fitting over the graded cards. This is quick.",
        ),
        "identity.benchmark": (
            "Benchmarking identification",
            "Scoring every labelled crop against the whole index, layer by layer.",
        ),
        "identity.fit_projection": (
            "Fitting photo matching",
            "Reading catalog art and measuring the gap to a photograph — a few "
            "minutes.",
        ),
        "identity.index_catalog": (
            "Indexing the catalog",
            "Downloading and embedding card art. It can be stopped and resumed.",
        ),
        "vision.reindex": ("Re-indexing labelled crops", "Re-embedding the corpus."),
        "models.training_env": (
            "Building the training environment",
            "Downloading the training stack.",
        ),
        "models.training_env_gpu": (
            "Building the GPU training environment",
            "Downloading CUDA-enabled torch — this is a large download.",
        ),
        "models.training_env_remove": ("Removing the training environment", ""),
    }
    title, detail = titles.get(op_id, ("Working", ""))

    frame = QFrame()
    frame.setObjectName("mdBanner")
    frame.setProperty("tone", "busy")
    col = QVBoxLayout(frame)
    col.setContentsMargins(13, 11, 13, 12)
    col.setSpacing(7)
    label = QLabel(f"<b>{title}…</b>" + (f"  {detail}" if detail else ""))
    label.setObjectName("mdBannerText")
    label.setWordWrap(True)
    col.addWidget(label)
    bar = QProgressBar()
    bar.setObjectName("mdProgress")
    bar.setRange(0, 0)  # indeterminate: none of these report a percentage
    bar.setTextVisible(False)
    bar.setFixedHeight(5)
    col.addWidget(bar)
    return frame


def _banner(text: str, action, tone: str = "warn") -> QFrame:
    frame = QFrame()
    frame.setObjectName("mdBanner")
    frame.setProperty("tone", tone)
    row = QHBoxLayout(frame)
    row.setContentsMargins(13, 10, 13, 10)
    row.setSpacing(11)
    label = QLabel(text)
    label.setObjectName("mdBannerText")
    label.setWordWrap(True)
    row.addWidget(label, 1)
    if action is not None:
        row.addWidget(action)
    return frame


def _qss(t: theme.Tokens) -> str:
    return f"""
    #mdPage, #mdBody {{ background: {t.bg}; }}
    #mdTitle {{ font-size: 19px; font-weight: 700; color: {t.ink}; }}
    #mdSub {{ color: {t.ink2}; font-size: 12px; }}
    #mdEyebrow {{ color: {t.ink3}; font-size: 9px; font-weight: 800;
        letter-spacing: 1px; }}

    #mdNav {{ background: transparent; border: none; outline: none; }}
    #mdNav::item {{ color: {t.ink2}; padding: 6px 9px; border-radius: 7px;
        margin-bottom: 1px; }}
    #mdNav::item:hover {{ background: {t.surface2}; color: {t.ink}; }}
    #mdNav::item:selected {{ background: {t.surface2}; color: {t.ink};
        border-left: 2px solid {t.accent}; font-weight: 600; }}

    #mdSection {{ background: {t.surface}; border: 1px solid {t.line};
        border-radius: 10px; }}
    #mdSectionTitle {{ font-size: 13.5px; font-weight: 700; color: {t.ink}; }}
    #mdSectionNote {{ color: {t.ink2}; font-size: 11.5px; }}
    #mdRule {{ background: {t.line}; border: none; }}

    #mdPill {{ font-size: 9px; font-weight: 800; letter-spacing: .7px;
        padding: 3px 8px; border-radius: 9px; border: 1px solid {t.line2};
        color: {t.ink3}; }}
    #mdPill[state="served"] {{ color: {t.pos}; border-color: {t.pos};
        background: {_tint(t.pos)}; }}
    #mdPill[state="stale"] {{ color: {t.warn}; border-color: {t.warn};
        background: {_tint(t.warn)}; }}
    #mdPill[state="blocked"] {{ color: {t.neg}; border-color: {t.neg};
        background: {t.neg_soft}; }}

    #mdCard {{ background: {t.surface2}; border: 1px solid {t.line};
        border-top: 2px solid {t.line2}; border-radius: 8px; }}
    #mdCard[state="served"] {{ border-top-color: {t.pos}; }}
    #mdCard[state="stale"] {{ border-top-color: {t.warn}; }}
    #mdCard[state="blocked"] {{ border-top-color: {t.neg}; }}
    #mdCardTitle {{ font-size: 12px; font-weight: 700; color: {t.ink}; }}
    #mdCardNote {{ color: {t.ink3}; font-size: 11px; }}
    #mdCardWho {{ color: {t.ink2}; font-size: 11.5px; }}
    #mdCardFlag {{ color: {t.warn}; font-size: 11px; font-weight: 600; }}
    #mdMetricKey {{ color: {t.ink3}; font-size: 10.5px; }}
    #mdMetricValue {{ color: {t.ink}; }}
    #mdMetricGood {{ color: {t.pos}; }}
    #mdHint {{ color: {t.ink3}; font-size: 10.5px; }}
    #mdCaption {{ color: {t.ink3}; font-size: 10.5px; }}
    #mdPath {{ color: {t.ink3}; }}

    #mdBtn, #mdPrimary {{ padding: 5px 11px; border-radius: 7px; font-size: 11.5px; }}
    #mdBtn {{ background: {t.surface}; border: 1px solid {t.line2}; color: {t.ink}; }}
    #mdBtn:hover {{ border-color: {t.ink3}; }}
    #mdBtn:disabled {{ color: {t.ink3}; border-color: {t.line}; }}
    #mdPrimary {{ background: {t.accent}; border: 1px solid {t.accent};
        color: {t.accent_on}; font-weight: 700; }}
    #mdPrimary:hover {{ background: {t.accent_ink}; border-color: {t.accent_ink}; }}

    #mdChip {{ padding: 3px 10px; border-radius: 11px; font-size: 10.5px;
        background: {t.surface}; border: 1px solid {t.line2}; color: {t.ink3}; }}
    #mdChip:checked {{ background: {t.surface2}; color: {t.ink};
        border-color: {t.ink3}; font-weight: 600; }}

    #mdChartCard {{ background: {t.surface2}; border: 1px solid {t.line};
        border-radius: 8px; }}
    #mdChartTitle {{ color: {t.ink2}; font-size: 11.5px; }}

    #mdBanner {{ border-radius: 8px; border: 1px solid {t.line2};
        background: {t.surface2}; }}
    #mdBanner[tone="warn"] {{ border-color: {t.warn};
        background: {_tint(t.warn, 0.10)}; }}
    #mdDialog {{ background: {t.surface}; }}
    #mdDialogTitle {{ font-size: 16px; font-weight: 700; color: {t.ink}; }}
    #mdDialogLine {{ color: {t.ink2}; font-size: 12px; }}
    #mdBanner[tone="busy"] {{ border-color: {t.accent};
        background: {t.accent_soft}; }}
    #mdProgress {{ border: none; border-radius: 3px; background: {t.line2}; }}
    #mdProgress::chunk {{ background: {t.accent}; border-radius: 3px; }}
    #mdBannerText {{ color: {t.ink}; font-size: 11.5px; }}

    #mdFacts {{ background: {t.line}; border: 1px solid {t.line};
        border-radius: 8px; }}
    #mdFact {{ background: {t.surface2}; }}
    #mdFactValue {{ color: {t.ink}; }}
    #mdFactGood {{ color: {t.pos}; }}
    #mdFactSub {{ color: {t.ink3}; font-size: 10px; }}

    #mdBarLabel {{ color: {t.ink}; font-size: 11.5px; }}
    #mdBarValue {{ color: {t.ink2}; }}

    #mdFeedName {{ color: {t.ink2}; font-size: 11.5px; }}
    #mdFeedOk {{ color: {t.pos}; font-size: 11.5px; font-weight: 700; }}
    #mdFeedWarn {{ color: {t.warn}; font-size: 11.5px; font-weight: 700; }}
    #mdFeedNo {{ color: {t.neg}; font-size: 11.5px; font-weight: 700; }}
    #mdFeedDetail {{ color: {t.ink3}; font-size: 11.5px; }}

    #mdTable {{ background: {t.surface}; border: 1px solid {t.line};
        border-radius: 8px; gridline-color: {t.line};
        alternate-background-color: {t.zebra}; color: {t.ink2};
        font-size: 11.5px; }}
    #mdTable::item {{ padding: 5px 10px; }}
    QHeaderView::section {{ background: {t.surface2}; color: {t.ink3};
        border: none; border-bottom: 1px solid {t.line}; padding: 6px 10px;
        font-size: 9px; font-weight: 800; letter-spacing: .7px; }}
    """


def _tint(color: str, alpha: float = 0.16) -> str:
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"
