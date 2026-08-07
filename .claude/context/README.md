# `.claude/context`

Anchoring data for Claude Code sessions. Version-controlled, so it travels across
machines the way `CLAUDE.md` does.

## `app-map.json`

A machine-readable index of the app: layers and non-negotiable rules, the four
SQLite files and their tables, shared UI machinery, the `MainWindow` contract,
and a **dossier per nav destination** (purpose, primary file, services,
repositories, tables, tests, signals, gotchas, specs).

Its job is to stop a fresh session from guessing. Every path in it is real —
most repo-relative, a few package-relative to `src/tcginventory/` where that
reads better in prose; the renderer normalises them.

## Who reads it

[`.claude/skills/task-planning/`](../skills/task-planning/SKILL.md) — the skill
does not read the map by eye. It runs
[`scripts/plan.py`](../skills/task-planning/scripts/plan.py), which is the only
thing that turns a request into paths, and which refuses to print anything until
the map matches the working tree:

```bash
python .claude/skills/task-planning/scripts/plan.py brief "inventory — add a 'days held' column"
python .claude/skills/task-planning/scripts/plan.py validate   # drift check alone
python .claude/skills/task-planning/scripts/plan.py windows     # destinations + aliases
```

Exit `1` means the map is stale and names the drift; exit `2` means the window
could not be resolved and lists every alias.

Nothing in the app depends on any of this at runtime — it is documentation with
a schema and a test.

## Keeping it current

`tests/test_app_map.py` fails when the map drifts, so staleness shows up as a red
suite rather than as a confident wrong brief. Refresh the relevant entry when you:

- add or rename a nav destination — update `main_window.nav_order` **and**
  `windows`;
- add a migration — update `databases["inventory.db"].tables`;
- add a service a window calls, or move logic between layers;
- record an ADR that changes how a window behaves — add it to that window's
  `gotchas` and `specs`;
- add a test file for a window — add it to that window's `tests`.

Bump `version` on a structural change; update `captured_at` /
`captured_at_commit` on a full pass.

The standing rule: **if the map disagrees with the code, the code wins** — and the
map gets fixed.

## Usage

```
/task-planning <window> — <what you want done> [| depth: quick|standard|deep]
```

Examples:

```
/task-planning inventory — add a "days held" column, sortable
/task-planning transaction — support a cash-only adjustment with no cards | depth: deep
/task-planning ops — add an operation that prunes old backups
/task-planning models — show the condition grader's per-grade confusion | depth: quick
```

Window names, keys and aliases all resolve — `ops`, `ledger`, `vision`, `ebay`
and `dash` land on Operations, History, Recognition, eBay Auctions and Dashboard
respectively.
