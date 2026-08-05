"""Find a Python on this machine and build the training environment with it.

The frozen app ships ``python312.dll`` but **no interpreter binary**, so it cannot
create a virtualenv on its own — ``python -m venv`` needs a real ``python.exe``.
What it *can* do is find one that's already installed and drive it, which turns the
whole setup into a single button:

    discover_interpreters()  →  bootstrap_steps(chosen)  →  run them  →  env_python()

The result lives in the data dir (``training-env/``), so it survives app upgrades
that replace ``_internal`` wholesale, and its path is saved as the
``training_python`` preference. When nothing suitable is found the caller shows
:data:`NO_PYTHON_HINT` — the one case no amount of engineering avoids.

Qt-free; every probe is a short subprocess so a broken install can't take the UI
down with it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from tcginventory import config
from tcginventory.services.training import TRAINING_REQUIREMENTS

# Training needs a reasonably current interpreter; the app itself requires 3.10+.
MIN_VERSION = (3, 10)
ENV_DIRNAME = "training-env"
NO_PYTHON_HINT = (
    "No suitable Python was detected on this machine. Install Python 3.10 or newer "
    "(python.org, or `winget install Python.Python.3.12`), then run this again."
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Interpreter:
    """A Python the app could build the training environment with."""

    path: str
    version: tuple[int, int] | None
    origin: str  # 'launcher' | 'registry' | 'path' | 'current'

    @property
    def label(self) -> str:
        version = ".".join(str(n) for n in self.version) if self.version else "?"
        return f"Python {version} ({self.origin})"

    @property
    def usable(self) -> bool:
        return self.version is not None and self.version >= MIN_VERSION


def env_dir() -> Path:
    """Where the training environment lives — in the data dir, not the install."""
    return config.data_dir() / ENV_DIRNAME


def env_python(directory: Path | None = None) -> Path:
    """The interpreter inside a built training environment."""
    base = directory or env_dir()
    if os.name == "nt":
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"


def is_bootstrapped(directory: Path | None = None) -> bool:
    """Whether the training environment exists (it may still lack packages)."""
    return env_python(directory).is_file()


def _probe_version(path: str) -> tuple[int, int] | None:
    """Ask an interpreter for its version — cheap, and proves it actually runs."""
    try:
        proc = subprocess.run(
            [path, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"(\d+)\.(\d+)", proc.stdout or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def _from_launcher() -> list[tuple[str, tuple[int, int] | None]]:
    """``py -0p`` lists every registered install, newest label first."""
    if os.name != "nt":
        return []
    try:
        proc = subprocess.run(
            ["py", "-0p"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in (proc.stdout or "").splitlines():
        # " -V:3.12          C:\...\python.exe"   or   "  *   C:\...\python.exe"
        match = re.search(r"(?:-V:(\d+)\.(\d+))?\s*\*?\s*(\S:\\.*python\.exe)", line)
        if not match:
            continue
        version = (
            (int(match.group(1)), int(match.group(2))) if match.group(1) else None
        )
        found.append((match.group(3).strip(), version))
    return found


def _from_registry() -> list[tuple[str, tuple[int, int] | None]]:
    """Registered CPython installs (HKCU then HKLM)."""
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return []
    found = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Python\PythonCore") as core:
                for i in range(winreg.QueryInfoKey(core)[0]):
                    tag = winreg.EnumKey(core, i)
                    try:
                        with winreg.OpenKey(core, tag + r"\InstallPath") as key:
                            base = winreg.QueryValue(key, "")
                    except OSError:
                        continue
                    exe = Path(base) / "python.exe"
                    match = re.match(r"(\d+)\.(\d+)", tag)
                    version = (
                        (int(match.group(1)), int(match.group(2))) if match else None
                    )
                    if exe.is_file():
                        found.append((str(exe), version))
        except OSError:
            continue
    return found


def discover_interpreters() -> list[Interpreter]:
    """Every usable Python we can find, best candidate first.

    Real installations rank above virtualenvs (a venv's base is what we actually
    want), and newer versions above older. Duplicates collapse by resolved path.
    """
    candidates: list[Interpreter] = []
    for path, version in _from_launcher():
        candidates.append(Interpreter(path, version, "launcher"))
    for path, version in _from_registry():
        candidates.append(Interpreter(path, version, "registry"))
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(Interpreter(found, None, "path"))
    if not getattr(sys, "frozen", False):
        candidates.append(Interpreter(sys.executable, sys.version_info[:2], "current"))

    seen: set[str] = set()
    resolved: list[Interpreter] = []
    for cand in candidates:
        try:
            key = str(Path(cand.path).resolve()).lower()
        except OSError:  # pragma: no cover - unresolvable path
            key = cand.path.lower()
        if key in seen:
            continue
        seen.add(key)
        version = cand.version or _probe_version(cand.path)
        if version is None or version < MIN_VERSION:
            continue
        resolved.append(Interpreter(cand.path, version, cand.origin))

    def rank(item: Interpreter):
        in_venv = "venv" in item.path.lower() or "virtualenv" in item.path.lower()
        return (not in_venv, item.version or (0, 0))

    return sorted(resolved, key=rank, reverse=True)


def bootstrap_steps(
    interpreter: Interpreter | str, directory: Path | None = None
) -> list[tuple[str, list[str]]]:
    """The commands that build the training environment, as (label, argv) pairs.

    Two steps, both long enough to deserve a progress line of their own: create
    the virtualenv, then install the training stack (~280 MB) into it.
    """
    base = str(getattr(interpreter, "path", interpreter))
    target = directory or env_dir()
    return [
        ("Creating the training environment…", [base, "-m", "venv", str(target)]),
        (
            "Installing ultralytics and its dependencies (~280 MB)…",
            [
                str(env_python(target)),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *TRAINING_REQUIREMENTS,
            ],
        ),
    ]
