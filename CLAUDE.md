# CLAUDE.md — TCGinventory

Context for Claude Code sessions. This file is version-controlled, so it travels
across machines (unlike session transcripts and auto-memory, which are
machine-local).

## What this is

A local, single-user **PySide6 desktop app** for tracking a personal **and**
business trading-card inventory (Pokémon-focused) — purchases, sales, and trades
on a single-card or lot basis, with cost basis and profit/loss. Local SQLite;
offline; no external price feeds in V1.

## Status

- **Planning baseline: fully ratified** (specs 00–05). See
  [`specs/README.md`](specs/README.md) for the section-by-section sign-off.
- **M0 (scaffolding): done** — package skeleton, config, blank main window,
  tests. Verified on Python 3.10.10 (ruff clean, tests pass, window launches).
- **M1 (data layer & migrations): done** — `data/db.py` (WAL + FK connection),
  `data/migrator.py` + `data/migrations/0001_init.sql` (forward-only runner,
  `schema_version`), `domain/models.py` dataclasses, `data/repositories.py`
  (CRUD per entity, match-or-create identity lookup, FIFO open-lot query).
  Migrations run on startup. Verified on Python 3.12.10 (ruff clean, 21 tests
  pass, fresh DB created at schema version 1).
- **M2 (core services & money math): done** — `services/allocation.py` (pure
  market-value split), `services/transactions.py` (unified post/void/edit/adjust,
  FIFO, audit), `services/inventory.py` (per-lot valuation, ripping at $0),
  `services/reporting.py` (realized/unrealized P/L, margin + gross-margin %,
  money-movement & realized-profit series). Typed exceptions; headless guard test.
  All 5 acceptance scenarios covered. Verified on Python 3.12.10 (ruff clean, 65
  tests pass, zero Qt in domain/services). Historical collection-value series
  deferred to M3 (see decisions log).
- **M3 (read-only UI over real data): done** — main window hosts live Dashboard
  (QtCharts money-movement/realized-profit + summary tiles, intent filter),
  Inventory grid (`ui/models/inventory_model.py`, sortable + filter bar), History
  (transaction log + detail drill-down), and Item detail dialog. Backed by
  `InventoryService.inventory_rows()` and a demo `data/seed.py`
  (`TCGINVENTORY_SEED=1`). Verified on Python 3.12.10 (ruff clean, 79 tests pass,
  offscreen screenshots render all views; ripping lot shows as a loss). The
  Transaction nav item is a placeholder until M4.
- **M4 (write UI — unified Transaction window): done** — `ui/views/transaction.py`
  (live type badge, give/receive tables, cash fields, summary+margin footer via
  `TransactionService.preview()`, submit blocked on oversell), `_card_dialogs.py`
  (give/receive add dialogs; condition greys out when graded; market value
  required), `item_editor.py` (edit/soft-delete guard), and Void/Edit wired from
  History (edit = atomic void+re-post, warns on P/L change). Verified on Python
  3.12.10 (ruff clean, 81 tests pass incl. a full purchase→sale→trade→rip
  lifecycle + edit + void through the UI; offscreen screenshots confirm).
- **M5 (import/export & backup): done** — `services/importexport.py` (human CSV
  exports of items/inventory/transactions; full `export_all`/`import_all` dump-
  restore round-trip; header-mapped item + purchase import with validation
  preview), `data/backup.py` (SQLite online-backup copy + timestamped startup
  auto-backup, keep-10), a File menu in the main window, and startup auto-backup
  wired in `app.py`. Verified on Python 3.12.10 (ruff clean, 92 tests pass;
  export→wipe→import reproduces the dataset; startup backup confirmed).
- **M6 (packaging & polish): done** — app icon (`ui/assets/`), PyInstaller
  build (`packaging/tcginventory.spec` + `entry.py`, onedir; see
  `packaging/README.md`), empty-state notes, Ctrl+S submit + a11y tooltips,
  finalized user guide. Verified on Python 3.12.10 (ruff clean, 94 tests pass;
  the frozen 125 MB exe boots on its own — creates the DB, migrates, writes a
  startup backup, runs the GUI).
- **V1 is feature-complete (M0–M6).** Post-V1 backlog in [`specs/05-roadmap.md`](specs/05-roadmap.md)
  (price feeds, explicit rip/open event, dashboard calendar, specific-lot basis,
  images/scanning, intent-change history, the 30-day void-restore idea, etc.).
- **M-Ops1 (Operations window): done** — the File menu's ten maintenance jobs
  became a nav destination (`ui/views/operations.py`) drawn from a Qt-free
  registry (`services/operations.py`): a tile each with what it does, its status
  and inline progress, a fact strip, and an Activity list backed by a run log
  (migration `0019_operation_run`, also written by the daily background task and
  the startup backup). The File menu is now just *Operations* + *Exit*, and the
  three price/catalog worker threads moved out of `main_window`. Also carries a
  **Maintenance & diagnostics** group (integrity check, VACUUM, recognition-corpus
  re-index with corpus stats), an on-demand **card-shows scrape**, and a
  **"while you were away" digest** of background runs. **Adding a future
  operation = one registry entry + one runner** (a generic `_TaskWorker` covers
  "call a service, report a sentence"); a `models` group is reserved for
  metrics/triggers. Restore is M-Ops2 (see the ADR).

