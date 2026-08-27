"""Ember Duo — the application's visual theme (colors, fonts, Qt styling).

Single source of truth for the look of the app. The design direction is
"grounded graphite chrome with an orange + brick-red pop" (a Scrafty palette):
neutral grey/black surfaces that stay calm for hours of dense tables, with the
warm accent confined to *punctuation* — the active nav item, primary buttons,
the chart, status pills. ``info``/``warn`` are a reserved status palette
(blue/amber) kept separate from the brand accent.

Two token sets (``DARK`` default, ``LIGHT``) are selected by the OS colour
scheme and applied via **Fusion style + QPalette + one app-level stylesheet**.
Nothing here does money math or SQL; it is pure presentation.

Views that need a raw colour (e.g. to tint a chart series or a gain/loss value
by sign) read the currently-applied set via :func:`active`.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tokens:
    """A complete colour set for one mode. Values are ``#rrggbb`` strings."""

    mode: str
    bg: str  # window ground
    surface: str  # panels, lists, tables
    surface2: str  # raised cards, inputs, buttons
    line: str  # hairline separators / grid
    line2: str  # stronger borders
    zebra: str  # alternating table rows
    ink: str  # primary text
    ink2: str  # secondary text
    ink3: str  # muted text / labels
    accent: str  # orange — the headline accent
    accent2: str  # brick red — the secondary accent
    accent_soft: str  # translucent accent wash (selection, active nav)
    accent_ink: str  # accent used as text (hover / on-surface)
    accent_on: str  # text/glyph on an accent fill
    pos: str  # gains
    neg: str  # losses (also a "Denied" show)
    neg_soft: str  # translucent loss wash
    info: str  # reserved status colour — "walking" show / cool secondary (blue)
    warn: str  # reserved status colour — "pending" show (amber)
    series: tuple[str, ...]  # chart categorical order


# Ember Duo · Dark (default) — graphite ground, orange + brick-red pop (Scrafty)
DARK = Tokens(
    mode="dark",
    bg="#121319",
    surface="#181a22",
    surface2="#20222d",
    line="#252834",
    line2="#373b4a",
    zebra="#16171f",
    ink="#eceaf2",
    ink2="#aab0c0",
    ink3="#6d7386",
    accent="#f5852e",
    accent2="#e0533a",
    accent_soft="rgba(245,133,46,0.15)",
    accent_ink="#ff9a4d",
    accent_on="#0e0f13",
    pos="#83d15a",
    neg="#ff6a5a",
    neg_soft="rgba(255,106,90,0.15)",
    info="#5b93a8",
    warn="#f6c233",
    series=("#f5852e", "#5b93a8", "#f6c233", "#83d15a", "#e0533a", "#b0784a"),
)

# Ember Duo · Light (OS auto-selects; warm cream, accents deepened for white)
LIGHT = Tokens(
    mode="light",
    bg="#f6f4f0",
    surface="#ffffff",
    surface2="#efece7",
    line="#e6e2db",
    line2="#d3ccc0",
    zebra="#faf8f4",
    ink="#1b1712",
    ink2="#574f44",
    ink3="#8a8072",
    accent="#d1611f",
    accent2="#c0392b",
    accent_soft="rgba(209,97,31,0.12)",
    accent_ink="#a94e18",
    accent_on="#ffffff",
    pos="#4f8f2f",
    neg="#c53a2a",
    neg_soft="rgba(197,58,42,0.10)",
    info="#3d7a92",
    warn="#b9821a",
    series=("#d1611f", "#3d7a92", "#b9821a", "#4f8f2f", "#c0392b", "#8a5a2a"),
)

# The currently-applied set (defaults to DARK so headless views/tests that never
# call apply_theme still get sensible colours from active()).
_ACTIVE: Tokens = DARK
_UI_FAMILY = "IBM Plex Sans"
_MONO_FAMILY = "IBM Plex Mono"


def active() -> Tokens:
    """The token set currently applied to the app (DARK until apply_theme runs)."""
    return _ACTIVE


def mono_family() -> str:
    """Family name for the monospace face (SKUs / IDs), with fallback."""
    return _MONO_FAMILY


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------


def _load_fonts() -> tuple[str, str]:
    """Register the bundled IBM Plex faces; return (ui_family, mono_family).

    Falls back to a clean system stack if a face fails to load, so the app is
    never left with an unstyled default.
    """
    ui_ok = mono_ok = False
    fonts = resources.files("tcginventory.ui") / "assets" / "fonts"
    wanted = {
        "IBMPlexSans-Variable.ttf": "IBM Plex Sans",
        "IBMPlexMono-Regular.ttf": "IBM Plex Mono",
        "IBMPlexMono-Medium.ttf": "IBM Plex Mono",
    }
    for name, family in wanted.items():
        try:
            data = (fonts / name).read_bytes()
        except (FileNotFoundError, OSError):
            continue
        fid = QFontDatabase.addApplicationFontFromData(data)
        if fid == -1:
            continue
        families = QFontDatabase.applicationFontFamilies(fid)
        if family in families:
            if family == "IBM Plex Sans":
                ui_ok = True
            else:
                mono_ok = True
    ui = "IBM Plex Sans" if ui_ok else "Segoe UI"
    mono = "IBM Plex Mono" if mono_ok else "Consolas"
    return ui, mono


