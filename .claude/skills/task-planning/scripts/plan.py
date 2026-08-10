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
RULES_REL = Path(".claude/context/rules.json")
HOOKS_REL = Path(".claude/hooks")
SEPARATORS = re.compile(r"\s*(?:—|–|--|-|:|—)\s+")
DEPTHS = ("quick", "standard", "deep")
# Any of these in the request means the money/schema rules are in play.
# `p/l` and `p&l` are spelled out rather than folded into the `\w*` alternation
# below: the old `p/?l` made the optional slash collapse to a bare `pl`, and with
# the trailing `\w*` that matched `plan`, `planning`, `please` and `platform`. It
# put specs/02-data-model.md at the top of the reading list, and raised the money
# flag, for any task with the word "planning" in it.
MONEY_WORDS = re.compile(
    r"\b(money|cash|cent|price|pricing|cost|basis|profit|loss|margin|"
    r"value|valuation|fifo|lot|intent|allocat|realiz|unrealiz|tax)\w*"
    r"|\bp[/&]l\b",
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

        # A window whose design lives in the decision log must cite an exact
        # heading, and that heading must still be there. Without the anchor the
        # reading list falls back to the whole 89-entry file; without the check,
        # a renamed heading silently drops the design note instead of failing.
        for spec in win.get("specs", []):
            if not spec.startswith(ADR_LOG):
                continue
            anchor = adr_anchor(spec)
            if anchor is None:
                problems.append(
                    f"{key}: cites {ADR_LOG} with no '{ADR_ANCHOR.strip()}' anchor — "
                    "planning would have to read the whole log"
                )
            elif adr_section(root, anchor) is None:
                problems.append(f"{key}: ADR heading not found — {anchor!r}")

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
        if not path.endswith(".md"):
            continue
        anchor = adr_anchor(spec)
        if anchor is not None:
            # Not a whole-file read: the map cites an exact heading, resolved
            # here to the line range that holds it. `validate` fails if it moved.
            span = adr_section(root, anchor)
            where = f"lines {span[0]}-{span[1]}" if span else "**anchor moved**"
            out.append((path, f"design lives here — read {where}: “{anchor}”"))
            continue
        out.append((path, "spec this window implements"))
    # `docs/decisions.md` used to be listed here. It is 89 entries and ~30k
    # tokens, it is append-only so that cost only rises, and it answers the
    # wrong question: planning needs to know what *binds* (bounded — the rule
    # registry) rather than why every past decision went the way it did
    # (unbounded). The log is now reached one section at a time, by following a
    # rule's anchor with `rules.py show <ID>`. See ticket 010.

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
        # Keyed on (path, why): two *sections* of the decision log are two
        # legitimate rows, where two identical whole-file reads are not.
        if (norm, why) in seen:
            continue
        seen.add((norm, why))
        rows.append((norm, why, exists(root, norm)))
    return rows


# --------------------------------------------------------------------------
# the rule registry — what binds, scoped to the files this task will touch
# --------------------------------------------------------------------------


def _path_matches(root: Path):
    """``rules.py``'s glob matcher, imported rather than reimplemented.

    Its docstring records two bugs it exists to prevent — ``**`` must span
    *zero* directories or ``ui/**/*.py`` misses ``ui/theme.py``, and matching is
    ``fnmatchcase`` so the Windows box behaves like everywhere else — plus a
    measured 7.86s hang on a doubled ``**``. Copying it here would re-open all
    three in a second place. Both files are stdlib-only, so the import costs
    nothing.
    """
    hooks = root / HOOKS_REL
    if str(hooks) not in sys.path:
        sys.path.insert(0, str(hooks))
    try:
        from rules import path_matches
    except ImportError:  # pragma: no cover - hook removed or renamed
        return None
    return path_matches


def load_rules(root: Path) -> dict:
    """The rule registry, or a hard failure.

    Deliberately not best-effort. This block replaced the dossier's hardcoded
    rules prose, so a registry that fails to load must stop the run — printing a
    dossier with no rules in it reads as "nothing binds here", which is the one
    way this can be dangerously wrong.
    """
    path = root / RULES_REL
    if not path.exists():
        raise SystemExit(f"error: no {RULES_REL} — the rule registry is missing")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {RULES_REL} is not valid JSON — {exc}") from exc


def task_paths(root: Path, win: dict, rows: list[tuple[str, str, bool]]) -> list[str]:
    """The repo-relative files this task is most likely to touch — the window,
    its services and its tests, which is exactly what the reading list holds."""
    paths = {win["file"]}
    paths.update(rel(root, s.split(" ")[0]) for s in win.get("services", []))
    paths.update(win.get("tests", []))
    paths.update(p for p, _why, ok in rows if ok and p.endswith(".py"))
    return sorted(p.replace("\\", "/") for p in paths if p)


def scoped_rules(
    root: Path, paths: list[str]
) -> tuple[dict[str, list[dict]], list[str]]:
    """``({lane: [rule, ...]}, unclaimed_paths)`` for these files.

    ``unclaimed_paths`` is the honest half: a path no rule's scope claims is
    reported rather than passed over, because an empty rules block and "the
    registry does not cover this" look identical to a reader and mean opposite
    things.
    """
    data = load_rules(root)
    matches = _path_matches(root)
    if matches is None:  # pragma: no cover - hook removed
        raise SystemExit("error: .claude/hooks/rules.py is missing path_matches()")

    by_lane: dict[str, list[dict]] = {}
    claimed: set[str] = set()
    for rule in data["rules"]:
        if rule["id"] == "UNENUMERATED":
            continue  # the registry's own escape hatch, scoped `**`; not a finding
        hit = [p for p in paths for g in rule["scope"] if matches(p, g)]
        if hit:
            by_lane.setdefault(rule["lane"], []).append(rule)
            claimed.update(hit)
    for lane in by_lane:
        by_lane[lane].sort(key=lambda r: r["id"])
    return by_lane, [p for p in paths if p not in claimed]


ADR_LOG = "docs/decisions.md"
ADR_ANCHOR = " § "  # separates the path from the exact heading it cites


def adr_anchor(spec: str) -> str | None:
    """The heading a map spec cites, if it cites the decision log."""
    if not spec.startswith(ADR_LOG) or ADR_ANCHOR not in spec:
        return None
    return spec.split(ADR_ANCHOR, 1)[1].strip()


def adr_section(root: Path, anchor: str) -> tuple[int, int] | None:
    """``(first_line, last_line)`` of one ADR, or ``None`` if it has moved.

    Four windows have no spec file of their own — their design lives in the
    decision log. Left as a bare path that is a ~30k-token read to reach one
    section, which is the problem this ticket removes everywhere else. An exact
    anchor makes it a bounded read, and the same discipline the rule registry
    already applies to the same file: cite a heading, never the whole thing.

    Matching by *date* was tried first and is far too coarse — 2026-07-10 alone
    carries ten entries, and the reading list filled with ADRs about ripping
    intent and import/export for a price-lookup task.
    """
    path = root / ADR_LOG
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    heads = [(i, ln) for i, ln in enumerate(lines, 1) if re.match(r"^#{2,3} ", ln)]
    for n, (start, heading) in enumerate(heads):
        if heading.strip() == anchor:
            end = heads[n + 1][0] - 1 if n + 1 < len(heads) else len(lines)
            return start, end
    return None


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

    rows = reading_list(root, data, win, depth, task)

    # What binds, scoped to the files this task will touch. This replaced a
    # hardcoded prose list plus a whole-file read of the 89-entry decision log:
    # the registry is bounded (rules are superseded in place, never appended),
    # every rule carries a `scope` glob and an `excerpt_sha256` seal, and
    # `rules.py check` fails when the text a rule cites moves under it.
    by_lane, unclaimed = scoped_rules(root, task_paths(root, win, rows))
    add("## Rules in scope — these bind this task")
    add("")
    if by_lane:
        for lane in sorted(by_lane):
            add(f"**{lane}**")
            for rule in by_lane[lane]:
                add(f"- `{rule['id']}` — {rule['rule']}")
                add(f"  - violation: {rule['violation']}")
            add("")
    else:
        add("_No registry rule claims any of this task's files._")
        add("")
    if unclaimed:
        add(
            "> **Not claimed by any rule:** "
            + ", ".join(f"`{p}`" for p in unclaimed[:8])
            + ("…" if len(unclaimed) > 8 else "")
            + ". The registry does not cover these paths — judge them on the "
            "specs and `CLAUDE.md`, and if something here should bind, say so "
            "(the registry's own `UNENUMERATED` id exists for exactly that)."
        )
        add("")
    add(
        "_Why a rule exists → `python .claude/hooks/rules.py show <ID>`. That "
        "prints the one section it is sourced from, which is how "
        "`docs/decisions.md` is read now — by anchor, never whole._"
    )
    add("")

    add("## Required reads")
    add("")
    add("| # | Path | Why | On disk |")
    add("|---|------|-----|---------|")
    for i, (path, why, ok) in enumerate(rows, 1):
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
