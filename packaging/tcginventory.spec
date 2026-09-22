# PyInstaller spec for TCGinventory (onedir build).
#
# Build from the repo root:
#     pyinstaller --clean --noconfirm packaging/tcginventory.spec
# Produces dist/TCGinventory/ (a distributable folder; zip it to share).
# For a single-file exe, set ONEFILE = True below.

import glob
import os

from PyInstaller.utils.hooks import collect_dynamic_libs

ONEFILE = False

spec_dir = SPECPATH  # noqa: F821 - injected by PyInstaller (dir of this .spec)
root = os.path.dirname(spec_dir)
src = os.path.join(root, "src")
assets = os.path.join(src, "tcginventory", "ui", "assets")
icon_ico = os.path.join(assets, "icon.ico")

datas = [
    (sql, "tcginventory/data/migrations")
    for sql in glob.glob(os.path.join(src, "tcginventory", "data", "migrations", "*.sql"))
]
datas += [
    (os.path.join(assets, "icon.png"), "tcginventory/ui/assets"),
    (icon_ico, "tcginventory/ui/assets"),
]
# Bundled animated GIFs (e.g. the Scrafty nav mascot).
datas += [
    (gif, "tcginventory/ui/assets")
    for gif in glob.glob(os.path.join(assets, "*.gif"))
]
# Bundled UI fonts (IBM Plex Sans/Mono) — the Neon Slate theme registers these
# at startup; ship them so the frozen app looks identical without system installs.
datas += [
    (font, "tcginventory/ui/assets/fonts")
    for font in glob.glob(os.path.join(assets, "fonts", "*.ttf"))
]
# Bundled Lucide SVG icons (recoloured to the theme at runtime via QtSvg).
datas += [
    (svg, "tcginventory/ui/assets/icons")
    for svg in glob.glob(os.path.join(assets, "icons", "*.svg"))
]
# DINOv2 identifier weights (LFS in the repo, ~88 MB) — bundled so the frozen
# app is fully offline and self-contained.
datas += [
    (model, "tcginventory/models")
    for model in glob.glob(os.path.join(src, "tcginventory", "models", "*.onnx"))
]
# Whisper base.en token table (ticket 76) — the encoder/decoder are caught by the
# *.onnx glob above; the vocabulary is a text file and needs its own.
datas += [
    (tokens, "tcginventory/models")
    for tokens in glob.glob(os.path.join(src, "tcginventory", "models", "*.txt"))
]
# The training worker — a standalone script the app runs in a *separate*
# interpreter that has ultralytics (torch is ~2 GB and deliberately unbundled).
# Shipped as data, not code: it must exist as a real .py on disk for another
# interpreter to execute, and nothing in the app imports it.
datas += [
    (
        os.path.join(spec_dir, "..", "training", "yolo_train_worker.py"),
        "tcginventory/training",
    )
]

a = Analysis(
    [os.path.join(spec_dir, "entry.py")],
    pathex=[src],
    # sherpa-onnx keeps its C API DLLs (and its own onnxruntime.dll) beside the
    # extension module under sherpa_onnx/lib/; collect them there so the .pyd
    # finds them where it expects, and so they never shadow the onnxruntime
    # package's own copy.
    binaries=collect_dynamic_libs("sherpa_onnx"),
    datas=datas,
    # httpx is imported lazily (in the price fetcher), so name it and its
    # dependency chain explicitly to guarantee they're bundled.
    hiddenimports=[
        "PySide6.QtCharts",
        "PySide6.QtSvg",
        "httpx",
        "httpcore",
        "h11",
        "certifi",
        "anyio",
        "sniffio",
        "idna",
        # Vision identifier — recognizer is imported lazily by the UI, so name it
        # (and onnxruntime) explicitly to pull the numpy/PIL/ort chain in.
        "tcginventory.services.recognizer",
        "onnxruntime",
        # Voice search (ticket 76) — the Whisper runtime is imported inside a
        # try in services/voice.py, so name it and its extension explicitly.
        "sherpa_onnx",
        "sherpa_onnx.lib._sherpa_onnx",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="TCGinventory",
        console=False,
        icon=icon_ico,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="TCGinventory",
        console=False,
        icon=icon_ico,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        name="TCGinventory",
    )