# ---------------------------------------------------------------------------
# Palette + stylesheet
# ---------------------------------------------------------------------------


def _build_palette(t: Tokens) -> QPalette:
    p = QPalette()
    c = QColor
    p.setColor(QPalette.Window, c(t.bg))
    p.setColor(QPalette.WindowText, c(t.ink))
    p.setColor(QPalette.Base, c(t.surface))
    p.setColor(QPalette.AlternateBase, c(t.zebra))
    p.setColor(QPalette.Text, c(t.ink))
    p.setColor(QPalette.Button, c(t.surface2))
    p.setColor(QPalette.ButtonText, c(t.ink))
    p.setColor(QPalette.BrightText, c(t.neg))
    p.setColor(QPalette.ToolTipBase, c(t.surface2))
    p.setColor(QPalette.ToolTipText, c(t.ink))
    p.setColor(QPalette.PlaceholderText, c(t.ink3))
    p.setColor(QPalette.Highlight, c(t.accent))
    p.setColor(QPalette.HighlightedText, c(t.accent_on))
    p.setColor(QPalette.Link, c(t.accent_ink))
    p.setColor(QPalette.LinkVisited, c(t.accent))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, c(t.ink3))
    return p


def _stylesheet(t: Tokens) -> str:
    """The app-level QSS, built from the active tokens."""
    return f"""
    /* ---- base chrome ------------------------------------------------- */
    QMainWindow, QDialog {{ background-color: {t.bg}; }}
    QToolTip {{
        background-color: {t.surface2}; color: {t.ink};
        border: 1px solid {t.line2}; border-radius: 6px; padding: 4px 6px;
    }}
    QSplitter::handle {{ background-color: {t.line}; }}

    /* ---- left navigation --------------------------------------------- */
    QListWidget#nav {{
        background-color: {t.surface}; border: none;
        border-right: 1px solid {t.line}; padding: 8px 6px; outline: none;
    }}
    QListWidget#nav::item {{
        padding: 9px 12px; margin: 2px 4px; border-radius: 8px; color: {t.ink2};
    }}
    QListWidget#nav::item:hover {{ background-color: {t.surface2}; color: {t.ink}; }}
    QListWidget#nav::item:selected {{
        background-color: {t.accent_soft}; color: {t.accent_ink};
        border-left: 2px solid {t.accent};
    }}

    /* ---- buttons ----------------------------------------------------- */
    QPushButton {{
        background-color: {t.surface2}; color: {t.ink};
        border: 1px solid {t.line2}; border-radius: 8px; padding: 6px 14px;
    }}
    QPushButton:hover {{ border-color: {t.accent}; }}
    QPushButton:pressed {{ background-color: {t.line}; }}
    QPushButton:checked {{
        background-color: {t.accent_soft}; border-color: {t.accent};
        color: {t.accent_ink};
    }}
    QPushButton:disabled {{ color: {t.ink3}; border-color: {t.line}; }}
    QPushButton#primary {{
        background-color: {t.accent}; color: {t.accent_on};
        border: none; font-weight: 600;
    }}
    QPushButton#primary:hover {{ background-color: {t.accent_ink}; }}

    /* ---- inputs ------------------------------------------------------ */
    QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox, QDateEdit,
    QPlainTextEdit, QTextEdit {{
        background-color: {t.surface2}; color: {t.ink};
        border: 1px solid {t.line2}; border-radius: 8px; padding: 5px 8px;
        selection-background-color: {t.accent}; selection-color: {t.accent_on};
    }}
    QComboBox:hover, QLineEdit:hover {{ border-color: {t.accent}; }}
    QComboBox:focus, QLineEdit:focus, QSpinBox:focus,
    QDoubleSpinBox:focus, QDateEdit:focus {{ border-color: {t.accent}; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background-color: {t.surface}; color: {t.ink};
        border: 1px solid {t.line2}; outline: none;
        selection-background-color: {t.accent_soft};
        selection-color: {t.accent_ink};
    }}

    /* ---- checkboxes --------------------------------------------------- */
    /* Unstyled, Qt's indicator all but vanishes on a dark palette — an empty
       box the same colour as the panel behind it. Checked reads as an accent
       fill rather than a glyph, so no image asset is needed. */
    QCheckBox {{ color: {t.ink}; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 14px; height: 14px; border-radius: 4px;
        border: 1px solid {t.line2}; background-color: {t.surface};
    }}
    QCheckBox::indicator:hover {{ border-color: {t.accent}; }}
    QCheckBox::indicator:checked {{
        background-color: {t.accent}; border-color: {t.accent};
    }}
    QCheckBox:disabled {{ color: {t.ink3}; }}
    QCheckBox::indicator:disabled {{
        border-color: {t.line}; background-color: {t.surface2};
    }}

    /* ---- tables ------------------------------------------------------ */
    QTableView, QTableWidget, QTreeView {{
        background-color: {t.surface}; alternate-background-color: {t.zebra};
        gridline-color: {t.line}; border: 1px solid {t.line};
        border-radius: 8px; outline: none;
        selection-background-color: {t.accent_soft}; selection-color: {t.ink};
    }}
    QHeaderView::section {{
        background-color: {t.surface2}; color: {t.ink3};
        padding: 6px 8px; border: none; border-bottom: 1px solid {t.line2};
        font-weight: 600;
    }}
    QTableCornerButton::section {{
        background-color: {t.surface2}; border: none;
    }}

    /* ---- scrollbars -------------------------------------------------- */
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{
        background: {t.line2}; border-radius: 5px; min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {t.ink3}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
    QScrollBar::handle:horizontal {{
        background: {t.line2}; border-radius: 5px; min-width: 24px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: {t.ink3}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* ---- menus & status bar ------------------------------------------ */
    QMenuBar {{ background-color: {t.bg}; color: {t.ink};
        border-bottom: 1px solid {t.line}; }}
    QMenuBar::item {{ padding: 6px 10px; background: transparent; }}
    QMenuBar::item:selected {{ background-color: {t.surface2}; border-radius: 6px; }}
    QMenu {{ background-color: {t.surface}; color: {t.ink};
        border: 1px solid {t.line2}; padding: 4px; }}
    QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
    QMenu::item:selected {{ background-color: {t.accent_soft};
        color: {t.accent_ink}; }}
    QMenu::separator {{ height: 1px; background: {t.line}; margin: 4px 8px; }}
    QStatusBar {{ background-color: {t.surface}; color: {t.ink2};
        border-top: 1px solid {t.line}; }}
    QStatusBar::item {{ border: none; }}

    /* ---- dashboard --------------------------------------------------- */
    QLabel#pageTitle {{ font-size: 20px; font-weight: 700; color: {t.ink}; }}
    QFrame#tile {{
        background-color: {t.surface2}; border: 1px solid {t.line};
        border-radius: 12px;
    }}
    QLabel#tileTitle {{ color: {t.ink3}; font-size: 11px; font-weight: 600; }}
    QLabel#tileValue {{ color: {t.ink}; font-size: 20px; font-weight: 600; }}
    """


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def active_tokens(app=None) -> Tokens:
    """Choose DARK/LIGHT from the OS colour scheme (defaults to DARK)."""
    from PySide6.QtWidgets import QApplication

    app = app or QApplication.instance()
    try:
        if app is not None and app.styleHints().colorScheme() == Qt.ColorScheme.Light:
            return LIGHT
    except (AttributeError, RuntimeError):
        pass
    return DARK


