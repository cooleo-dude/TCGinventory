"""Application entry point: wire up config, logging, and launch the GUI."""

from __future__ import annotations

import logging
import sys

from tcginventory import config


def main() -> int:
    """Launch the TCGinventory desktop app. Returns the Qt exit code."""
    config.configure_logging()
    log = logging.getLogger(__name__)

    data_dir = config.ensure_data_dir()
    log.info("Data directory: %s", data_dir)
    log.info("Database path: %s", config.db_path())

    # Imported lazily so headless tooling (tests, tools) can import this module
    # without requiring a Qt display or the PySide6 dependency at import time.
    from PySide6.QtWidgets import QApplication

    from tcginventory.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
