"""Application configuration: data-directory / DB-path resolution and logging.

Kept free of any Qt or SQL dependency so it can be imported and tested headless.
Paths are OS-appropriate but overridable via environment variables (used by dev
and tests):

- ``TCGINVENTORY_DATA_DIR`` — override the whole app-data directory.
- ``TCGINVENTORY_DB`` — override the database file path directly.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

APP_NAME = "TCGinventory"
DB_FILENAME = "inventory.db"

_ENV_DATA_DIR = "TCGINVENTORY_DATA_DIR"
_ENV_DB = "TCGINVENTORY_DB"


def default_data_dir() -> Path:
    """Return the OS-appropriate application-data directory (not yet created)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    # Linux / other: honour XDG_DATA_HOME, else ~/.local/share
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def data_dir() -> Path:
    """Resolve the app-data directory, honouring the env override."""
    override = os.environ.get(_ENV_DATA_DIR)
    return Path(override) if override else default_data_dir()


def db_path() -> Path:
    """Resolve the SQLite database file path, honouring env overrides."""
    override = os.environ.get(_ENV_DB)
    return Path(override) if override else data_dir() / DB_FILENAME


def ensure_data_dir() -> Path:
    """Create the app-data directory if needed and return it."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging once, idempotently."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
