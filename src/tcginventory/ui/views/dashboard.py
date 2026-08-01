"""Dashboard view — a stylized portfolio overview ("collector's instrument").

Composition (not a flat tile row):
- a **hero** metric (Collection Value) with a delta and its own sparkline;
- a supporting **KPI cluster** (cost / market / unrealized / realized), each with
  a mini-sparkline, a sign-tinted value, a delta chip and a colored rail;
- a **signature chart** — a magenta gradient area for collection value over the
  cyan net-cash line (plus the realized line), themed to match;
- a **Highlights** strip: Top Gainers / Losers (by **market-price movement**),
  Top Sellers (units sold) and Upcoming Shows, with card-art thumbnails.

The chart's date-range selector is a **shared time filter**: changing it windows
the flow boxes (Realized, Net Cash), the movers, the sellers, and every sparkline
(the position boxes stay a current snapshot). The Card Shows calendar lives in its
own navigation destination.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from PySide6.QtCharts import (
    QAreaSeries,
    QChart,
    QDateTimeAxis,
    QLineSeries,
    QValueAxis,
)
from PySide6.QtCore import QDateTime, QObject, QSize, Qt, QThread, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QGradient,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.domain.enums import Intent
from tcginventory.services import cardimages
from tcginventory.services.cardshows import CardShowService
from tcginventory.services.reporting import (
    MoverRow,
    ReportingService,
    SellerRow,
    SeriesPoint,
)
from tcginventory.ui import icons, theme
from tcginventory.ui.charts import RangedChartView
from tcginventory.ui.formatting import money

# Date-range window (days) → the label shown on the range pills / chips.
_RANGE_LABELS = {30: "1M", 90: "3M", 180: "6M", 270: "9M", 365: "1Y", None: "All time"}

_INTENTS = ["All", "personal", "resale", "ripping"]
_PAGE = 5  # movers rows shown per "Show 5 more" step

# Chart series → token role.
_SERIES_COLORS = {
    "Collection Value": "accent",  # orange headline
    "Net cash": "info",  # cool blue (cash reads cool, not "loss red")
    "Realized Gain/Loss": "warn",  # amber
}

# Muted, warm plate colours for the set chips behind a card (placeholder art).
_SET_SWATCHES = ("#4a3a24", "#5a3320", "#3f3320", "#463b2a", "#524526", "#3a2f22")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _to_msecs(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = datetime.fromisoformat(iso[:10])
    return dt.timestamp() * 1000.0


def _tone(cents: int) -> str | None:
    if cents > 0:
        return "pos"
    if cents < 0:
        return "neg"
    return None


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _mono(size: int, weight: QFont.Weight = QFont.Normal) -> QFont:
    f = QFont(theme.mono_family())
    f.setPointSize(size)
    f.setWeight(weight)
    return f


def _vals(points: list[SeriesPoint]) -> list[float]:
    return [p.value_cents / 100.0 for p in points]


def _window_slice(points: list[SeriesPoint], cutoff: str | None) -> list[SeriesPoint]:
    """Points on/after ``cutoff`` (all points when cutoff is None = All Time)."""
    if cutoff is None:
        return points
    return [p for p in points if p.at >= cutoff]


def _series_color(role: str) -> str:
    t = theme.active()
    return {
        "accent": t.accent,
        "accent2": t.accent2,
        "info": t.info,
        "warn": t.warn,
        "series3": t.series[3],
    }.get(role, t.ink2)


def _set_color(key: str | None) -> str:
    if not key:
        return _SET_SWATCHES[0]
    return _SET_SWATCHES[sum(ord(c) for c in key) % len(_SET_SWATCHES)]


def _chip(text: str, tone: str) -> QLabel:
    """A small rounded stat chip. `tone`: 'up' | 'down' | 'mut'."""
    t = theme.active()
    color = {"up": t.pos, "down": t.neg, "mut": t.ink2}[tone]
    bg = t.surface2 if tone == "mut" else _rgba(color, 0.15)
    lbl = QLabel(text)
    lbl.setFont(_mono(8, QFont.Bold))
    lbl.setStyleSheet(
        f"color:{color};background:{bg};border-radius:9px;padding:2px 8px;"
    )
    return lbl


# attendance status → (label, token role). A reserved semantic palette
# (green / blue / amber / red) kept distinct from the brand accent.
_STATUS_STYLE = {
    "attending": ("Attending", "pos"),
    "walking": ("Walking", "info"),
    "pending": ("Pending", "warn"),
    "denied": ("Denied", "neg"),
}


def _month_day(iso: str) -> tuple[str, str]:
    try:
        d = datetime.fromisoformat(iso[:10])
    except ValueError:
        return "", "?"
    return d.strftime("%b").upper(), str(d.day)


def _status_badge(status: str) -> QLabel:
    t = theme.active()
    label, key = _STATUS_STYLE.get(status, (status.title(), "mut"))
    color = {
        "pos": t.pos,
        "info": t.info,
        "warn": t.warn,
        "neg": t.neg,
        "mut": t.ink2,
    }[key]
    lbl = QLabel(label)
    lbl.setFont(_mono(8, QFont.Bold))
    lbl.setStyleSheet(
        f"color:{color};background:{_rgba(color, 0.15)};"
        "border-radius:9px;padding:2px 8px;"
    )
    return lbl


# ---------------------------------------------------------------------------
# card-art thumbnails (cached-first, background fetch)
# ---------------------------------------------------------------------------

_THUMB_W, _THUMB_H, _THUMB_R = 24, 32, 5


def _rounded_pixmap(path: str) -> QPixmap | None:
    src = QPixmap(path)
    if src.isNull():
        return None
    scaled = src.scaled(
        _THUMB_W, _THUMB_H, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
    )
    x = max(0, (scaled.width() - _THUMB_W) // 2)
    y = max(0, (scaled.height() - _THUMB_H) // 2)
    cropped = scaled.copy(x, y, _THUMB_W, _THUMB_H)
    out = QPixmap(_THUMB_W, _THUMB_H)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(0, 0, _THUMB_W, _THUMB_H, _THUMB_R, _THUMB_R)
    p.setClipPath(clip)
    p.drawPixmap(0, 0, cropped)
    p.end()
    return out


class CardThumb(QLabel):
    """Fixed-size card-art thumbnail; a colour placeholder until the art loads."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(_THUMB_W, _THUMB_H)

    def show_placeholder(self, color: str) -> None:
        self.setPixmap(QPixmap())
        self.setStyleSheet(f"background:{color};border-radius:{_THUMB_R}px;")

    def show_image(self, path: str) -> None:
        pix = _rounded_pixmap(path)
        if pix is not None:
            self.setStyleSheet("background:transparent;")
            self.setPixmap(pix)


