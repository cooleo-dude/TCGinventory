"""Application configuration: data-directory / DB-path resolution and logging.

Kept free of any Qt or SQL dependency so it can be imported and tested headless.
Paths are OS-appropriate but overridable via environment variables (used by dev
and tests):

- ``TCGINVENTORY_DATA_DIR`` — override the whole app-data directory.
- ``TCGINVENTORY_DB`` — override the database file path directly.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

APP_NAME = "TCGinventory"
DB_FILENAME = "inventory.db"
PREFS_FILENAME = "preferences.json"

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


def prefs_path() -> Path:
    """Path to the JSON preferences file (honours the data-dir override)."""
    override = os.environ.get(_ENV_DB)
    if override:  # keep prefs beside an overridden DB (tests / custom locations)
        return Path(override).parent / PREFS_FILENAME
    return data_dir() / PREFS_FILENAME


def read_prefs() -> dict:
    """Read the JSON preferences (``{}`` if missing/unreadable).

    Plain JSON (not QSettings) so the headless background task can read it
    without importing Qt.
    """
    try:
        return json.loads(prefs_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def write_prefs(prefs: dict) -> None:
    """Persist the JSON preferences, creating the data dir if needed."""
    ensure_data_dir()
    prefs_path().write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def get_pref(key: str, default=None):
    return read_prefs().get(key, default)


def set_pref(key: str, value) -> None:
    prefs = read_prefs()
    prefs[key] = value
    write_prefs(prefs)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging once, idempotently."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
