"""Rule registry tool — keeps the critics' static reference honest.

    rules.py check              every source, anchor and seal still resolves
    rules.py list [--lane L]    what the critics may judge against
    rules.py scope <path ...>   which rules apply to these files
    rules.py show <ID>          one rule, plus the section it is sourced from
    rules.py seal [--id ID]     (re)record the excerpt hash after a spec edit
    rules.py validate <file>    check a critic's JSON verdict against the schema

`.claude/context/rules.json` is an **index**, never the source. Each rule points
at the document that states it, and carries a sha256 of the cited section. An
anchor check alone only catches a renamed heading; the seal catches the heading
staying put while the words underneath change — which is the way a rule registry
actually rots.

When `check` fails on a seal, that is the system working. Read the section, decide
whether the rule still says what the registry claims, then `seal --id <ID>`
deliberately. Never reseal to make a failure go away.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# `show` is a bounded read by construction — see cmd_show.
SHOW_LINES = 80
MAX_LINE = 400
RULES = REPO / ".claude" / "context" / "rules.json"
SCHEMA = REPO / ".claude" / "context" / "verdict.schema.json"

AGENTS = REPO / ".claude" / "agents"


def load() -> dict:
    return json.loads(RULES.read_text(encoding="utf-8"))


def section_text(path: Path, anchor: str | None) -> str | None:
    """The cited section: the anchor line up to the next heading of equal or
    higher level. A null anchor seals the whole file — used where the citation is
    a convention the file embodies rather than a titled section."""
    text = path.read_text(encoding="utf-8")
    if anchor is None:
        return text
    if path.suffix == ".json":
        # A JSON anchor is a dotted key path; seal that subtree.
        try:
            node = json.loads(text)
        except json.JSONDecodeError:
            return None
        for part in anchor.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return json.dumps(node, sort_keys=True)
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == anchor)
    except StopIteration:
        return None
    level = len(anchor) - len(anchor.lstrip("#")) if anchor.startswith("#") else 0
    out = [lines[start]]
    for line in lines[start + 1 :]:
        if level and line.startswith("#"):
            if len(line) - len(line.lstrip("#")) <= level:
                break
        out.append(line)
    return "\n".join(out)


def path_matches(path: str, pattern: str) -> bool:
    """Glob match in which ``**`` spans **zero** or more directories.

    ``fnmatch`` alone is wrong for this. Its ``*`` matches ``/`` as well, so
    ``src/tcginventory/**/*.py`` demands at least one intermediate directory and
    silently misses ``src/tcginventory/__init__.py``. Worse,
    ``src/tcginventory/ui/**/*.py`` missed ``ui/theme.py`` — the very file the
    THEME-TOKENS rule cites. A rule whose scope cannot match its own source is a
    rule that never fires, and it fails silently: the critic is simply not
    dispatched.

    Matching is **case-sensitive** on every platform (`fnmatchcase`, not
    `fnmatch`, which normalises case on Windows). Paths come from
    `git diff --name-only`, which reports git's tracked case rather than the
    filesystem's, so a case-sensitive match is the one that behaves the same on
    the Windows dev box and anywhere else.
    """
    return _match_segments(path.split("/"), _collapse(pattern.split("/")))


def _collapse(pattern: list[str]) -> list[str]:
    """Fold runs of ``**`` into one. ``a/**/**/b`` means exactly ``a/**/b``.

    Not cosmetic. Each ``**`` branches into ``len(parts)+1`` recursive calls, so
    consecutive ones multiply: measured 0.06s at five in a row and **7.86s at
    eight** against a 25-segment path. A doubled ``**`` — a typo, or two agents
    merging edits to the same `scope` array — would hang every `rules.py scope`
    call, and since every critic shells out to that before judging a diff, the
    whole review gate would stop responding with no error saying why. Collapsing
    makes the pathological pattern impossible rather than merely unlikely.
    """
    out: list[str] = []
    for segment in pattern:
        if segment == "**" and out and out[-1] == "**":
            continue
        out.append(segment)
    return out


def _match_segments(parts: list[str], pattern: list[str]) -> bool:
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        # Zero or more segments: try consuming none, then one, then two, ...
        return any(_match_segments(parts[i:], rest) for i in range(len(parts) + 1))
    if not parts or not fnmatch.fnmatchcase(parts[0], head):
        return False
    return _match_segments(parts[1:], rest)


def seal_of(text: str) -> str:
    return hashlib.sha256(
        "\n".join(t.strip() for t in text.splitlines()).encode()
    ).hexdigest()[:16]


#: Files unrelated work appends to. A rule sourced from one must cite a heading.
APPEND_ONLY_SOURCES = ("docs/decisions.md",)


def anchor_problems(data: dict) -> list[str]:
    """A rule sourced from an append-only log must anchor at a heading.

    ``anchor: null`` seals the whole file. That is right where the file as a whole
    embodies the convention — ``tests/conftest.py`` for QTHREAD-LIFETIME,
    ``verdict.schema.json`` for UNENUMERATED — and corrosive on a 2,000-line log
    that unrelated work appends to weekly: the seal breaks on every entry that had
    nothing to do with the rule, and resealing becomes reflex. This module's own
    instruction — *never reseal to make a failure go away* — only survives while
    the failures mean something, so the shape that trains the opposite habit is
    rejected here rather than left to be remembered.
    """
    problems: list[str] = []
    for rule in data["rules"]:
        source = rule["source"]
        if source["file"] in APPEND_ONLY_SOURCES and source.get("anchor") is None:
            problems.append(
                f"{rule['id']}: {source['file']} is append-only — seal a heading, "
                f"not the whole file (anchor is null)"
            )
    return problems


def cmd_check(args) -> int:
    data = load()
    problems: list[str] = []
    seen: set[str] = set()
    lanes = set(data["lanes"]) | {"any"}

    for rule in data["rules"]:
        rid = rule["id"]
        if rid in seen:
            problems.append(f"{rid}: duplicate id")
        seen.add(rid)

        if rule["lane"] not in lanes:
            problems.append(
                f"{rid}: lane {rule['lane']!r} is not one of {sorted(lanes)}"
            )
        if rule["severity_max"] not in data["severities"]:
            problems.append(
                f"{rid}: severity_max {rule['severity_max']!r} is not valid"
            )

        source = rule["source"]
        path = REPO / source["file"]
        if not path.is_file():
            problems.append(f"{rid}: source {source['file']} does not exist")
            continue
        text = section_text(path, source.get("anchor"))
        if text is None:
            problems.append(
                f"{rid}: anchor {source['anchor']!r} not found in {source['file']}"
            )
            continue
        actual = seal_of(text)
        recorded = source.get("excerpt_sha256")
        if recorded is None:
            problems.append(f"{rid}: unsealed — run `rules.py seal --id {rid}`")
        elif recorded != actual:
            problems.append(
                f"{rid}: {source['file']} changed under the seal "
                f"({recorded} -> {actual}). Re-read the section, confirm the rule "
                f"still holds, then `rules.py seal --id {rid}`."
            )

    problems.extend(anchor_problems(data))

    # Every lane must map to an agent that exists, or a critic cannot be dispatched.
    for lane, agent in data["lanes"].items():
        if not (AGENTS / f"{agent}.md").is_file():
            problems.append(f"lane {lane}: agent {agent}.md is missing")

    for problem in problems:
        print(f"  FAIL  {problem}")
    if problems:
        return 1
    print(f"  ok — {len(seen)} rules, every source, anchor and seal resolves")
    return 0


def cmd_list(args) -> int:
    data = load()
    for rule in data["rules"]:
        if args.lane and rule["lane"] != args.lane:
            continue
        print(
            f"  {rule['id']:<28} {rule['lane']:<11} {rule['severity_max']:<6} "
            f"{rule['source']['file']}"
        )
    return 0


def lanes_in_scope(paths: list[str]) -> dict[str, list[str]]:
    """``{lane: [rule ids]}`` for the rules these paths can actually violate.

    This is what decides how many critics get spawned, so it is a function rather
    than something each critic re-derives for itself. A lane with nothing in scope
    has nothing to judge, and an agent spawned to discover that costs ~26k tokens
    to reach the answer already computed here.
    """
    data = load()
    normalised = [p.replace("\\", "/") for p in paths]
    hits: dict[str, list[str]] = {}
    for rule in data["rules"]:
        if rule["id"] == "UNENUMERATED":
            continue
        for pattern in rule["scope"]:
            if any(path_matches(p, pattern) for p in normalised):
                hits.setdefault(rule["lane"], []).append(rule["id"])
                break
    return {lane: sorted(ids) for lane, ids in hits.items()}


def cmd_scope(args) -> int:
    data = load()
    hits = lanes_in_scope(args.paths)
    if args.json:
        print(json.dumps({"lanes": hits}, indent=2))
        return 0
    if not hits:
        print("  no rules in scope — nothing for the panel to judge")
        return 0
    for lane in sorted(hits):
        print(f"  {lane:<11} {', '.join(sorted(hits[lane]))}")
    agents = [data["lanes"][lane] for lane in sorted(hits) if lane in data["lanes"]]
    print(f"\n  dispatch: {', '.join(agents)}")
    return 0


def cmd_show(args) -> int:
    """One rule in full, including the section it is sourced from.

    This is what makes the decision log affordable. Planning reads the *registry*
    — bounded, scoped, sealed — and never the 89-entry log, whose cost grows with
    the age of the project. When an agent needs the reasoning behind a rule
    rather than the rule itself, it follows the rule's anchor to exactly one
    section instead of reading the whole file.
    """
    data = load()
    rule = next((r for r in data["rules"] if r["id"] == args.id), None)
    if rule is None:
        known = ", ".join(sorted(r["id"] for r in data["rules"]))
        print(f"  no rule {args.id!r}. Known ids: {known}")
        return 1

    source = rule["source"]
    print(f"  {rule['id']}  ({rule['lane']}, max {rule['severity_max']})")
    print(f"  rule      {rule['rule']}")
    print(f"  violation {rule['violation']}")
    print(f"  scope     {', '.join(rule['scope'])}")
    anchor = source.get("anchor")
    where = f"{source['file']}  ·  {anchor}" if anchor else f"{source['file']} (whole)"
    print(f"  source    {where}")

    path = REPO / source["file"]
    if not path.exists():
        print(f"\n  (source file missing: {source['file']})")
        return 1
    text = section_text(path, anchor)
    if text is None:
        print(f"\n  (anchor not found in {source['file']} — run rules.py check)")
        return 1

    # Bounded, always. Some anchors are JSON subtrees rather than prose headings
    # — `windows` in app-map.json is a single ~15k-token line — and a command
    # whose whole purpose is to replace a 30k-token read must not become one.
    lines = text.splitlines()
    cap = getattr(args, "lines", None) or SHOW_LINES
    shown = 0
    print()
    for line in lines:
        if shown >= cap:
            print(f"  | … {len(lines) - shown} more line(s)")
            print(f"\n  Full section: {source['file']}  ·  {anchor or '(whole file)'}")
            break
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + f"… (+{len(line) - MAX_LINE} chars)"
        print(f"  | {line}")
        shown += 1
    return 0


def cmd_seal(args) -> int:
    data = load()
    changed = 0
    for rule in data["rules"]:
        if args.id and rule["id"] != args.id:
            continue
        source = rule["source"]
        path = REPO / source["file"]
        if not path.is_file():
            print(f"  skip  {rule['id']}: {source['file']} missing")
            continue
        text = section_text(path, source.get("anchor"))
        if text is None:
            print(f"  skip  {rule['id']}: anchor not found")
            continue
        new = seal_of(text)
        if source.get("excerpt_sha256") != new:
            source["excerpt_sha256"] = new
            changed += 1
            print(f"  sealed  {rule['id']}  {new}")
    if changed:
        RULES.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"  {changed} rule(s) sealed")
    return 0


def cmd_validate(args) -> int:
    """Check a critic's verdict against the schema and the registry.

    Deliberately hand-rolled: jsonschema is not a dependency of this app, and a
    gate that needs a pip install is a gate that gets skipped.
    """
    data = load()
    ids = {r["id"]: r for r in data["rules"]}
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    problems: list[str] = []

    if payload.get("lane") not in data["lanes"]:
        problems.append(f"lane {payload.get('lane')!r} is not a known lane")
    if payload.get("verdict") not in ("pass", "revise", "block"):
        problems.append(f"verdict {payload.get('verdict')!r} is not pass|revise|block")

    findings = payload.get("findings", [])
    for i, finding in enumerate(findings):
        where = f"findings[{i}]"
        for field in ("rule", "file", "line", "evidence", "severity"):
            if field not in finding:
                problems.append(f"{where}: missing {field!r}")
        rid = finding.get("rule")
        if rid and rid not in ids:
            problems.append(
                f"{where}: rule {rid!r} is not in the registry — a finding must "
                f"cite an enumerated rule, or UNENUMERATED"
            )
        elif rid:
            allowed = ids[rid]["severity_max"]
            if finding.get("severity") == "block" and allowed != "block":
                problems.append(
                    f"{where}: {rid} is capped at {allowed!r}; it cannot block"
                )
            if rid == "UNENUMERATED" and not finding.get("suggested_rule"):
                problems.append(f"{where}: UNENUMERATED needs `suggested_rule`")
        if not str(finding.get("evidence", "")).strip():
            problems.append(f"{where}: evidence is empty")

    if payload.get("verdict") == "block" and not any(
        f.get("severity") == "block" for f in findings
    ):
        problems.append("verdict is `block` but no finding has severity `block`")
    if payload.get("verdict") == "pass" and findings:
        problems.append("verdict is `pass` but findings were reported")

    for problem in problems:
        print(f"  FAIL  {problem}")
    if problems:
        return 1
    print(f"  ok — {payload['lane']}: {payload['verdict']}, {len(findings)} finding(s)")
    return 0


def main() -> None:
    # Anchors are section headings, and this repo's headings carry em dashes and
    # arrows. The Windows console defaults to cp1252, which cannot encode them —
    # `show` is the first command to print an anchor routinely, so it is the
    # first to have crashed on one. Replace rather than raise: a mangled dash is
    # a far better outcome than a traceback in a tool the review gate shells out
    # to.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check").set_defaults(func=cmd_check)
    lister = sub.add_parser("list")
    lister.add_argument("--lane")
    lister.set_defaults(func=cmd_list)
    scoper = sub.add_parser("scope")
    scoper.add_argument("paths", nargs="+")
    scoper.add_argument(
        "--json",
        action="store_true",
        help="machine-readable {lane: [rule ids]} — what the gate reads to decide "
        "which critics to spawn",
    )
    scoper.set_defaults(func=cmd_scope)
    shower = sub.add_parser("show")
    shower.add_argument("id")
    shower.add_argument(
        "--lines",
        type=int,
        default=SHOW_LINES,
        help=f"cap the cited section at N lines (default {SHOW_LINES})",
    )
    shower.set_defaults(func=cmd_show)
    sealer = sub.add_parser("seal")
    sealer.add_argument("--id")
    sealer.set_defaults(func=cmd_seal)
    validator = sub.add_parser("validate")
    validator.add_argument("file")
    validator.set_defaults(func=cmd_validate)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
