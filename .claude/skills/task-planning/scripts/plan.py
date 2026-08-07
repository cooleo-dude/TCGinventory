"""Render the grounded dossier for a TCGinventory task-planning request.

The `/task-planning` skill is not allowed to describe a window from memory: it
runs this script, and the script is the only thing that turns a free-form
request into file paths, services, tables and test commands. Everything it
prints comes from `.claude/context/app-map.json` and is checked against the
working tree first, so a stale map fails loudly instead of quietly producing a
confident, wrong brief.

Usage
-----
    python .claude/skills/task-planning/scripts/plan.py brief "<request>"
    python .claude/skills/task-planning/scripts/plan.py validate
    python .claude/skills/task-planning/scripts/plan.py windows

Exit codes: 0 ok · 1 map is stale · 2 window could not be resolved.
Stdlib only, so it runs under any interpreter on PATH — no venv required.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MAP_REL = Path(".claude/context/app-map.json")
SEPARATORS = re.compile(r"\s*(?:—|–|--|-|:|—)\s+")
DEPTHS = ("quick", "standard", "deep")
# Any of these in the request means the money/schema rules are in play.
MONEY_WORDS = re.compile(
    r"\b(money|cash|cent|price|pricing|cost|basis|p/?l|profit|loss|margin|"
    r"value|valuation|fifo|lot|intent|allocat|realiz|unrealiz|tax)\w*",
    re.I,
)
SCHEMA_WORDS = re.compile(
    r"\b(schema|migrat|column|table|field|persist|store|database|db|index)\w*",
    re.I,
)
# Path-shaped tokens inside the map, verified against the tree.
PATH_TOKEN = re.compile(r"[A-Za-z0-9_./-]+\.(?:py|sql|md|json|onnx|spec)\b")


# --------------------------------------------------------------------------
# loading + validation
# --------------------------------------------------------------------------


def find_root(start: Path) -> Path:
    for d in (start, *start.parents):
        if (d / MAP_REL).exists():
            return d
    raise SystemExit(f"error: no {MAP_REL} found at or above {start}")


def load(root: Path) -> dict:
    try:
        return json.loads((root / MAP_REL).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {MAP_REL} is not valid JSON — {exc}") from exc


def exists(root: Path, token: str) -> bool:
    """A token resolves if it exists at the root or under the package."""
    return any((root / base / token).exists() for base in ("", "src/tcginventory"))


def rel(root: Path, token: str) -> str:
    """Normalise a map token to a repo-relative path the agent can Read.

    The map writes some paths package-relative (`services/pricing.py`) because
    that is how they read in prose. An agent needs the real one.
    """
    token = token.strip()
    if (root / token).exists():
        return token
    if (root / "src/tcginventory" / token).exists():
        return f"src/tcginventory/{token}"
    return token


def walk_strings(node, trail: str = ""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk_strings(v, f"{trail}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk_strings(v, f"{trail}[{i}]")
    elif isinstance(node, str):
        yield trail, node


def validate(root: Path, data: dict) -> list[str]:
    """Return a list of drift complaints. Empty means the map matches the tree."""
    problems: list[str] = []

    seen: set[str] = set()
    for trail, text in walk_strings(data):
        for token in PATH_TOKEN.findall(text):
            if token in seen or "*" in token:
                continue
            seen.add(token)
            if not exists(root, token):
                problems.append(f"path does not exist: {token}  (at {trail})")

    # The nav list is the app's real ordering; the map must not drift from it.
    mw = root / "src/tcginventory/ui/main_window.py"
    if mw.exists():
        src = mw.read_text(encoding="utf-8")
        match = re.search(r"NAV_ITEMS\s*=\s*\[(.*?)\]", src, re.S)
        if match:
            nav = re.findall(r'"([^"]+)"', match.group(1))
            if nav != data.get("main_window", {}).get("nav_order"):
                problems.append(
                    "main_window.nav_order disagrees with NAV_ITEMS in "
                    f"main_window.py: {nav}"
                )
            labels = sorted(w["nav_label"] for w in data["windows"].values())
            if labels != sorted(nav):
                missing = sorted(set(nav) - set(labels))
                extra = sorted(set(labels) - set(nav))
                problems.append(
                    f"windows do not cover the nav (missing={missing}, extra={extra})"
                )
    else:
        problems.append("src/tcginventory/ui/main_window.py is missing")

    for key, win in data["windows"].items():
        path = root / win["file"]
        if path.exists():
            if f"class {win['class']}" not in path.read_text(encoding="utf-8"):
                problems.append(f"{key}: class {win['class']} not in {win['file']}")
        else:
            problems.append(f"{key}: primary file missing — {win['file']}")

    return problems


# --------------------------------------------------------------------------
# request parsing
# --------------------------------------------------------------------------


def split_request(request: str) -> tuple[str, str, str]:
    """Split free-form input into (head, task, depth)."""
    depth = "standard"
    body = request.strip()
    if "|" in body:
        body, _, tail = body.rpartition("|")
        found = [d for d in DEPTHS if d in tail.lower()]
        if found:
            depth = found[0]
        else:  # not a depth flag after all — put it back
            body = f"{body}|{tail}"
    body = body.strip()
    parts = SEPARATORS.split(body, maxsplit=1)
    if len(parts) == 2 and parts[0]:
        return parts[0].strip(), parts[1].strip(), depth
    return body, body, depth


def resolve_window(head: str, whole: str, data: dict) -> tuple[str | None, list[str]]:
    """Score every window against the request; return (key, ambiguous_keys)."""
    scores: dict[str, int] = {}
    head_l = head.lower()
    whole_l = whole.lower()
    for key, win in data["windows"].items():
        names = {key, key.replace("_", " "), win["nav_label"].lower()}
        names.update(a.lower() for a in win.get("aliases", []))
        best = 0
        for name in names:
            if not name:
                continue
            pattern = re.compile(rf"\b{re.escape(name)}\b")
            if pattern.search(head_l):
                best = max(best, 100 + len(name))
            elif pattern.search(whole_l):
                best = max(best, 40 + len(name))
        if best:
            scores[key] = best
    if not scores:
        return None, []
    top = max(scores.values())
    winners = sorted(k for k, v in scores.items() if v == top)
    if len(winners) > 1:
        return None, winners
    return winners[0], []


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def bullets(title: str, items, empty: str = "_none_") -> list[str]:
    items = [i for i in (items or [])]
    if not items:
        return [f"**{title}:** {empty}"]
    return [f"**{title}:**"] + [f"- {i}" for i in items]


def reading_list(root: Path, data: dict, win: dict, depth: str, task: str):
    """(path, why, ok) triples the agent must read, ordered by priority."""
    out: list[tuple[str, str]] = []
    money = bool(MONEY_WORDS.search(task))
    schema = bool(SCHEMA_WORDS.search(task))

    if money or schema:
        out.append(
            ("specs/02-data-model.md", "task touches money or schema — read first")
        )
    out.append((win["file"], "the window itself"))
    for spec in win.get("specs", []):
        path = spec.split(" ")[0]
        if path.endswith(".md"):
            out.append((path, "spec this window implements"))
    out.append(
        (
            "docs/decisions.md",
            "ADR log, newest first — check nothing decided this already",
        )
    )

    if depth in ("standard", "deep"):
        for svc in win.get("services", []):
            out.append((svc.split(" ")[0], "service this window calls"))
        for test in win.get("tests", [])[: 4 if depth == "standard" else 99]:
            out.append((test, "existing coverage — the shape new tests should follow"))
    if depth == "deep":
        for extra in win.get("supporting_files", []):
            out.append((extra.split(" ")[0], "shared machinery this window builds on"))
        if win.get("tables"):
            out.append(
                ("src/tcginventory/data/repositories.py", "the tables listed above")
            )
        out.append(("CLAUDE.md", "milestone status + non-negotiable rules"))

    seen, rows = set(), []
    for path, why in out:
        if "*" in path:  # a glob (bundled models, migrations) is not a read
            continue
        norm = rel(root, path)
        if norm in seen:
            continue
        seen.add(norm)
        rows.append((norm, why, exists(root, norm)))
    return rows


def next_migration(root: Path) -> str:
    d = root / "src/tcginventory/data/migrations"
    if not d.is_dir():
        return "unknown"
    nums = [
        int(m.group(1)) for f in d.glob("*.sql") if (m := re.match(r"(\d+)", f.name))
    ]
    return f"{max(nums) + 1:04d}" if nums else "0001"


def render(root: Path, data: dict, key: str, task: str, depth: str, head: str) -> str:
    win = data["windows"][key]
    cmds = data["commands"]
    lines: list[str] = []
    add = lines.append

    add(f"# Dossier — {win['nav_label']}")
    add("")
    add(
        f"_Resolved `{head}` → window `{key}` · depth `{depth}` · "
        f"source `{MAP_REL.as_posix()}` @ {data['captured_at']}_"
    )
    add("")
    add("## The app")
    app = data["app"]
    add(app["one_liner"])
    add("")
    add(app["shape"])
    add("")
    add(f"**Status:** {app['status']}")
    add("")
    add("**Rules that always bind:**")
    for rule in data["architecture"]["non_negotiable_rules"]:
        add(f"- {rule}")
    add("")

    add(f"## The window — {win['nav_label']}")
    add("")
    add(win["purpose"])
    add("")
    primary = root / win["file"]
    n = len(primary.read_text(encoding="utf-8").splitlines()) if primary.exists() else 0
    add(f"**Class:** `{win['class']}` · **File:** `{win['file']}` ({n} lines)")
    add("")
    services = [
        rel(root, s.split(" ")[0]) + s[len(s.split(" ")[0]) :]
        for s in win.get("services", [])
    ]
    lines.extend(bullets("Services", services))
    add("")
    lines.extend(bullets("Repositories", win.get("repositories")))
    add("")
    lines.extend(bullets("Tables (inventory.db)", win.get("tables")))
    add("")
    if win.get("other_dbs"):
        lines.extend(bullets("Other databases", win["other_dbs"]))
        add("")
    supporting = [rel(root, s.split(" ")[0]) for s in win.get("supporting_files", [])]
    lines.extend(bullets("Supporting files", supporting))
    add("")
    lines.extend(bullets("Emits", win.get("signals_out"), "nothing — read-only screen"))
    add("")
    lines.extend(
        bullets("Driven from elsewhere by", win.get("entry_points_in"), "nothing")
    )
    add("")
    add("### Gotchas that bind work here")
    for g in win.get("gotchas", []):
        add(f"- {g}")
    add("")

    add("## Required reads")
    add("")
    add("| # | Path | Why | On disk |")
    add("|---|------|-----|---------|")
    for i, (path, why, ok) in enumerate(reading_list(root, data, win, depth, task), 1):
        add(f"| {i} | `{path}` | {why} | {'yes' if ok else '**MISSING**'} |")
    add("")

    add("## Shared machinery — reuse, do not reinvent")
    for name, block in data["shared_ui"].items():
        if isinstance(block, dict) and "rule" in block:
            add(f"- **{name}** — {block['rule']}")
    add("")

    add("## MainWindow contract")
    for item in data["main_window"]["contract"]:
        add(f"- {item}")
    add("")

    add("## Verification")
    add("")
    tests = " ".join(win.get("tests", []))
    runner = cmds["test_one"].split(" tests/")[0]
    offscreen = '$env:QT_QPA_PLATFORM="offscreen";'
    add("```")
    add(f"{offscreen} {runner} {tests} -q")
    add(cmds["format"])
    add(cmds["lint"])
    add(f"{offscreen} {cmds['test_all']}")
    add("```")
    add("")
    add(f"Run the app: `{cmds['run_app_windows']}`  ·  {cmds['shell_note']}")
    add("")

    if SCHEMA_WORDS.search(task):
        add(
            f"> **Schema flag:** the wording is persistence-adjacent. Decide "
            f"explicitly whether this needs stored state or is derived at read "
            f"time — prefer derived. If it does need a column, migrations are "
            f"forward-only and the next file is "
            f"`src/tcginventory/data/migrations/{next_migration(root)}_<name>.sql`; "
            f"never edit a shipped one."
        )
        add("")
    if MONEY_WORDS.search(task):
        add(
            "> **Money flag:** this task touches value. Integer cents only, the UI "
            "does no arithmetic, and the invariants in "
            "`specs/02-data-model.md#invariants-must-always-hold` must hold after "
            "the change."
        )
        add("")

    add("## Definition of done")
    for step in data["workflow"]["definition_of_done"]:
        add(f"- {step}")
    add("")
    add(f"_Branch policy: {data['workflow']['branch']}_")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def cmd_windows(data: dict) -> int:
    print("Windows in the map (nav label · key · aliases):\n")
    for key, win in data["windows"].items():
        aliases = ", ".join(win.get("aliases", [])) or "—"
        print(f"  {win['nav_label']:<14} {key:<14} {aliases}")
    return 0


def main(argv: list[str]) -> int:
    # The map is full of em dashes and middots; a Windows console pipe defaults
    # to cp1252 and would raise rather than print them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    brief = sub.add_parser("brief", help="render the dossier for a request")
    brief.add_argument("request", nargs="+")
    sub.add_parser("validate", help="check the map against the working tree")
    sub.add_parser("windows", help="list windows and their aliases")
    args = parser.parse_args(argv)

    root = find_root(Path.cwd().resolve())
    data = load(root)

    if args.cmd == "windows":
        return cmd_windows(data)

    problems = validate(root, data)
    if problems:
        print(
            "MAP IS STALE — the code has moved and the map has not.\n", file=sys.stderr
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\nFix .claude/context/app-map.json (the code wins) before planning.",
            file=sys.stderr,
        )
        return 1

    if args.cmd == "validate":
        wins = len(data["windows"])
        print(f"map ok — {wins} windows, every path resolves, nav order matches")
        return 0

    request = " ".join(args.request)
    head, task, depth = split_request(request)
    key, ambiguous = resolve_window(head, request, data)
    if key is None:
        if ambiguous:
            print(
                f"ambiguous window — could be: {', '.join(ambiguous)}", file=sys.stderr
            )
        else:
            print(f"could not resolve a window from: {request!r}", file=sys.stderr)
        print("", file=sys.stderr)
        cmd_windows(data)
        return 2

    print(render(root, data, key, task, depth, head))
    print("")
    print(f"<!-- map_ok: true · window: {key} · depth: {depth} · task: {task} -->")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
