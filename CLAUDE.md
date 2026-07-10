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
- **Next: M4** (write UI — the unified Transaction window) — see [`specs/05-roadmap.md`](specs/05-roadmap.md).

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
  edit = void + re-post atomically.
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
