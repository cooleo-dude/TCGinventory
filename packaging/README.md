# Packaging

Build a distributable desktop app with [PyInstaller](https://pyinstaller.org).

## Build (from the repo root, in the project venv)

```bash
pip install -e ".[dev]"          # includes pyinstaller
pyinstaller --clean --noconfirm packaging/tcginventory.spec
```

Output: `dist/TCGinventory/` — a folder containing `TCGinventory.exe` (Windows)
and its `_internal/` runtime. Zip the folder to distribute; it runs on a clean
machine with no Python installed.

- **Single-file exe:** set `ONEFILE = True` at the top of
  [`tcginventory.spec`](tcginventory.spec) (slower to start; unpacks to a temp
  dir on launch).
- **macOS:** the same spec builds a `.app`/binary when run on macOS.

## What the spec bundles

- The SQL migrations (`tcginventory/data/migrations/*.sql`), read at startup.
- The app icon (`tcginventory/ui/assets/icon.*`).
- PySide6 + Qt (via PyInstaller's bundled hooks); `QtCharts` is pinned as a
  hidden import for the dashboard.

`entry.py` is the frozen entry point — equivalent to `python -m tcginventory`.

## First run

The app resolves its data directory per-OS (overridable via
`TCGINVENTORY_DATA_DIR` / `TCGINVENTORY_DB`), creates `inventory.db`, applies
migrations, and writes a timestamped startup backup under `backups/`.
