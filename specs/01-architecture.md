# 01 — Architecture

## Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.10+ | Floor is 3.10 (PEP 604 unions, `match`); tested through 3.12. Type hints throughout |
| GUI | PySide6 (Qt 6) | Qt Widgets + Model/View for tables |
| Persistence | SQLite (via `sqlite3` stdlib) | Single local file, WAL mode |
| Migrations | Hand-rolled versioned scripts | `schema_version` table (see below) |
| Money | `decimal.Decimal`, stored as integer cents | Never use `float` for money |
| Packaging | PyInstaller | Produce `.app` / `.exe` later |
| Testing | `pytest` | Focus on domain/service layer |
| Lint/format | `ruff` + `ruff format` | Enforced in CI later |

### Why these

- **PySide6** gives first-class table/tree widgets and the Model/View
  framework, which fits an inventory grid with sorting/filtering far better than
  Tkinter. LGPL license is fine for personal and commercial use.
- **SQLite** is zero-config, transactional, and the DB is a single portable
  file the user can back up or move. `sqlite3` is in the stdlib.
- **Integer cents + `Decimal`** avoids floating-point rounding errors in money
  math, which is core to correctness.

## Layering

The app is split so the **domain and data layers have no Qt dependency** and can
be unit-tested headlessly. The GUI is a thin presentation layer over services.

```
┌─────────────────────────────────────────────┐
│  UI layer (PySide6)                           │
│  windows, dialogs, Qt table models, widgets   │
└───────────────▲───────────────────────────────┘
                │ calls services, receives DTOs / signals
┌───────────────┴───────────────────────────────┐
│  Service layer (pure Python)                   │
│  InventoryService, TransactionService,         │
│  ReportingService, ImportExportService         │
│  — enforces business rules, cost-basis logic   │
└───────────────▲───────────────────────────────┘
                │ repository interfaces
┌───────────────┴───────────────────────────────┐
│  Data-access layer                             │
│  repositories, SQLite connection, migrations   │
└───────────────▲───────────────────────────────┘
                │
┌───────────────┴───────────────────────────────┐
│  SQLite database file (inventory.db)           │
└────────────────────────────────────────────────┘
```

- **Domain models** are plain dataclasses (no Qt, no SQL).
- **Repositories** own all SQL; services depend on repository interfaces.
- **Services** hold business logic (cost basis, P/L, validation) and expose a
  clean API to the UI. All money-affecting operations run inside a single DB
  transaction.
- **UI** never issues SQL and never computes money math itself.

## Proposed project layout

```
TCGinventory/
├── specs/                     # planning baseline (this folder)
├── docs/                      # personal operating records
├── src/
│   └── tcginventory/
│       ├── __init__.py
│       ├── app.py             # entry point, wires everything up
│       ├── config.py          # paths, settings, DB location
│       ├── domain/            # dataclasses + enums (no deps)
│       │   ├── models.py
│       │   └── enums.py
│       ├── data/              # SQLite + repositories + migrations
│       │   ├── db.py
│       │   ├── migrations/
│       │   └── repositories.py
│       ├── services/          # business logic
│       │   ├── inventory.py
│       │   ├── transactions.py
│       │   ├── reporting.py
│       │   └── importexport.py
│       └── ui/                # PySide6 presentation
│           ├── main_window.py
│           ├── models/        # Qt table/list models
│           ├── views/         # dashboard, transaction, inventory, history, item detail
│           └── dialogs/       # edit item, void/edit confirm, backup/import
├── tests/                     # pytest (domain + services)
├── pyproject.toml
└── README.md
```

## Data storage & migrations

- DB path resolved via `config.py` using an OS-appropriate app-data dir
  (e.g. `~/Library/Application Support/TCGinventory/` on macOS), overridable by
  env var for dev/tests.
- SQLite opened in **WAL mode** with foreign keys enforced (`PRAGMA
  foreign_keys = ON`).
- A `schema_version` table records the current version. On startup the app runs
  any pending numbered migration scripts (`0001_init.sql`, `0002_*.sql`, …)
  inside a transaction. Migrations are **forward-only** and additive where
  possible.
- Every write that touches money or quantities is wrapped in a transaction so
  inventory and the transaction log can never diverge.

## Error handling & backups

- Services raise typed domain exceptions (e.g. `InsufficientQuantityError`);
  the UI maps them to friendly dialogs.
- A "Backup database" action copies the SQLite file (and CSV export) to a
  user-chosen location. Automatic timestamped backup on app start is a
  candidate for V1.1.

## Testing strategy

- **Unit tests** cover the service layer, especially cost-basis and P/L math,
  against an in-memory SQLite DB — no Qt required.
- **Smoke tests** instantiate key dialogs/windows via `pytest-qt` (optional,
  later).
- Golden-file tests for CSV import/export round-tripping.
