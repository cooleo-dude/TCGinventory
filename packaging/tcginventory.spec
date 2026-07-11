# PyInstaller spec for TCGinventory (onedir build).
#
# Build from the repo root:
#     pyinstaller --clean --noconfirm packaging/tcginventory.spec
# Produces dist/TCGinventory/ (a distributable folder; zip it to share).
# For a single-file exe, set ONEFILE = True below.

import glob
import os

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

a = Analysis(
    [os.path.join(spec_dir, "entry.py")],
    pathex=[src],
    binaries=[],
    datas=datas,
    # httpx is imported lazily (in the price fetcher), so name it explicitly;
    # PyInstaller then follows its deps (httpcore, certifi, anyio, …).
    hiddenimports=["PySide6.QtCharts", "httpx"],
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