class _ThumbFetchWorker(QThread):
    """Fetches (and caches) card images off the UI thread."""

    loaded = Signal(str, str)  # card_id, local path

    def __init__(self, jobs: list[tuple[str, str]]) -> None:
        super().__init__()
        self._jobs = jobs

    def run(self) -> None:
        for card_id, url in self._jobs:
            path = cardimages.ensure_cached(card_id, url)
            if path is not None:
                self.loaded.emit(card_id, str(path))


class _ImageLoader(QObject):
    """Resolves inventory identities to card art — cache first, network in the
    background — and paints thumbnails in as they arrive.

    Self-driving: ``bind`` shows cached art immediately, else a placeholder and a
    queued fetch. One worker runs at a time, draining the queue (so expanding a
    movers list mid-session just adds to the queue). Results are applied to every
    live thumbnail for that card; already-deleted row widgets are skipped."""

    def __init__(self, conn, parent=None) -> None:
        super().__init__(parent)
        self._catalog = CardCatalogRepository(conn)
        self._thumbs: dict[str, list[CardThumb]] = {}
        self._queue: dict[str, str] = {}  # card_id -> url awaiting fetch
        self._worker: _ThumbFetchWorker | None = None

    def bind(
        self,
        thumb: CardThumb,
        name: str,
        set_name: str | None,
        number: str | None,
        fallback: str,
    ) -> None:
        res = self._catalog.image_for(name, set_name, number)
        if not res:
            thumb.show_placeholder(fallback)
            return
        card_id, url = res
        path = cardimages.cache_path(card_id)
        if path.exists():
            thumb.show_image(str(path))
            return
        thumb.show_placeholder(fallback)
        self._thumbs.setdefault(card_id, []).append(thumb)
        self._queue[card_id] = url
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if (self._worker is not None and self._worker.isRunning()) or not self._queue:
            return
        jobs = list(self._queue.items())
        self._queue = {}
        self._worker = _ThumbFetchWorker(jobs)
        self._worker.loaded.connect(self._on_loaded)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    def _on_worker_done(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        self._ensure_worker()  # anything queued while it ran

    def _on_loaded(self, card_id: str, path: str) -> None:
        live: list[CardThumb] = []
        for thumb in self._thumbs.get(card_id, []):
            try:
                thumb.show_image(path)
                live.append(thumb)  # keep only widgets that are still alive
            except RuntimeError:
                pass
        if live:
            self._thumbs[card_id] = live
        else:
            self._thumbs.pop(card_id, None)


# ---------------------------------------------------------------------------
# widgets
# ---------------------------------------------------------------------------


class Sparkline(QWidget):
    """A tiny inline trend line with a soft gradient fill. Purely decorative."""

    def __init__(self, color: str, fill: bool = True, parent=None) -> None:
        super().__init__(parent)
        self._vals: list[float] = []
        self._color = QColor(color)
        self._fill = fill
        self.setMinimumWidth(80)
        self.setMinimumHeight(24)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, values: list[float], color: str | None = None) -> None:
        self._vals = list(values)
        if color:
            self._color = QColor(color)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        if len(self._vals) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h, pad = self.width(), self.height(), 3.0
        lo, hi = min(self._vals), max(self._vals)
        rng = (hi - lo) or 1.0
        n = len(self._vals)
        pts = [
            (
                pad + (w - 2 * pad) * i / (n - 1),
                pad + (h - 2 * pad) * (1 - (v - lo) / rng),
            )
            for i, v in enumerate(self._vals)
        ]
        line = QPainterPath()
        line.moveTo(*pts[0])
        for x, y in pts[1:]:
            line.lineTo(x, y)
        if self._fill:
            area = QPainterPath(line)
            area.lineTo(pts[-1][0], h)
            area.lineTo(pts[0][0], h)
            area.closeSubpath()
            grad = QLinearGradient(0, 0, 0, h)
            top = QColor(self._color)
            top.setAlpha(70)
            bot = QColor(self._color)
            bot.setAlpha(0)
            grad.setColorAt(0, top)
            grad.setColorAt(1, bot)
            p.fillPath(area, grad)
        pen = QPen(self._color, 1.8)
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawPath(line)
        p.end()


