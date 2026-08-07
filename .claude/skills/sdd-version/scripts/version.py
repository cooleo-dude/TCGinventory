"""Version tool for TCGinventory.

    version.py show            what the version is, everywhere it is read from
    version.py check           non-zero if any consumer disagrees
    version.py set X.Y.Z       rewrite the single source
    version.py bump major|minor|patch

``src/tcginventory/__init__.py`` is the **only** place the version is written.
``pyproject.toml`` reads that literal through ``[tool.setuptools.dynamic]``, so
packaging metadata cannot drift from what the window title, the splash and the
frozen exe display. Before 0.7.0 it was written in two files and had said
``0.0.1`` since the first commit, through seven shipped milestones.

Stdlib only, apart from importing setuptools to resolve the dynamic version the
same way a build would. Never tags and never pushes — that is ``/sdd-release``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
SOURCE = REPO / "src" / "tcginventory" / "__init__.py"
PYPROJECT = REPO / "pyproject.toml"

# X.Y.Z only. No pre-release or build metadata: nothing in this app consumes
# them, and a scheme nobody parses is a scheme that rots.
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_ASSIGN = re.compile(r'^__version__\s*=\s*["\'](.+?)["\']', re.MULTILINE)
_STATIC_PYPROJECT = re.compile(r'^version\s*=\s*["\']', re.MULTILINE)


def read_source() -> str:
    match = _ASSIGN.search(SOURCE.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"no __version__ assignment in {SOURCE}")
    return match.group(1)


def read_metadata() -> str | None:
    """What setuptools would write into a built distribution."""
    try:
        from setuptools.config.pyprojecttoml import read_configuration

        return read_configuration(PYPROJECT)["project"].get("version")
    except Exception as exc:  # pragma: no cover - setuptools unavailable
        print(f"  (could not resolve packaging metadata: {exc})")
        return None


def read_installed() -> str | None:
    try:
        from importlib.metadata import version

        return version("tcginventory")
    except Exception:  # pragma: no cover - not installed
        return None


def _git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )
    return done.stdout.strip()


def cmd_show(args) -> int:
    source = read_source()
    print(f"  {source:<10}  src/tcginventory/__init__.py   <- source of truth")
    for label, value in (
        ("packaging metadata (setuptools)", read_metadata()),
        ("installed distribution", read_installed()),
    ):
        shown = value or "unresolved"
        flag = "" if value in (source, None) else "   <-- DISAGREES"
        print(f"  {shown:<10}  {label}{flag}")
    existing = [t for t in _git("tag", "--list", "v*").splitlines() if t]
    print(f"\n  tags: {', '.join(existing) if existing else 'none'}")
    if f"v{source}" not in existing:
        print(f"  v{source} is not tagged — /sdd-release does that, not this tool")
    return 0


def cmd_check(args) -> int:
    source = read_source()
    problems: list[str] = []

    if not SEMVER.match(source):
        problems.append(f"{source!r} is not X.Y.Z")

    metadata = read_metadata()
    if metadata is not None and metadata != source:
        problems.append(f"packaging metadata says {metadata}, source says {source}")

    installed = read_installed()
    if installed is not None and installed != source:
        problems.append(
            f"installed distribution says {installed}, source says {source} — "
            "run: pip install -e . --no-deps"
        )

    # A second literal assignment anywhere is exactly the drift 0.7.0 removed.
    for path in sorted((REPO / "src").rglob("*.py")):
        if path != SOURCE and _ASSIGN.search(path.read_text(encoding="utf-8")):
            problems.append(f"{path.relative_to(REPO)} also assigns __version__")
    if _STATIC_PYPROJECT.search(PYPROJECT.read_text(encoding="utf-8")):
        problems.append("pyproject.toml declares a static version again")

    for problem in problems:
        print(f"  FAIL  {problem}")
    if problems:
        return 1
    print(f"  ok — {source}, one source, every consumer agrees")
    return 0


def _write(new: str) -> None:
    if not SEMVER.match(new):
        raise SystemExit(f"{new!r} is not X.Y.Z")
    old = read_source()
    if old == new:
        print(f"  already {new}, nothing to do")
        return
    text = SOURCE.read_text(encoding="utf-8")
    SOURCE.write_text(
        _ASSIGN.sub(f'__version__ = "{new}"', text, count=1), encoding="utf-8"
    )
    print(f"  {old} -> {new}   {SOURCE.relative_to(REPO)}")
    print("  next: pip install -e . --no-deps    (refreshes installed metadata)")


def cmd_set(args) -> int:
    _write(args.version)
    return 0


def cmd_bump(args) -> int:
    major, minor, patch = (int(p) for p in SEMVER.match(read_source()).groups())
    if args.part == "major":
        major, minor, patch = major + 1, 0, 0
    elif args.part == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    _write(f"{major}.{minor}.{patch}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print every version consumer").set_defaults(
        func=cmd_show
    )
    sub.add_parser("check", help="non-zero if they disagree").set_defaults(
        func=cmd_check
    )
    setter = sub.add_parser("set", help="write an explicit version")
    setter.add_argument("version")
    setter.set_defaults(func=cmd_set)
    bumper = sub.add_parser("bump", help="increment one part")
    bumper.add_argument("part", choices=["major", "minor", "patch"])
    bumper.set_defaults(func=cmd_bump)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
