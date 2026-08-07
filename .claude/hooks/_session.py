"""Session lease registry — who owns which checkout right now.

Several Claude sessions work this repo at once and a checkout has exactly one
`HEAD`. This is the small piece of shared state that lets them tell each other
apart: one JSON file per live session, recording the worktree it is working in and
the branch it was last seen on.

    _session.claim(repo, session, pid)      take or refresh a lease
    _session.leases(repo)                   every live lease, stale ones reaped
    _session.owning_session(repo)           the session holding this worktree
    _session.rivals(repo, session)          other live sessions in this worktree
    _session.release(repo, session)         give it up

**Where it lives matters.** The registry sits in the *git common directory* — the
one `.git` that every worktree of a repository shares. `.claude/context/` would
not do: each worktree has its own checkout of it, so two sessions would each see
an empty registry and conclude they were alone, which is the exact failure this
exists to prevent. The common dir is also never committed and never synced.

Consumers: `branch_lease.py` (the guard) and `review_gate.py` (verdict
attribution). Nothing here imports either, so the registry stays a leaf.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

# A lease this old is presumed dead. Long enough to survive a lunch break, short
# enough that a session killed at 6pm does not wedge the repo overnight. Every
# guarded tool call refreshes the heartbeat, so a working session never expires.
STALE_AFTER = 8 * 60 * 60

# Tests must never touch the real registry: the suite runs `-n auto --dist
# loadfile`, so a test writing to the live `.git/tcgi-sessions/` would fight both
# the other workers and whichever session is running it.
REGISTRY_ENV = "TCGI_SESSION_REGISTRY"


def registry_dir(repo: Path) -> Path:
    """`<git-common-dir>/tcgi-sessions/`, or the test override."""
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return Path(override)
    done = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    common = done.stdout.strip() if done.returncode == 0 else ""
    if not common:
        # Not a git repo, or git is unavailable. Keep the registry beside the tree
        # rather than raising — a guard that cannot read its registry must still
        # let work happen.
        return repo / ".git" / "tcgi-sessions"
    # Relative in the main clone (`.git`), absolute from a linked worktree.
    root = Path(common)
    return (root if root.is_absolute() else repo / root) / "tcgi-sessions"


def _path(repo: Path, session: str) -> Path:
    # A session id is opaque and comes from the harness; keep it to characters that
    # are safe as a filename rather than trusting it.
    safe = "".join(c for c in session if c.isalnum() or c in "-_")[:64] or "unknown"
    return registry_dir(repo) / f"{safe}.json"


def worktree_root(repo: Path) -> str:
    """A comparable identity for a checkout.

    `resolve()` matters on Windows: the same worktree reached through a short
    (8.3) path, a different drive-letter case or a junction would otherwise read as
    two different trees, and two "different" trees never look like rivals.
    """
    return str(Path(repo).resolve()).lower()


def branch_of(repo: Path) -> str:
    done = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() or "unknown"


def claim(repo: Path, session: str, pid: int | None = None) -> dict:
    """Take or refresh this session's lease on `repo`. Idempotent."""
    directory = registry_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    path = _path(repo, session)

    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    now = time.time()
    lease = {
        "session": session,
        "worktree": worktree_root(repo),
        "branch": branch_of(repo),
        "pid": pid if pid is not None else os.getpid(),
        "started": existing.get("started", now),
        "heartbeat": now,
        # Worktrees isolate code, not the app's SQLite files. A session that boots
        # the app should point it here so two sessions cannot write one ledger.
        "data_dir": str(Path(repo).resolve() / ".claude" / "session-data"),
    }
    _write(path, lease)
    return lease


def _write(path: Path, lease: dict) -> None:
    """Atomic-ish replace, so a reader never sees a half-written lease."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(lease, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def leases(repo: Path, reap: bool = True) -> list[dict]:
    """Every live lease. Stale ones are deleted on the way past."""
    directory = registry_dir(repo)
    if not directory.is_dir():
        return []
    live: list[dict] = []
    cutoff = time.time() - STALE_AFTER
    for path in sorted(directory.glob("*.json")):
        try:
            lease = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue  # a torn or unreadable lease is not evidence of an owner
        if float(lease.get("heartbeat", 0)) < cutoff:
            if reap:
                path.unlink(missing_ok=True)
            continue
        live.append(lease)
    return live


def rivals(repo: Path, session: str | None) -> list[dict]:
    """Other live sessions working in **this same worktree**.

    The only condition under which sharing hurts. A session alone in its own
    worktree can switch branches and `git add -A` freely: nobody else's files are
    there to sweep up and nobody else's `HEAD` moves.
    """
    here = worktree_root(repo)
    return [
        lease
        for lease in leases(repo)
        if lease.get("worktree") == here and lease.get("session") != session
    ]


def holders(repo: Path) -> list[str]:
    """Every live session with a lease on this worktree."""
    here = worktree_root(repo)
    return [
        lease.get("session", "?")
        for lease in leases(repo)
        if lease.get("worktree") == here
    ]


def owning_session(repo: Path) -> str | None:
    """The session holding this worktree, if exactly one does.

    `None` covers two very different situations, and the caller **must** tell them
    apart with `holders()`:

    * **nobody** holds a lease — the registry is not running, so verdicts cannot be
      attributed and the gate has to fall back to its older, weaker guarantee;
    * **several** sessions hold it — ambiguity is not ownership, and treating that
      as "unattributed" would make a verdict admissible to every one of them. That
      is precisely the hole ticket 007 exists to close, so it must fail *closed*.
    """
    owners = holders(repo)
    return owners[0] if len(owners) == 1 else None


def release(repo: Path, session: str) -> bool:
    path = _path(repo, session)
    if path.is_file():
        path.unlink()
        return True
    return False


def release_all(repo: Path) -> int:
    """The escape hatch. A guard with no manual override gets disabled wholesale
    at the first false positive, so this is deliberate and documented."""
    directory = registry_dir(repo)
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.glob("*.json"):
        path.unlink(missing_ok=True)
        removed += 1
    return removed
