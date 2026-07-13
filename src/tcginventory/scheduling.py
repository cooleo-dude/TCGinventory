"""Background price-update scheduling via the Windows Task Scheduler.

The GUI only refreshes prices when it's open. To keep price history filling in
continuously, we register a daily Windows Scheduled Task that runs the app's
headless ``--refresh-prices`` entry point (no display needed). Qt-free so it can
be imported and unit-tested; the actual ``schtasks`` calls are guarded to
Windows.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK_NAME = "TCGinventory Daily Price Update"
DEFAULT_TIME = "09:00"


def is_supported() -> bool:
    """True on Windows, where the Task Scheduler (`schtasks`) is available."""
    import os

    return os.name == "nt"


def refresh_command() -> str:
    """The command line that performs one headless refresh.

    Frozen (PyInstaller) → the app exe with the flag. From source → the current
    Python running ``-m tcginventory``. Paths are quoted for ``schtasks /TR``.
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return f'"{exe}" --refresh-prices'
    py = Path(sys.executable)
    return f'"{py}" -m tcginventory --refresh-prices'


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def is_enabled() -> bool:
    """Whether the daily update task is currently registered."""
    if not is_supported():
        return False
    result = _run(["schtasks", "/Query", "/TN", TASK_NAME])
    return result.returncode == 0


def enable(time: str = DEFAULT_TIME) -> None:
    """Register (or replace) the daily refresh task at ``time`` (HH:MM, 24h).

    Raises :class:`RuntimeError` with schtasks' message on failure.
    """
    if not is_supported():
        raise RuntimeError("Automatic updates are only supported on Windows.")
    result = _run(
        [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            refresh_command(),
            "/SC",
            "DAILY",
            "/ST",
            time,
            "/F",  # overwrite if it already exists
        ]
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip())


def disable() -> None:
    """Remove the daily refresh task (no error if it doesn't exist)."""
    if not is_supported():
        return
    _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