- **Personal Collection (ticket 60): done** — a twelfth nav destination
  (`ui/views/collections.py`) for collecting projects: a block grid over an
  in-window navigator, each opening a checklist with one slot per collector
  number, or per printing in *master set* mode. Six templates from a Qt-free
  registry (`services/collections.py`): set, subset, artist, rarity, card type,
  custom. **Ownership is membership** — a slot is *filled* when a matching lot's
  intent is `personal`, *importable* when it is `resale`, *missing* when unowned
  — so template collections store no membership rows. Bringing a card in changes
  that lot's intent and **splits the lot** when quantity > 1
  (`InventoryService.split_lot_for_intent`, audited), which also closed an audit
  gap: the Inventory grid used to change intent with no `audit_log` row.
  Migration 0025. Verified on Python 3.12.10 (ruff clean, 1,167 tests pass;
  migration 24→25 on a copy of the real ledger leaves every ledger count
  identical; Base Set renders 102 slots / 9 importable in 8 ms).

## Source of truth

- **`specs/`** — what we're building and why. Read [`specs/02-data-model.md`](specs/02-data-model.md)
  before touching money/schema logic; it's the heart of the app.
- **`.claude/context/rules.json`** — what **binds**, as a registry: one entry per
  invariant, each with the files it scopes to and a hash of the text it cites, so
  `rules.py check` fails when a rule drifts from its source. `rules.py scope
  <files>` narrows it to a change; a `/task-planning` dossier prints that
  narrowing for you.
- **`docs/decisions.md`** — the ADR **archive**: why things are the way they are,
  and **write-mostly**. Do not read it whole — 89 entries, ~30k tokens, and it is
  append-only, so that cost only rises. It is also not ordered, whatever its
  header claims. Reach one entry by anchor instead:

  ```bash
  python .claude/hooks/rules.py show <RULE-ID>   # the section a rule is sourced from
  ```

  Still record significant decisions here. Reading it whole is what stopped;
  writing to it did not.

## Non-negotiable architecture rules

- **Domain + service layers are Qt-free** and unit-testable headless. The UI
  never issues SQL and never does money math.
- **Money is integer cents** (`int`), never `float`. Quantities are whole ints.
- **One unified transaction posting algorithm** handles purchase/sale/trade;
  `type` is inferred from which sides carry cards.
- **Cost basis is inferred** by market-value-weighted split of
  `net_consideration_in = cash_out + Σ(cards out) − cash_in`; rounding residual
  goes to the largest-value line. Disposals draw down lots **FIFO** — within the
  cost group an outgoing card names, else across all of that item's lots.
- **Intent** is per-lot: `personal | resale | ripping`. Ripping lots are valued
  at **$0** (shown as a loss = cost basis), never sold.
- **Transactions are never hard-deleted** — soft-void + append-only `audit_log`;
  edit = void + re-post atomically. (One sanctioned exception:
  `delete_inventory_entry` hard-deletes a *mistaken inventory entry* — an unsold
  lot + its now-empty line/txn — writing an audit `delete`; ADR 2026-07-11.)
