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

## Source of truth

- **`specs/`** — what we're building and why. Read [`specs/02-data-model.md`](specs/02-data-model.md)
  before touching money/schema logic; it's the heart of the app.
- **`docs/decisions.md`** — chronological decision log (ADR-style). Skim this
  first to understand *why* things are the way they are.

## Non-negotiable architecture rules

- **Domain + service layers are Qt-free** and unit-testable headless. The UI
  never issues SQL and never does money math.
- **Money is integer cents** (`int`), never `float`. Quantities are whole ints.
- **One unified transaction posting algorithm** handles purchase/sale/trade;
  `type` is inferred from which sides carry cards.
- **Cost basis is inferred** by market-value-weighted split of
  `net_consideration_in = cash_out + Σ(cards out) − cash_in`; rounding residual
  goes to the largest-value line. Disposals draw down lots **FIFO**.
- **Intent** is per-lot: `personal | resale | ripping`. Ripping lots are valued
  at **$0** (shown as a loss = cost basis), never sold.
- **Transactions are never hard-deleted** — soft-void + append-only `audit_log`;
  edit = void + re-post atomically. (One sanctioned exception:
  `delete_inventory_entry` hard-deletes a *mistaken inventory entry* — an unsold
  lot + its now-empty line/txn — writing an audit `delete`; ADR 2026-07-11.)
- Enforce the invariants in [`specs/02-data-model.md`](specs/02-data-model.md#invariants-must-always-hold).

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
