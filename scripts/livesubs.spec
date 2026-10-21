# PyInstaller spec: a one-folder build (dist/LiveSubs/LiveSubs.exe).
# One folder, not one file: a one-file exe unpacks itself to %TEMP% on every start.
# Build with scripts/build_exe.ps1.

from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller
SRC = ROOT / "src"

a = Analysis(  # noqa: F821
    [str(SRC / "livesubs" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=[
        # Only used by tests and dev scripts.
        "tkinter",
        "pytest",
        "pytestqt",
        "respx",
        "IPython",
        "matplotlib",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LiveSubs",
    console=False,  # a desktop app: no terminal window
    disable_windowed_traceback=False,
    upx=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    name="LiveSubs",
    upx=False,
)
