---
name: task-planning
description: Produce a grounded task brief before working on a TCGinventory window. Use when starting any feature, fix, or change scoped to a screen (Dashboard, Transaction, Inventory, History, Calendar, Price Lookup, eBay Auctions, Recognition, Market, Models, Operations) — it resolves the window from the request, renders a verified dossier from .claude/context/app-map.json, and turns it into a plan. Also use when the user types /task-planning.
argument-hint: <window> — <what you want done> [| depth: quick|standard|deep]   e.g. "inventory — add a 'days held' column, sortable | depth: standard"
allowed-tools: [Read, Grep, Glob, Bash, PowerShell]
---

# Task planning

Request: **$ARGUMENTS**

## Step 1 — Run the dossier script. This is not optional.

```bash
python .claude/skills/task-planning/scripts/plan.py brief "$ARGUMENTS"
```

Use `./.venv/Scripts/python.exe` if a bare `python` is not on PATH. The script is
stdlib-only, so any interpreter works.

**Everything factual in your brief comes from that output.** Do not name a file,
service, table, signal or test that the script did not print. You have not read
this repo yet; the script has.

The script self-checks before it prints: every path in the map must exist, the
window list must match `NAV_ITEMS` in `main_window.py`, and every `class` must be
in the file claimed. Handle its exit code:

| Exit | Meaning | What you do |
|---|---|---|
| `0` | Dossier printed, ending in `<!-- map_ok: true … -->` | Continue to step 2. |
| `1` | **The map is stale.** It lists the specific drift. | Stop. Fix `.claude/context/app-map.json` against the working tree — *the code wins, always* — then re-run. Tell the user what drifted. |
| `2` | The window could not be resolved (or was ambiguous). | It prints every window and its aliases. Pick the right one and re-run with it named explicitly, or ask the user if genuinely ambiguous. |

Other subcommands: `plan.py validate` (drift check alone) and `plan.py windows`
(list destinations + aliases).

**Cross-cutting work with no window** — migrations, theme, packaging,
services-only changes — will exit `2`. That is correct. Run `plan.py validate`
instead and read the `architecture`, `databases` and `shared_ui` sections of the
map directly. Say in your brief that you did this and why.

## Step 2 — Read what the dossier tells you to

Read every row of its **Required reads** table, in order. It is already sorted by
priority and depth, and each row says why it is there. Skipping a row because it
"looks unrelated" is how the gotchas get violated.

Then check `docs/decisions.md` (newest first) for an ADR that already settled part
of this task — the dossier lists the relevant ones, but the log moves faster than
the map.

## Step 3 — Write the brief

The dossier is *context*, not the deliverable. Do not paste it back at the user.
Write these five sections on top of it:

### 1. What is being asked
One paragraph restating the task in the app's own vocabulary (lot, line, intent,
basis, region, capture, operation). Name any ambiguity you resolved and the
assumption you made. If the script inferred the window rather than being told,
say so.

### 2. Blast radius
A table: `file` | `change` | `risk`. Cover the view, any service, any Qt model or
delegate, any migration, any *other* window that reads the same tables, and the
tests that must change. State plainly whether a new migration is needed.

### 3. Rules this task must not break
Only the ones actually at stake — from the dossier's *Rules that always bind*,
its window **gotchas**, and `specs/02-data-model.md`. Three to six lines. Quote
the gotcha and say why it applies. If nothing beyond the boilerplate applies, say
that.

### 4. Plan
Numbered steps in dependency order, each naming the file it lands in. Services
before UI. Migration before service. Tests alongside, not after. Mark any step
that is a judgement call **[decide]** and give your recommendation *in prose* —
this repo's stated preference is a reasoned recommendation, not a pick-list,
unless the choice is crisp and mutually exclusive.

### 5. Open questions
Only ones that would change the work. If there are none: "None — proceeding."

Keep it tight. This is a working document.

## Step 4 — Stop

Do not edit anything yet. Let the user confirm or adjust the plan.

Once they approve, either **work it** or **file it** — not neither.

**Working it now:** the steps in order, then close out per the dossier's
**Definition of done**: `ruff format .`, `ruff check .`, the full suite with
`QT_QPA_PLATFORM=offscreen`, an ADR in `docs/decisions.md` if the decision was
significant, `CLAUDE.md` status if a milestone moved. Then a branch and a PR —
this repo moved off direct-to-`main` on 2026-08-07 — and rebuild the exe after
merge.

**Not working it now:** file the brief as a ticket via `/sdd-ticket`, `kind:
feature` or `fix`. The brief is already the right shape for one — the blast-radius
table is the area of impact, the numbered steps are the plan, and the open
questions are what blocks it. An agreed plan that is only in a transcript is a
plan that gets re-derived from scratch, dossier and all.

## Maintaining the map

If you add or rename a window, add a migration, move logic between layers, or add
a test file for a window, update `.claude/context/app-map.json` in the same
change. `plan.py validate` and `tests/test_app_map.py` both fail when it drifts,
so staleness surfaces as a test failure rather than as a confident wrong brief.
