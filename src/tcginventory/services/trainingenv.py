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

# Which flavour of the stack an environment holds. The difference is torch: the
# default wheels on Windows are CPU-only, and a CUDA build has to be asked for by
# index. Everything else in the environment is identical.
FLAVOUR_CPU = "cpu"
FLAVOUR_GPU = "gpu"

# CUDA wheels are forward compatible with newer drivers, so one channel covers
# every card recent enough to be worth training on.
CUDA_CHANNEL = "cu124"
CUDA_INDEX = f"https://download.pytorch.org/whl/{CUDA_CHANNEL}"
MIN_VRAM_MB = 4096  # below this a fine-tune will thrash rather than run
NO_PYTHON_HINT = (
    "No suitable Python was detected on this machine. Install Python 3.10 or newer "
    "(python.org, or `winget install Python.Python.3.12`), then run this again."
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Gpu:
    """A CUDA device this machine could train on."""

    name: str
    vram_mb: int
    driver: str
    compute: str

    @property
    def usable(self) -> bool:
        return self.vram_mb >= MIN_VRAM_MB

    @property
    def label(self) -> str:
        return f"{self.name} · {self.vram_mb / 1024:.0f} GB"


def detect_gpu() -> Gpu | None:
    """The best CUDA device ``nvidia-smi`` reports, or None.

    ``nvidia-smi`` ships with the NVIDIA driver, so its presence is also the
    evidence that a driver is installed — which is what actually matters, since a
    card with no driver cannot be trained on. AMD and Intel GPUs are not detected
    on purpose: torch's Windows wheels are CUDA-only.
    """
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    best: Gpu | None = None
    for line in (proc.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            vram = int(float(parts[1]))
        except ValueError:
            continue
        found = Gpu(parts[0], vram, parts[2], parts[3])
        if best is None or found.vram_mb > best.vram_mb:
            best = found
    return best


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
        version = (int(match.group(1)), int(match.group(2))) if match.group(1) else None
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


# path -> (fingerprint, bytes). See _size_fingerprint for what invalidates it.
_size_cache: dict[str, tuple[tuple[tuple[str, ...], ...], int]] = {}


def _walk_size(base: Path) -> int:
    """Sum the file sizes under ``base`` with one syscall per directory.

    ``rglob`` + ``is_file`` + ``is_symlink`` + ``stat`` cost four stat calls per
    path — 116k of them for a training environment's ~37k files, which took
    close to four seconds. ``scandir`` answers all of that from the data the
    directory enumeration already returned: same total, about a tenth the time.
    """
    total = 0
    stack = [str(base)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:  # vanished mid-walk, or unreadable
                        continue
        except OSError:
            continue
    return total


def _site_packages(base: Path) -> list[Path]:
    """Where packages land inside the environment — one cheap directory read."""
    if os.name == "nt":
        return [base / "Lib" / "site-packages"]
    try:
        return sorted((base / "lib").glob("python*/site-packages"))
    except OSError:
        return []


def _size_fingerprint(base: Path) -> tuple[tuple[str, ...], ...]:
    """What the environment looks like from the outside, cheaply.

    Directory *mtimes* are no use for this on Windows: they're updated lazily,
    so writing a file into ``site-packages`` can leave its mtime untouched — a
    cache keyed on them reports a stale size forever. The listing is always
    accurate, and it moves whenever packages are installed or removed, a torch
    CPU↔GPU swap included, since the version sits in the ``.dist-info`` name.
    Two directory reads (~0.1ms) to decide whether to walk ~37k files.
    """
    listings = []
    for path in (base, *_site_packages(base)):
        try:
            listings.append(tuple(sorted(os.listdir(path))))
        except OSError:
            listings.append(())
    return tuple(listings)


def env_size(directory: Path | None = None, *, recount: bool = False) -> int:
    """Bytes on disk under the training environment (0 when there isn't one).

    Cached, because the Operations screen asks on every refresh and the answer
    costs a walk of ~37k files. The cache re-checks a fingerprint of the
    environment first, so an install or a rebuild is picked up on its own; pass
    ``recount=True`` to force the walk regardless.
    """
    base = Path(directory or env_dir())
    key = str(base)
    if not base.is_dir():
        _size_cache.pop(key, None)  # a removed environment can't be stale
        return 0
    fingerprint = _size_fingerprint(base)
    cached = _size_cache.get(key)
    if recount or cached is None or cached[0] != fingerprint:
        _size_cache[key] = (fingerprint, _walk_size(base))
    return _size_cache[key][1]


def forget_env_size(directory: Path | None = None) -> None:
    """Drop the cached size after something changed the environment on disk."""
    if directory is None:
        _size_cache.clear()
    else:
        _size_cache.pop(str(Path(directory)), None)


def remove_env(directory: Path | None = None) -> int:
    """Delete the training environment, returning the bytes reclaimed.

    **Models are never touched.** Trained artefacts live in
    ``<data>/vision/models`` and the corpus in ``vision.db``; this only removes
    ``<data>/training-env``, which is nothing but an interpreter and its
    packages, and is rebuilt by running setup again.

    The recorded ``training_python`` preference is cleared only when it pointed
    *into* the environment being removed — a user who chose their own interpreter
    keeps that choice.
    """
    from tcginventory.services import training

    base = Path(directory or env_dir()).resolve()
    if base.name != ENV_DIRNAME or not base.is_dir():
        raise RuntimeError(f"Not a training environment: {base}")

    reclaimed = env_size(base)
    shutil.rmtree(base)
    forget_env_size(base)

    recorded = training.training_python()
    if recorded:
        try:
            inside = Path(recorded).resolve().is_relative_to(base)
        except (OSError, ValueError):
            inside = False
        if inside:
            training.forget_training_env()
    return reclaimed


def bootstrap_steps(
    interpreter: Interpreter | str,
    directory: Path | None = None,
    flavour: str = FLAVOUR_CPU,
) -> list[tuple[str, list[str]]]:
    """The commands that build the training environment, as (label, argv) pairs.

    For the GPU flavour the CUDA build of torch is installed **first**, from
    PyTorch's own index; installing ultralytics first would pull the CPU wheels and
    then find its dependency already satisfied, leaving a CPU environment wearing a
    GPU label.
    """
    base = str(getattr(interpreter, "path", interpreter))
    target = directory or env_dir()
    python = str(env_python(target))
    steps = [("Creating the training environment…", [base, "-m", "venv", str(target)])]
    if flavour == FLAVOUR_GPU:
        steps.append(
            (
                f"Installing CUDA torch ({CUDA_CHANNEL}) — this is the big one…",
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--index-url",
                    CUDA_INDEX,
                    "torch",
                    "torchvision",
                ],
            )
        )
    steps.append(
        (
            "Installing ultralytics and its dependencies…",
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *TRAINING_REQUIREMENTS,
            ],
        )
    )
    return steps


def probe_flavour(python: str | Path) -> tuple[str, str]:
    """Ask an environment what it actually got: (flavour, a line to show).

    Reports what torch *can do*, not what was asked for — an install can silently
    fall back to CPU wheels, and a GPU environment that cannot see the card is a
    CPU environment with extra steps.
    """
    script = (
        "import torch;"
        "print(torch.__version__, torch.version.cuda,"
        " torch.cuda.is_available(),"
        " torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')"
    )
    try:
        proc = subprocess.run(
            [str(python), "-c", script],
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return FLAVOUR_CPU, f"could not ask torch: {exc}"
    if proc.returncode != 0:
        return FLAVOUR_CPU, "torch is not installed in this environment"
    parts = (proc.stdout or "").strip().split(maxsplit=3)
    if len(parts) < 3:  # pragma: no cover - unexpected torch output
        return FLAVOUR_CPU, "torch reported nothing useful"
    version, cuda, available = parts[0], parts[1], parts[2] == "True"
    device = parts[3] if len(parts) > 3 else "-"
    if available:
        return FLAVOUR_GPU, f"torch {version} · CUDA {cuda} · {device}"
    return FLAVOUR_CPU, f"torch {version} · CPU only"