def _reapply(app) -> None:
    global _ACTIVE
    _ACTIVE = active_tokens(app)
    app.setPalette(_build_palette(_ACTIVE))
    app.setStyleSheet(_stylesheet(_ACTIVE))


def apply_theme(app) -> None:
    """Apply the Neon Slate theme to the QApplication.

    Sets the Fusion base style, registers and applies the IBM Plex font, and
    installs the palette + stylesheet for the OS colour scheme. Re-applies
    automatically if the OS switches between light and dark.
    """
    global _UI_FAMILY, _MONO_FAMILY
    app.setStyle("Fusion")
    _UI_FAMILY, _MONO_FAMILY = _load_fonts()
    app.setFont(QFont(_UI_FAMILY, 10))
    _reapply(app)
    try:  # live OS light/dark switch (Qt 6.5+)
        app.styleHints().colorSchemeChanged.connect(lambda _s: _reapply(app))
    except (AttributeError, RuntimeError):
        pass


def style_chart(chart, t: Tokens | None = None) -> None:
    """Theme a :class:`QChart` to match: recessive grid, muted axis text, a
    surface-coloured plate. Series colours are assigned by the caller (they know
    each series' role)."""
    from PySide6.QtGui import QPen

    t = t or active()
    chart.setBackgroundBrush(QColor(t.surface))
    chart.setBackgroundPen(QPen(Qt.NoPen))
    chart.setPlotAreaBackgroundVisible(False)
    chart.setTitleBrush(QColor(t.ink2))
    title_font = chart.titleFont()
    title_font.setPointSizeF(10.5)
    title_font.setBold(True)
    chart.setTitleFont(title_font)
    legend = chart.legend()
    legend.setLabelColor(QColor(t.ink2))
    for axis in chart.axes():
        axis.setLabelsColor(QColor(t.ink3))
        axis.setTitleBrush(QColor(t.ink3))
        line_pen = axis.linePen()
        line_pen.setColor(QColor(t.line2))
        axis.setLinePen(line_pen)
        grid_pen = axis.gridLinePen()
        grid_pen.setColor(QColor(t.line))
        grid_pen.setWidthF(1.0)
        axis.setGridLinePen(grid_pen)
