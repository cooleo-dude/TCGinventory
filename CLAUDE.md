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
- **Next: M1** (data layer & migrations) — see [`specs/05-roadmap.md`](specs/05-roadmap.md).

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

- **Python 3.10+** (developed on 3.10.10 via pyenv). PySide6.
- A local `.venv` is gitignored — recreate it on a new machine:

```bash
# from repo root, with a 3.10+ interpreter (e.g. pyenv's 3.10.10)
python -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e ".[dev]"
```

- **Run:** `.venv/bin/python -m tcginventory` (launches the GUI)
- **Test:** `.venv/bin/python -m pytest`
- **Lint/format:** `.venv/bin/ruff check .` and `.venv/bin/ruff format .`
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
