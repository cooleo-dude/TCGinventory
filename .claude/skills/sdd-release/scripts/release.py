"""Release tool for TCGinventory.

    release.py check              preconditions: clean tree, version sane, tag free
    release.py notes              changelog entries since the last tag
    release.py build              build the exe from a clean worktree, then boot it
    release.py publish [--attach] tag, write CHANGELOG.md, create the GitHub release

A release is a claim that a particular commit is worth naming. Everything here
exists to make that claim checkable: the build comes from a **clean worktree at
the tag**, never the working tree, and the result is booted before it is
published.

Building from the working tree is the specific mistake this prevents. On
2026-08-07 the tree held another session's half-finished `history.py`, which
would have produced an exe that crashed constructing `MainWindow`.

Stdlib only. Needs `gh` authenticated for `publish`.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
VERSION_TOOL = REPO / ".claude" / "skills" / "sdd-version" / "scripts" / "version.py"
CHANGELOG = REPO / "CHANGELOG.md"
GH_FALLBACK = Path(r"C:\Program Files\GitHub CLI\gh.exe")
PYTHON = sys.executable


def git(*args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )
    if check and done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}\n{done.stderr.strip()}")
    return done.stdout.strip()


def gh(*args: str, check: bool = True, cwd: Path | None = None) -> str:
    exe = "gh"
    try:
        subprocess.run([exe, "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        if not GH_FALLBACK.is_file():
            raise SystemExit(
                "gh not found — install it, or open a fresh shell"
            ) from None
        exe = str(GH_FALLBACK)
    done = subprocess.run(
        [exe, *args], capture_output=True, text=True, cwd=cwd or REPO, check=False
    )
    if check and done.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)}\n{done.stderr.strip()}")
    return done.stdout.strip()


def version() -> str:
    out = subprocess.run(
        [PYTHON, str(VERSION_TOOL), "show"],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"^\s*(\d+\.\d+\.\d+)\s", out.stdout, re.MULTILINE)
    if not match:
        raise SystemExit("could not read the version — run /sdd-version show")
    return match.group(1)


def last_tag() -> str | None:
    tags = [
        t for t in git("tag", "--list", "v*", "--sort=-v:refname").splitlines() if t
    ]
    return tags[0] if tags else None


# ---- check -----------------------------------------------------------------


def leases_note() -> str:
    """Say *whose* uncommitted work is blocking the release, if it is not yours.

    A dirty tree is a correct reason to refuse — but on 2026-08-07 the dirt was
    another session's half-finished `history.py`, and `check` could only report the
    filenames. Naming the session turns "clean this up" into "wait, or ask them".

    Deliberately not a `--ignore-foreign` escape hatch: skipping the check is how
    that half-finished file would have shipped in the first place.
    """
    lease_tool = REPO / ".claude" / "hooks" / "branch_lease.py"
    if not lease_tool.is_file():
        return ""
    done = subprocess.run(
        [PYTHON, str(lease_tool), "show"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    if done.returncode != 0 or len(lines) < 3:
        return ""  # no registry, or nobody holds a lease: nothing to add
    return "\n      sessions holding a lease:\n      " + "\n      ".join(
        line.strip() for line in lines[2:]
    )


def cmd_check(args) -> int:
    problems: list[str] = []
    ver = version()
    print(f"  version   {ver}")

    dirty = git("status", "--porcelain")
    if dirty:
        problems.append(
            "working tree is not clean:\n      "
            + "\n      ".join(dirty.splitlines()[:8])
            + leases_note()
        )

    consistency = subprocess.run(
        [PYTHON, str(VERSION_TOOL), "check"], capture_output=True, text=True
    )
    if consistency.returncode != 0:
        problems.append(f"version check failed:\n      {consistency.stdout.strip()}")

    if f"v{ver}" in git("tag", "--list", f"v{ver}"):
        problems.append(f"v{ver} is already tagged — bump before releasing again")

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    print(f"  branch    {branch}")
    ahead = git("rev-list", "--count", "@{u}..HEAD", check=False)
    if ahead and ahead != "0":
        problems.append(f"{ahead} commit(s) not pushed")

    previous = last_tag()
    print(f"  since     {previous or 'the first commit'}")
    span = f"{previous}..HEAD" if previous else "HEAD"
    print(f"  commits   {git('rev-list', '--count', span)}")

    for problem in problems:
        print(f"  FAIL  {problem}")
    if problems:
        return 1
    print(f"\n  ok — v{ver} is releasable from {branch}")
    return 0


# ---- notes -----------------------------------------------------------------


def collect_notes() -> tuple[str, list[str]]:
    ver = version()
    previous = last_tag()
    span = f"{previous}..HEAD" if previous else "HEAD"
    subjects = [s for s in git("log", "--format=%s", span).splitlines() if s]
    # Merge commits restate their branch; the branch's own commits are already here.
    return ver, [s for s in subjects if not s.startswith("Merge ")]


def cmd_notes(args) -> int:
    ver, subjects = collect_notes()
    print(render_notes(ver, subjects))
    return 0


LIST_LIMIT = 30  # above this, summarise by area instead of listing every subject
AREA_LIMIT = 12  # areas shown before the rest are rolled into one line


def render_notes(ver: str, subjects: list[str]) -> str:
    """Release notes a person will actually read.

    Listing every commit is right for a normal release and useless for the first
    one: v0.7.0 covers 314 commits, and a wall of them says less than a count
    plus the areas they touched. Above `LIST_LIMIT`, group by the `Area:` prefix
    the commit subjects already use.
    """
    today = git("log", "-1", "--format=%cs")
    lines = [f"## v{ver} — {today}", ""]

    if not subjects:
        lines.append("- no changes recorded")
    elif len(subjects) <= LIST_LIMIT:
        lines += [f"- {s}" for s in subjects]
    else:
        areas: dict[str, int] = {}
        for subject in subjects:
            area = subject.split(":", 1)[0].strip() if ":" in subject else "Other"
            # A prefix that is really a sentence is not an area.
            if len(area) > 28 or " " in area.strip() and area.count(" ") > 3:
                area = "Other"
            areas[area] = areas.get(area, 0) + 1
        ranked = sorted(areas.items(), key=lambda kv: (-kv[1], kv[0]))
        # A tail of one-commit areas is noise: 90 lines that say less than a
        # count. Show the areas that carried the release, roll up the rest.
        top = [(a, n) for a, n in ranked if a != "Other"][:AREA_LIMIT]
        shown = {a for a, _ in top}
        rest = sum(n for a, n in ranked if a not in shown)
        lines.append(f"**{len(subjects)} commits.** By area:")
        lines.append("")
        for area, count in top:
            lines.append(f"- {area} — {count}")
        if rest:
            lines.append(f"- everything else — {rest}")
        lines += ["", f"Most recent {min(10, len(subjects))}:", ""]
        lines += [f"- {s}" for s in subjects[:10]]

    lines += [
        "",
        "See `docs/decisions.md` for why each of these went the way it did, and "
        "`CLAUDE.md` for the milestone status.",
    ]
    return "\n".join(lines)


# ---- build -----------------------------------------------------------------


def cmd_build(args) -> int:
    """Build from a throwaway worktree at `ref`, then boot what came out."""
    ref = args.ref or "HEAD"
    ver = version()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tree = Path(tmp) / "build"
        print(f"  worktree  {ref} -> {tree}")
        git("worktree", "add", "--detach", "--quiet", str(tree), ref)
        try:
            spec = tree / "packaging" / "tcginventory.spec"
            print("  building  (pyinstaller, a few minutes)")
            done = subprocess.run(
                [
                    str(Path(PYTHON).with_name("pyinstaller.exe")),
                    "--clean",
                    "--noconfirm",
                    str(spec),
                ],
                cwd=tree,
                capture_output=True,
                text=True,
                check=False,
            )
            if done.returncode != 0:
                print(done.stdout[-2000:])
                raise SystemExit("  FAIL  pyinstaller")

            built = tree / "dist" / "TCGinventory"
            size = sum(f.stat().st_size for f in built.rglob("*") if f.is_file())
            print(f"  built     {size / 1e6:.0f} MB  {built.name}")

            if not args.no_boot:
                if not boot_check(built):
                    raise SystemExit("  FAIL  the built exe did not boot")

            out = REPO / "dist" / "TCGinventory"
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.exists():
                shutil.rmtree(out, ignore_errors=True)
            shutil.move(str(built), str(out))
            print(f"  installed {out}")
            if args.zip:
                archive = REPO / "dist" / f"TCGinventory-{ver}-windows"
                shutil.make_archive(str(archive), "zip", out.parent, out.name)
                print(f"  zipped    {archive}.zip")
        finally:
            git("worktree", "remove", "--force", str(tree), check=False)
            git("worktree", "prune", check=False)
    return 0


def boot_check(bundle: Path) -> bool:
    """Launch the exe against a throwaway data dir and confirm it comes up."""
    exe = bundle / "TCGinventory.exe"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data:
        env = {**os.environ, "TCGINVENTORY_DATA_DIR": data}
        print("  booting   (isolated data dir)")
        proc = subprocess.Popen([str(exe)], env=env)
        try:
            time.sleep(25)
            alive = proc.poll() is None
            created = sorted(p.name for p in Path(data).glob("*.db"))
            print(f"  {'up' if alive else 'EXITED':<9} databases: {created or 'none'}")
            # Migrations ran if the ledger exists; the GUI is up if it is alive.
            return alive and "inventory.db" in created
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()


# ---- publish ---------------------------------------------------------------


def cmd_publish(args) -> int:
    ver, subjects = collect_notes()
    tag = f"v{ver}"
    notes = render_notes(ver, subjects)

    if git("tag", "--list", tag):
        print(f"  {tag} already exists")
    else:
        git("tag", "-a", tag, "-m", f"TCGinventory {ver}")
        git("push", "origin", tag)
        print(f"  tagged    {tag}")

    existing = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.is_file() else ""
    if f"## v{ver} " not in existing:
        header = "# Changelog\n\nNewest first. Generated by `/sdd-release`.\n\n"
        body = existing[len(header) :] if existing.startswith(header) else existing
        CHANGELOG.write_text(header + notes + "\n\n" + body, encoding="utf-8")
        print(f"  changelog {CHANGELOG.name} updated")

    assets = []
    if args.attach:
        archive = REPO / "dist" / f"TCGinventory-{ver}-windows.zip"
        if not archive.is_file():
            raise SystemExit(f"  no {archive.name} — run: release.py build --zip")
        assets = [str(archive)]

    cmd = ["release", "create", tag, "--title", f"TCGinventory {ver}", "--notes", notes]
    print(gh(*cmd, *assets))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("notes").set_defaults(func=cmd_notes)
    builder = sub.add_parser("build")
    builder.add_argument("--ref", default=None, help="what to build (default HEAD)")
    builder.add_argument("--zip", action="store_true")
    builder.add_argument("--no-boot", action="store_true")
    builder.set_defaults(func=cmd_build)
    publisher = sub.add_parser("publish")
    publisher.add_argument("--attach", action="store_true", help="upload the zip")
    publisher.set_defaults(func=cmd_publish)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