- Enforce the invariants in [`specs/02-data-model.md`](specs/02-data-model.md#invariants-must-always-hold).

## Testing rules

Project-wide, and deliberately kept out of the section above: the eight rules
sealed against those bullets all break at once when that block is edited, and
these two are about the suite rather than the architecture.

- **A test must fail if the behaviour it covers regresses.** Asserting the setup
  does not count, and neither does a test that still passes against a broken
  implementation. The check is "would this go red on revert?"
- **A new validator, guard, lint or gate ships with a test proving it fails on
  bad input.** A `check` subcommand exercised only against the passing case has
  not been tested; it has been demonstrated.

## Working in parallel sessions

Several Claude sessions work this repo at once, and **a checkout has exactly one
`HEAD`**. Since the move to a branch + PR flow, that makes the shared working tree
a resource two sessions cannot both hold.

**If you are going to commit, take your own worktree first — before any edit, not
after noticing a neighbour.** Call `EnterWorktree`; this paragraph is the project
instruction that authorises it. A session that only reads can stay in the main
clone.

```bash
# what EnterWorktree does, if you need it by hand
git worktree add -b the-thing-you-are-doing ../tcgi-thing main
```

- Run the venv from the **main clone** (`.venv/Scripts/python.exe`) with
  `PYTHONPATH=<worktree>/src`, or the editable install resolves back to the main
  clone and you will verify the wrong tree.
- Worktrees isolate code, not the app's SQLite files. Set
  `TCGINVENTORY_DATA_DIR` before booting the app so two sessions cannot write one
  ledger; the lease records a suggested path.
- Remove it when done: `git worktree remove --force <path>`.

This is **enforced**, not advised. `.claude/hooks/branch_lease.py` runs as a
`PreToolUse` hook and, when another live session holds the same checkout, denies
the commands that would move its `HEAD` or sweep its files into your commit —
`checkout`, `switch`, `reset`, `rebase`, `merge`, `stash`, `git add -A`, and
`git commit` without a pathspec. Alone in your own worktree, none of that fires.

While you are sharing a tree, the safe forms still work: `git add <path>` and
`git commit -m "…" -- <path>`. Stage only your own files, by explicit path, never
`git add -A` — on 2026-08-03 an 831-line feature landed under another session's
commit message that way, and undoing it needs a history rewrite that races their
live commits.

Inspect and unstick the registry with:

```bash
python .claude/hooks/branch_lease.py show          # who holds what
python .claude/hooks/branch_lease.py release --all # the escape hatch
```

Review verdicts are bound to the session that recorded them, so a panel run by
another session will not open your gate — see `/sdd-ticket`.

## Dev environment

- **Python 3.10+.** macOS: 3.10.10 via pyenv. Windows: 3.12.10 via winget
  (`winget install Python.Python.3.12`, at
  `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`).
- A local `.venv` is gitignored — recreate it on a new machine:

```bash
# POSIX (macOS/Linux), from repo root with a 3.10+ interpreter
python -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e ".[dev]"
```

```powershell
# Windows (PowerShell), from repo root
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

- **Run:** `.venv/bin/python -m tcginventory` (POSIX) /
  `.\.venv\Scripts\python.exe -m tcginventory` (Windows) — launches the GUI.
- **Test:** `python -m pytest` from the venv. **Lint/format:**
  `ruff check .` and `ruff format .` (venv's `ruff`).
- **Headless GUI checks:** set `QT_QPA_PLATFORM=offscreen`. DB path overrides:
  `TCGINVENTORY_DATA_DIR` / `TCGINVENTORY_DB`.

## Layout

```
src/tcginventory/  domain/ (enums, dataclasses)  data/ (sqlite, migrations,
                   repos)  services/ (business logic)  ui/ (PySide6)
tests/             pytest (domain + services headless; offscreen UI smoke)
```

## Working style (preference)

For open-ended "how should we model X" questions, discuss tradeoffs in prose and
give a recommendation — don't force a multiple-choice pick-list. Reserve
pick-lists for crisp, mutually-exclusive choices (framework, per-lot vs
per-item). Record significant decisions in `docs/decisions.md`.

## Cost discipline

Agents are the expensive part of this repo, and they are expensive in a way that
is invisible while it is happening. On 2026-08-07 a two-file thread-lifetime fix
cost roughly 560k tokens in critic agents alone, and about two thirds of that
bought nothing.

**The critic panel is opt-in — never dispatch it unless asked.** Critics run when
the user says "run the critics", not on every merge. Recommend one in a sentence
if a change looks like it warrants review; that costs nothing, where four agents
cost ~100k. A panel that *was* run still binds: a recorded `block` stops the merge
until it is fixed or deliberately cleared.

**When you are asked, run it once, on a finished branch.** A verdict is bound to
a sha, so every push discards a completed panel. Land every fix, push, confirm
you are done editing, *then* dispatch. If a `revise` forces another commit, batch
all outstanding fixes into it and re-judge **only the lanes whose files changed** —
`review_gate.py record --inherit <sha>` carries the rest forward.

**Ask the registry who to spawn.** `rules.py scope <changed files>` names the
lanes a diff can actually violate and prints a `dispatch:` line. Spawn exactly
that; if it reports nothing in scope, spawn nothing. See
[`/sdd-ticket`](.claude/skills/sdd-ticket/SKILL.md) § The review gate.

**Critics read; they do not rebuild.** Each is capped at 12 tool calls and told
not to run the suite, build a worktree or launch the app — the author did that
before dispatching. Every real finding the panel has produced came from reading
code. If something can only be settled by running it, that is a `revise` naming
the experiment, not the agent doing it.

**Match the tier to the diff.** `tcgi-mini-critic` covers all four lenses in one
spawn for anything under ~150 changed lines with no migration and no money path.
Four independent agents are for changes that earn them.

**Read what you need, not what you might need.** A dossier's file list is a map,
not a reading list — `history.py` is 1,750 lines and reading it whole twice cost
more than the feature did. Prefer the class and the methods a gotcha names.

The economy here is not "use fewer agents"; it is **don't pay twice for the same
judgement**. A panel that catches a real regression is worth what it costs — the
one on 2026-08-07 caught a genuine one. Running it three times over an unchanged
diff is not.