class HeroCard(QFrame):
    """The headline metric: big mono figure + delta chips + sparkline."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("hero")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(2)
        self._lab = QLabel(title)
        self._lab.setObjectName("heroLab")
        self._val = QLabel("—")
        self._val.setFont(_mono(32, QFont.DemiBold))
        self._chips = QHBoxLayout()
        self._chips.setSpacing(7)
        self._chips.addStretch(1)
        self._spark = Sparkline(theme.active().accent)
        self._spark.setMinimumHeight(52)
        lay.addWidget(self._lab)
        lay.addWidget(self._val)
        lay.addLayout(self._chips)
        lay.addSpacing(6)
        lay.addWidget(self._spark)
        self._restyle()

    def _restyle(self) -> None:
        t = theme.active()
        self.setStyleSheet(
            f"QFrame#hero{{background:{t.surface};border:1px solid {t.line};"
            f"border-radius:14px;}}"
            f"QLabel#heroLab{{color:{t.ink2};font-size:12px;font-weight:600;}}"
        )
        self._val.setStyleSheet(f"color:{t.ink};")

    def set_value(
        self,
        text: str,
        delta_cents: int,
        pct: float | None,
        spark: list[float],
        window_label: str,
    ) -> None:
        self._restyle()
        self._val.setText(text)
        # rebuild chips
        while self._chips.count() > 1:  # keep the trailing stretch
            item = self._chips.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        tone = "up" if delta_cents > 0 else "down" if delta_cents < 0 else "mut"
        arrow = "▲ " if delta_cents > 0 else "▼ " if delta_cents < 0 else ""
        idx = 0
        self._chips.insertWidget(idx, _chip(f"{arrow}{money(abs(delta_cents))}", tone))
        idx += 1
        if pct is not None:
            self._chips.insertWidget(idx, _chip(f"{pct:+.1f}%", tone))
            idx += 1
        self._chips.insertWidget(idx, _chip(window_label, "mut"))
        self._spark.set_data(spark, theme.active().accent)


class KpiCard(QFrame):
    """A supporting metric: mono value + sparkline + delta chip + colored rail."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("kpi")
        self._rail = theme.active().line2
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 11, 14, 11)
        lay.setSpacing(4)
        self._lab = QLabel(title.upper())
        self._lab.setObjectName("kpiLab")
        self._val = QLabel("—")
        self._val.setFont(_mono(19, QFont.DemiBold))
        self._spark = Sparkline(self._rail)
        self._delta = QLabel("")
        self._delta.setFont(_mono(9, QFont.Bold))
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.addWidget(self._spark, 1)
        foot.addWidget(self._delta, 0, Qt.AlignRight | Qt.AlignBottom)
        lay.addWidget(self._lab)
        lay.addWidget(self._val)
        lay.addLayout(foot)
        self._restyle()

    def _restyle(self) -> None:
        t = theme.active()
        self.setStyleSheet(
            f"QFrame#kpi{{background:{t.surface};border:1px solid {t.line};"
            f"border-left:3px solid {self._rail};border-radius:12px;}}"
            f"QLabel#kpiLab{{color:{t.ink3};font-size:11px;font-weight:700;}}"
        )

    def set_value(
        self,
        text: str,
        tone: str | None,
        delta_text: str,
        spark: list[float],
        rail: str,
    ) -> None:
        t = theme.active()
        self._rail = rail
        self._restyle()
        self._val.setText(text)
        val_color = {"pos": t.pos, "neg": t.neg}.get(tone or "", t.ink)
        self._val.setStyleSheet(f"color:{val_color};")
        d_color = {"pos": t.pos, "neg": t.neg}.get(tone or "", t.ink3)
        self._delta.setText(delta_text)
        self._delta.setStyleSheet(f"color:{d_color};")
        self._spark.set_data(spark, rail)


