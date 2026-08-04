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
- The theme fonts, Lucide icons, mascot GIFs and the ONNX models
  (`tcginventory/models/*.onnx` — DINOv2, plus `card_yolo.onnx` when one has been
  trained and committed).
- **`tcginventory/training/yolo_train_worker.py`** — shipped as *data*, not code:
  the app executes it in a different interpreter, so it has to exist as a real
  `.py` on disk. See below.

`entry.py` is the frozen entry point — equivalent to `python -m tcginventory`.

## Training the card detector from the frozen app

Training needs `ultralytics` and torch. That is deliberately **not** bundled — the
app never needs them to *use* a model, only to make one — so the app drives a
separate interpreter that has them.

Measured on Windows (2026-08-04): **~280 MB of downloads, ~1 GB installed.** The
Windows PyPI `torch` wheel is **CPU-only** (122 MB, no CUDA packages), which is
fine here: yolov8n on a 20-image corpus trains in minutes on CPU.

`onnx`, `onnxslim` and `onnxruntime` are **required** and are *not* part of a plain
`pip install ultralytics` — they sit behind its `export-base` extra, and without
them a run trains to completion and then fails at the ONNX export:

```powershell
# one time, anywhere on the machine
py -m venv C:\trainvenv
C:\trainvenv\Scripts\python.exe -m pip install ultralytics onnx onnxslim onnxruntime
```

`services.training.TRAINING_REQUIREMENTS` is the single source of truth for that
list; `INSTALL_HINT` is the command the app shows.

Then point the app at it — Operations ▸ Models & signals stores the path in
`preferences.json` (`training_python`), or set it from source:

```python
from tcginventory.services import training
training.set_training_python(r"C:\trainvenv\Scripts\python.exe")
```

The app exports the YOLO dataset, opens a row in the `detector_run` log, runs
`yolo_train_worker.py` in that interpreter, then reads back the metrics, hashes
the artifact and decides whether to promote it. The worker imports nothing from
`tcginventory`, so the training venv needs *only* ultralytics — no editable
install, no source tree.

A UI-triggered run writes the model to
`<data_dir>/vision/models/card_yolo.onnx`, which `yolo_model_path()` picks up on
its next lookup. (The package copy under `_internal` is not writable across
upgrades, which is why the data dir wins for runtime-trained models.) To ship a
model *with* the exe, copy it to `src/tcginventory/models/` and track it with Git
LFS; the spec's `models/*.onnx` glob does the rest.

From the command line the same path is available, and it's the one the UI uses:

```bash
python training/train_detector.py --python C:\trainvenv\Scripts\python.exe --epochs 100
```

## First run

The app resolves its data directory per-OS (overridable via
`TCGINVENTORY_DATA_DIR` / `TCGINVENTORY_DB`), creates `inventory.db`, applies
migrations, and writes a timestamped startup backup under `backups/`.
