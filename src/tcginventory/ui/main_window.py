"""The application's main window.

M0 scaffolding: a blank shell with the ratified navigation rail
(Dashboard · Transaction · Inventory · History) and a status bar. Views are
filled in from M3 onward.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from tcginventory import __version__, config

NAV_ITEMS = ["Dashboard", "Transaction", "Inventory", "History"]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"TCGinventory {__version__}")
        self.resize(1100, 720)

        nav = QListWidget()
        nav.addItems(NAV_ITEMS)
        nav.setMaximumWidth(180)

        self._stack = QStackedWidget()
        for name in NAV_ITEMS:
            placeholder = QLabel(f"{name} — coming soon")
            placeholder.setContentsMargins(24, 24, 24, 24)
            self._stack.addWidget(placeholder)

        nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        nav.setCurrentRow(0)

        splitter = QSplitter()
        splitter.addWidget(nav)
        splitter.addWidget(self._stack)
        splitter.setStretchFactor(1, 1)

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.setCentralWidget(container)

        status = QStatusBar()
        status.showMessage(f"DB: {config.db_path()}")
        self.setStatusBar(status)