class _PagedBox(QFrame):
    """A titled highlights card that shows _PAGE items with "Show 5 more" /
    "Collapse" paging.

    It sizes to its own content (no trailing stretch), so sibling boxes laid out
    in a top-aligned row keep **independent** heights — expanding one doesn't grow
    the others. Subclasses implement ``_render_item`` and ``_empty_text``."""

    def __init__(self, title: str, dot_color: str) -> None:
        super().__init__()
        self.setObjectName("movers")
        self._dot_color = dot_color
        self._items: list = []
        self._visible = _PAGE
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(16, 13, 16, 12)
        self._outer.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(7)
        self._dot = QLabel()
        self._dot.setFixedSize(9, 9)
        self._title = QLabel(title)
        self._title.setObjectName("moversTitle")
        head.addWidget(self._dot)
        head.addWidget(self._title)
        head.addStretch(1)
        self._outer.addLayout(head)
        self._rows = QVBoxLayout()
        self._rows.setContentsMargins(0, 4, 0, 0)
        self._rows.setSpacing(0)
        self._outer.addLayout(self._rows)
        self._footer = QHBoxLayout()
        self._footer.setContentsMargins(0, 6, 0, 0)
        self._footer.setSpacing(12)
        self._outer.addLayout(self._footer)
        self._restyle()

    # -- subclass hooks -----------------------------------------------------
    def _render_item(self, item, first: bool) -> None:  # pragma: no cover
        raise NotImplementedError

    def _empty_text(self) -> str:
        return "Nothing here."

    # -- shared machinery ---------------------------------------------------
    def set_items(self, items: list) -> None:
        self._items = items
        self._render()

    def _render(self) -> None:
        self._restyle()
        self._clear()
        self._clear_footer()
        if not self._items:
            self._empty(self._empty_text())
            return
        for i, item in enumerate(self._items[: self._visible]):
            self._render_item(item, first=(i == 0))
        self._render_footer()

    def _restyle(self) -> None:
        t = theme.active()
        self._dot.setStyleSheet(f"background:{self._dot_color};border-radius:3px;")
        self.setStyleSheet(
            f"QFrame#movers{{background:{t.surface};border:1px solid {t.line};"
            f"border-radius:14px;}}"
            f"QLabel#moversTitle{{color:{t.ink2};font-size:12px;font-weight:700;}}"
        )

    def _clear(self) -> None:
        while self._rows.count():
            item = self._rows.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _clear_footer(self) -> None:
        while self._footer.count():
            item = self._footer.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _empty(self, text: str) -> None:
        t = theme.active()
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{t.ink3};padding:10px 2px;")
        self._rows.addWidget(lbl)

    def _render_footer(self) -> None:
        remaining = len(self._items) - self._visible
        added = False
        if remaining > 0:
            self._footer.addWidget(
                self._foot_button(
                    f"Show {min(_PAGE, remaining)} more",
                    "chevron-down",
                    theme.active().accent2,
                    self._expand,
                )
            )
            added = True
        if self._visible > _PAGE:
            self._footer.addWidget(
                self._foot_button(
                    "Collapse", "chevron-up", theme.active().ink3, self._collapse
                )
            )
            added = True
        if added:
            self._footer.addStretch(1)

    def _foot_button(self, text, icon_name, color, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setIcon(icons.icon(icon_name, color, 14))
        btn.setIconSize(QSize(14, 14))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton{{border:none;background:transparent;color:{color};"
            "font-size:11px;font-weight:700;padding:2px 4px;}"
            f"QPushButton:hover{{color:{theme.active().ink};}}"
        )
        btn.clicked.connect(slot)
        return btn

    def _expand(self) -> None:
        self._visible += _PAGE
        self._render()

    def _collapse(self) -> None:
        self._visible = _PAGE
        self._render()


class MoversColumn(_PagedBox):
    """Top Gainers / Losers / Sellers rows, with card-art thumbnails."""

    def __init__(self, title: str, dot_color: str, images: _ImageLoader) -> None:
        super().__init__(title, dot_color)
        self._images = images
        self._mode = "gain"  # gain | loss | seller
        self._label = ""  # window label (sellers empty-state)

    def _render_item(self, item, first: bool) -> None:
        self._row(*self._format(item, self._mode), first=first)

    def _row(
        self,
        name: str,
        set_name: str | None,
        number: str | None,
        sub: str,
        r_top: str,
        r_bottom: str,
        r_tone: str | None,
        first: bool,
    ) -> None:
        t = theme.active()
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(2, 7, 2, 7)
        h.setSpacing(11)
        thumb = CardThumb()
        self._images.bind(thumb, name, set_name, number, _set_color(set_name or name))
        h.addWidget(thumb)
        who = QVBoxLayout()
        who.setSpacing(1)
        nm = QLabel(name)
        nm.setStyleSheet(f"color:{t.ink};font-weight:600;font-size:13px;")
        sb = QLabel(sub)
        sb.setFont(_mono(8))
        sb.setStyleSheet(f"color:{t.ink3};")
        who.addWidget(nm)
        who.addWidget(sb)
        h.addLayout(who)
        h.addStretch(1)
        right = QVBoxLayout()
        right.setSpacing(1)
        rt = QLabel(r_top)
        rt.setFont(_mono(12))
        rt.setStyleSheet(f"color:{t.ink};")
        rt.setAlignment(Qt.AlignRight)
        rb = QLabel(r_bottom)
        rb.setFont(_mono(9, QFont.Bold))
        rb_color = {"pos": t.pos, "neg": t.neg}.get(r_tone or "", t.ink3)
        rb.setStyleSheet(f"color:{rb_color};")
        rb.setAlignment(Qt.AlignRight)
        right.addWidget(rt)
        right.addWidget(rb)
        h.addLayout(right)
        row.setObjectName("mvrow")
        if not first:
            row.setStyleSheet(f"QWidget#mvrow{{border-top:1px solid {t.line};}}")
        self._rows.addWidget(row)

    def set_movers(self, rows: list[MoverRow], gain: bool) -> None:
        self._mode = "gain" if gain else "loss"
        self.set_items(rows)

    def set_sellers(self, rows: list[SellerRow], window_label: str) -> None:
        self._mode = "seller"
        self._label = window_label
        self.set_items(rows)

    @staticmethod
    def _format(item, mode: str):
        """(name, set, number, sub, right-top, right-bottom, tone) for one item."""
        sub = " · ".join(x for x in (item.set_name, item.card_number) if x) or "—"
        if mode == "seller":
            plural = "s" if item.sales != 1 else ""
            return (
                item.name,
                item.set_name,
                item.card_number,
                sub,
                f"{item.units} sold",
                f"{money(item.revenue_cents)} · {item.sales} sale{plural}",
                None,
            )
        if mode == "loss":
            # Show what was paid, so a market drop can be read against cost basis.
            paid = f"paid {money(item.cost_cents)}"
            sub = f"{sub} · {paid}" if sub != "—" else paid
        pct = f" ({item.change_pct:+.0f}%)" if item.change_pct is not None else ""
        arrow = "▲ " if item.change_cents >= 0 else "▼ "
        return (
            item.name,
            item.set_name,
            item.card_number,
            sub,
            money(item.price_cents),
            f"{arrow}{money(abs(item.change_cents))}{pct}",
            _tone(item.change_cents),
        )

    def _empty_text(self) -> str:
        if self._mode == "seller":
            return f"No sales in {self._label.lower()}."
        if self._mode == "gain":
            return "No gainers in this range."
        return "No losers here."


class UpcomingShowsBox(_PagedBox):
    """Upcoming card shows (attending/walking), paged like the movers."""

    def __init__(self) -> None:
        super().__init__("Upcoming Shows", theme.active().info)

    def set_shows(self, shows: list) -> None:
        self.set_items(shows)

    def _empty_text(self) -> str:
        return "No upcoming shows."

    def _render_item(self, show, first: bool) -> None:
        t = theme.active()
        row = QWidget()
        row.setObjectName("showrow")
        h = QHBoxLayout(row)
        h.setContentsMargins(2, 7, 2, 7)
        h.setSpacing(11)
        cal = QFrame()
        cal.setFixedSize(40, 44)
        cal.setStyleSheet(
            f"QFrame{{background:{t.surface2};border:1px solid {t.line2};"
            f"border-radius:8px;}}"
        )
        cv = QVBoxLayout(cal)
        cv.setContentsMargins(0, 4, 0, 4)
        cv.setSpacing(0)
        mon, day = _month_day(show.starts_on)
        m = QLabel(mon)
        m.setAlignment(Qt.AlignCenter)
        m.setStyleSheet(f"color:{t.accent};font-size:9px;font-weight:800;")
        d = QLabel(day)
        d.setAlignment(Qt.AlignCenter)
        d.setFont(_mono(15, QFont.Bold))
        d.setStyleSheet(f"color:{t.ink};")
        cv.addWidget(m)
        cv.addWidget(d)
        h.addWidget(cal)
        who = QVBoxLayout()
        who.setSpacing(1)
        nm = QLabel(show.name)
        nm.setStyleSheet(f"color:{t.ink};font-weight:600;font-size:13px;")
        loc = ", ".join(x for x in (show.city, show.state) if x) or "—"
        lc = QLabel(loc)
        lc.setStyleSheet(f"color:{t.ink3};font-size:11px;")
        who.addWidget(nm)
        who.addWidget(lc)
        h.addLayout(who)
        h.addStretch(1)
        if show.status:
            h.addWidget(_status_badge(show.status), 0, Qt.AlignVCenter)
        if not first:
            row.setStyleSheet(f"QWidget#showrow{{border-top:1px solid {t.line};}}")
        self._rows.addWidget(row)


def _eyebrow(text: str) -> QLabel:
    t = theme.active()
    lbl = QLabel(text.upper())
    lbl.setStyleSheet(
        f"color:{t.ink3};font-size:11px;font-weight:700;letter-spacing:2px;"
    )
    return lbl


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------


class DashboardView(QWidget):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._reporting = ReportingService(conn)
        self._shows_service = CardShowService(conn)
        self._images = _ImageLoader(conn, self)

        self._intent = QComboBox()
        self._intent.addItems(_INTENTS)
        self._intent.currentTextChanged.connect(self.refresh)

        title = QLabel("Dashboard")
        title.setObjectName("pageTitle")
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(QLabel("Intent:"))
        header.addWidget(self._intent)

        # hero + a 2×3 grid of six uniform stat boxes (left) beside the chart
        self._hero = HeroCard("Collection Value")
        self._kpis = {
            "cost": KpiCard("Cost Basis"),
            "market": KpiCard("Market Value"),
            "unrealized": KpiCard("Unrealized P/L"),
            "realized": KpiCard("Realized P/L"),
            "netcash": KpiCard("Net Cash"),
            "roi": KpiCard("Total Return"),
        }
        kpi_grid = QGridLayout()
        kpi_grid.setSpacing(10)
        for i, card in enumerate(self._kpis.values()):
            kpi_grid.addWidget(card, i // 2, i % 2)

        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(self._hero)
        left.addLayout(kpi_grid)
        left.addStretch(1)

        self._chart_view = RangedChartView(default_days=None)
        self._chart_view.setMinimumHeight(300)
        self._chart_view.setObjectName("chartCard")
        # The chart's range selector doubles as the dashboard-wide time filter.
        self._chart_view.range_changed.connect(self.refresh)

        overview = QHBoxLayout()
        overview.setSpacing(16)
        overview.addLayout(left, 10)
        overview.addWidget(self._chart_view, 13)

        # highlights strip: movers + upcoming shows
        t = theme.active()
        self._gainers = MoversColumn("Top Gainers", t.pos, self._images)
        self._losers = MoversColumn("Top Losers", t.neg, self._images)
        self._sellers = MoversColumn("Top Sellers", t.warn, self._images)
        self._shows_box = UpcomingShowsBox()
        movers = QHBoxLayout()
        movers.setSpacing(16)
        # Top-align so each box keeps its own height — expanding one doesn't
        # stretch its neighbours (they size to their own content).
        for col in (self._gainers, self._losers, self._sellers, self._shows_box):
            movers.addWidget(col, 1, Qt.AlignTop)

        self._note = QLabel("")
        self._note.setStyleSheet("color: palette(windowText);")

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(16, 14, 16, 16)
        page_layout.setSpacing(14)
        page_layout.addLayout(header)
        page_layout.addWidget(_eyebrow("Overview"))
        page_layout.addLayout(overview)
        page_layout.addWidget(self._note)
        page_layout.addWidget(_eyebrow("Highlights"))
        page_layout.addLayout(movers)
        page_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

        self.refresh()

    def _selected_intent(self) -> Intent | None:
        text = self._intent.currentText()
        return None if text == "All" else Intent(text)

    def refresh(self, *_args) -> None:
        intent = self._selected_intent()
        rpt = self._reporting
        summary = rpt.portfolio_summary(intent)
        t = theme.active()

        # selected window (shared with the chart's range selector)
        days = self._chart_view.days()
        label = _RANGE_LABELS.get(days, "All time")
        cutoff = None
        if days is not None:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        # series, sliced to the window so every sparkline reflects the range
        collection_pts = rpt.collection_value_series(intent)
        collection_w = _window_slice(collection_pts, cutoff)
        money_series = rpt.money_movement_series()
        market_spark = _vals(collection_w)
        cost_spark = _vals(_window_slice(rpt.cost_basis_series(), cutoff))
        realized_pts = _window_slice(rpt.realized_profit_series(intent), cutoff)
        realized_spark = _vals(realized_pts)
        money_spark = _vals(_window_slice(money_series, cutoff))

        # collection change over the window (drives hero + market deltas)
        if len(collection_w) >= 2 and collection_w[0].value_cents:
            base = collection_w[0].value_cents
            delta_cents = collection_w[-1].value_cents - base
            pct = 100.0 * delta_cents / base
        else:
            delta_cents, pct = 0, None

        # hero — current Collection Value; delta over the window
        self._hero.set_value(
            money(rpt.collection_value_cents(intent)),
            delta_cents,
            pct,
            market_spark,
            label,
        )

        # position boxes — current snapshot (value stays "now"); sparklines window
        self._kpis["cost"].set_value(
            money(summary.cost_basis_cents), None, "invested", cost_spark, t.ink3
        )
        self._kpis["market"].set_value(
            money(summary.market_value_cents),
            None,
            _pct_text(pct),
            market_spark,
            t.accent2,
        )
        un = summary.unrealized_pl_cents
        self._kpis["unrealized"].set_value(
            money(un),
            _tone(un),
            "unrealized",
            market_spark,
            t.pos if un >= 0 else t.neg,
        )
        roi = rpt.total_return_pct(intent)
        roi_tone = None if roi is None else ("pos" if roi >= 0 else "neg")
        self._kpis["roi"].set_value(
            _pct_text(roi),
            roi_tone,
            "return on cost",
            market_spark,
            t.pos if (roi or 0) >= 0 else t.neg,
        )

        # flow boxes — windowed to the selected range
        rz = rpt.realized_pl_cents(intent, since=cutoff)
        self._kpis["realized"].set_value(
            money(rz),
            _tone(rz),
            f"realized · {label}",
            realized_spark,
            t.pos if rz >= 0 else t.neg,
        )
        nc = rpt.net_cash_cents(since=cutoff)
        self._kpis["netcash"].set_value(
            money(nc),
            _tone(nc),
            f"cash · {label}",
            money_spark,
            t.info,
        )

        # highlights: price-movement movers + sellers + upcoming shows (windowed).
        # Fetch the full lists (limit=None) so the columns can expand on demand.
        # One combined call so the movers query isn't recomputed twice per refresh.
        gainers, losers = rpt.price_movers(intent, limit=None, since=cutoff)
        self._gainers.set_movers(gainers, gain=True)
        self._losers.set_movers(losers, gain=False)
        self._sellers.set_sellers(rpt.top_sellers(window_days=days, limit=None), label)
        self._shows_box.set_shows(self._shows_service.upcoming(limit=None))

        # chart
        self._chart_view.setChart(
            self._build_chart(
                money_series,
                rpt.realized_profit_series(intent),
                collection_pts,
            )
        )
        if not money_series:
            self._note.setText(
                "No data yet — open the Transaction tab to record your first "
                "purchase, sale, or trade."
            )
        elif not collection_pts:
            self._note.setText(
                "Collection Value line needs price history — run "
                "File ▸ Refresh prices to start recording it."
            )
        else:
            self._note.setText("")

    def _build_chart(
        self,
        money_pts: list[SeriesPoint],
        profit_pts: list[SeriesPoint],
        collection_pts: list[SeriesPoint],
    ) -> QChart:
        chart = QChart()
        chart.setTitle("Portfolio over time")

        cash_series = _line_series("Net cash", money_pts)
        profit_series = _line_series("Realized Gain/Loss", profit_pts)
        collection_series = _line_series("Collection Value", collection_pts)
        all_pts = money_pts + profit_pts + collection_pts
        if not all_pts:
            chart.setTitle("No transactions yet")
            return chart

        lines = [cash_series, profit_series]
        if collection_pts:
            lines.append(collection_series)

        # gradient area under the collection line (its own upper boundary series,
        # kept out of the chart's line list so hover/tests still see 3 lines).
        # Hold Python refs to both — an unreferenced upper series is GC'd and
        # invalidates the QAreaSeries (shiboken "already deleted").
        area = None
        area_upper = None
        if collection_pts:
            area_upper = _line_series("", collection_pts)
            area = QAreaSeries(area_upper)
            chart.addSeries(area)  # add first → sits behind the lines
        self._area_keepalive = (area, area_upper)

        for series in lines:
            chart.addSeries(series)

        axis_x = QDateTimeAxis()
        axis_x.setFormat("MMM d")
        axis_x.setTitleText("")
        chart.addAxis(axis_x, Qt.AlignBottom)
        axis_y = QValueAxis()
        axis_y.setTitleText("")
        chart.addAxis(axis_y, Qt.AlignLeft)

        plotted = ([area] if area else []) + lines
        for series in plotted:
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)

        xs = [_to_msecs(pt.at) for pt in all_pts]
        ys = [pt.value_cents / 100.0 for pt in all_pts]
        axis_x.setRange(
            QDateTime.fromMSecsSinceEpoch(int(min(xs))),
            QDateTime.fromMSecsSinceEpoch(int(max(xs))),
        )
        low, high = min(ys + [0.0]), max(ys + [0.0])
        pad = max(1.0, (high - low) * 0.1)
        axis_y.setRange(low - pad, high + pad)

        t = theme.active()
        for series in lines:
            pen = series.pen()
            pen.setWidthF(2.2 if series is collection_series else 1.8)
            pen.setColor(QColor(_series_color(_SERIES_COLORS.get(series.name(), ""))))
            series.setPen(pen)
        if area is not None:
            grad = QLinearGradient(0, 0, 0, 1)
            grad.setCoordinateMode(QGradient.ObjectBoundingMode)
            top = QColor(t.accent)
            top.setAlphaF(0.30)
            bot = QColor(t.accent)
            bot.setAlphaF(0.0)
            grad.setColorAt(0, top)
            grad.setColorAt(1, bot)
            area.setBrush(grad)
            area.setPen(QPen(Qt.NoPen))
            for m in chart.legend().markers(area):
                m.setVisible(False)  # keep the fill out of the legend
        theme.style_chart(chart)
        return chart


def _pct_text(pct: float | None) -> str:
    return f"{pct:+.1f}%" if pct is not None else "—"


def _line_series(name: str, points: list[SeriesPoint]) -> QLineSeries:
    series = QLineSeries()
    series.setName(name)
    for pt in points:
        series.append(_to_msecs(pt.at), pt.value_cents / 100.0)
    return series
