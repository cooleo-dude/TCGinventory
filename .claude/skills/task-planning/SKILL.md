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

**A window must be *named*, not merely mentioned.** `"Inventory — add a filter"`
resolves; so does a mention specific enough to mean it (`recognition`,
`dashboard`, `calendar`). A sentence that merely contains a short alias does
**not** — `log`, `post`, `write`, `shows`, `audit`, `month`, `price`, `stock`
are ordinary English words, and treating them as names produced a confident
dossier for the wrong screen. If you meant a window, say `"<window> — <task>"`.

**Cross-cutting work with no window** — migrations, theme, packaging, tooling,
services-only changes — will exit `2`. That is correct. Run `plan.py validate`
instead and read the `architecture`, `databases` and `shared_ui` sections of the
map directly. Say in your brief that you did this and why.

## Step 2 — Read what the dossier tells you to

Read every row of its **Required reads** table, in order. It is already sorted by
priority and depth, and each row says why it is there. Skipping a row because it
"looks unrelated" is how the gotchas get violated. A row that names a **line
range** is a section read — read those lines, not the file.

**Do not read `docs/decisions.md` looking for what binds.** It is 89 entries and
~30k tokens, it is append-only so that cost only rises, and it answers the wrong
question. What binds is the dossier's **Rules in scope** block, taken from
`.claude/context/rules.json` and narrowed to the files this task touches: every
rule there carries a `scope` glob and an `excerpt_sha256` seal, so it cannot
drift from the text it cites without `rules.py check` going red.

When you want the *reasoning* behind a rule rather than the rule itself:

```bash
python .claude/hooks/rules.py show <RULE-ID>
```

That prints the one section the rule is sourced from — which for a couple of
rules is an ADR. That is how the decision log is read now: by anchor, bounded,
and only when a rule is actually in your way.

If the dossier says a path is **not claimed by any rule**, that is a real
statement and not an oversight: judge those files on the specs and `CLAUDE.md`,
and if something there *should* bind, say so in your brief — the registry's own
`UNENUMERATED` id exists for exactly that.

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
Only the ones actually at stake — from the dossier's **Rules in scope** block,
its window **gotchas**, and `specs/02-data-model.md`. Three to six lines. **Cite
each by its rule id** (`MONEY-CENTS`, `QT-FREE-CORE`) so the reader can run
`rules.py show` on it, quote the gotcha, and say why it applies. The block lists
everything the scope globs match, which is deliberately a superset — your job is
to name the few that this change can actually violate, not to restate the block.
If nothing beyond the boilerplate applies, say that.

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

## Step 5 — File the ticket. Always.

**Once the brief is approved it becomes a ticket, before any code is written.**
Not "if you are not doing it now" — always. The ticket is what the branch, the
pull request and the commit all point back at, and it is the only artefact that
survives the conversation. A brief that exists solely in a transcript gets
re-derived from scratch, dossier and all.

`/sdd-ticket`, `kind: feature` or `fix`. The brief is already the right shape:
the blast-radius table is the area of impact, the numbered steps are the plan,
the open questions are what blocks it, and the rules section is why the approach
is what it is. Then `push` it so the issue exists.

Then work it, or leave it `open` for someone else — that decision no longer
changes whether the ticket gets written.

## Step 6 — Working it

**Take your own worktree before the first edit.** Call `EnterWorktree` — CLAUDE.md's
`## Working in parallel sessions` section authorises it. Several sessions work this
repo at once and a checkout has exactly one `HEAD`, so a shared tree puts their
commits on your branch and into your PR. `.claude/hooks/branch_lease.py` enforces
this: beside another live session it denies `checkout`, `switch`, `reset`, `rebase`,
`merge`, `git add -A` and pathspec-less `git commit`. Alone in your own worktree,
none of it fires.

Then the steps in order, and close out per the dossier's **Definition of done**:
`ruff format .`, `ruff check .`, the full suite with `QT_QPA_PLATFORM=offscreen`,
an ADR in `docs/decisions.md` if the decision was significant, `CLAUDE.md` status
if a milestone moved.

Then a branch and a pull request — this repo moved off direct-to-`main` on
2026-08-07 — with `Closes #<issue>` in the PR body. Then step 7.

## Step 7 — Closing it out

**A merged PR is not a closed ticket.** The exe the user actually runs is the
**main clone's** `dist/TCGinventory/TCGinventory.exe`, and it is still the
pre-change build until you rebuild it *there*. Your worktree's `dist/` is not
that exe — PyInstaller writes `dist/` under the build root, so a build in
`.claude/worktrees/<branch>/` is thrown away with the worktree.

In this order:

1. **Merge the PR.** `gh pr merge <N> --squash --delete-branch` (the recent ones
   land squashed; older history has merge commits). `review_gate.py` guards this
   command, but only refuses when a panel **you** ran on this sha returned
   `block` — a ticket with no panel merges, because the critics are opt-in.
2. **Bring the main clone up to date.** `git -C <main-clone> pull --ff-only`.
   Skipping this rebuilds the exe from the old `main` and everything below then
   passes against a build that lacks your change.
3. **Stop the running exe.** `Stop-Process -Name TCGinventory -Force`, or
   `taskkill //F //IM TCGinventory.exe`. Killing it to rebuild is expected, and
   it is not optional: with it running, `--clean` dies with `PermissionError:
   [WinError 5]` on a locked DLL and leaves the *previous* build in `dist/`.
4. **Rebuild**, from the main clone root:

   ```powershell
   .\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm packaging\tcginventory.spec
   ```

   Read **PyInstaller's own exit code** — not a trailing `echo`/`tail` in the
   same command line, which reports `0` over a failed build.
5. **Confirm the exe's timestamp actually moved**, then boot it against a
   throwaway `TCGINVENTORY_DATA_DIR` and check the process stays alive and
   `inventory.db` appears. Because a failed `--clean` leaves the old build in
   place, a boot check that skips the timestamp proves nothing (2026-08-05).
6. **Close the ticket** — `/sdd-ticket close <N>` — recording the measured
   result, not the intent.
7. **Drop the worktree.** `ExitWorktree` with `action: "remove"`, or by hand:
   `git worktree remove --force <path>` and `branch_lease.py release`. A worktree
   left behind keeps a lease and denies the next session commands in this tree.

**One exception, before the merge:** if the change touched packaging inputs —
`packaging/tcginventory.spec`, a new bundled asset, a model, a hidden import — a
spec omission only shows up frozen, so build once in the worktree first as a
smoke test. That build is a check, never the deliverable; steps 1–5 still run.

## Maintaining the map

If you add or rename a window, add a migration, move logic between layers, or add
a test file for a window, update `.claude/context/app-map.json` in the same
change. `plan.py validate` and `tests/test_app_map.py` both fail when it drifts,
so staleness surfaces as a test failure rather than as a confident wrong brief.
