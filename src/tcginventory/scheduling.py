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


_enabled_cache: bool | None = None


def is_enabled(*, recheck: bool = False) -> bool:
    """Whether the daily update task is currently registered.

    Cached for the life of the process. The Operations screen asks on every
    refresh and each answer costs a ``schtasks`` subprocess — 23ms of a 64ms
    refresh. :func:`enable` and :func:`disable` update the cache, so every change
    this app makes is reflected at once.

    The tradeoff: a task removed *outside* the app, in Task Scheduler, is not
    noticed until the next launch or a ``recheck=True`` call. Registering the
    task is a one-time action taken from this screen, so a stale read is both
    unlikely and cheap; a wrong 23ms on every refresh is neither.
    """
    global _enabled_cache
    if not is_supported():
        return False
    if _enabled_cache is None or recheck:
        _enabled_cache = _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0
    return _enabled_cache


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
    global _enabled_cache
    _enabled_cache = True


def disable() -> None:
    """Remove the daily refresh task (no error if it doesn't exist)."""
    if not is_supported():
        return
    _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    global _enabled_cache
    _enabled_cache = False
