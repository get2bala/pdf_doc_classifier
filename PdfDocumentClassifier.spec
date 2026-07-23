# PyInstaller recipe: pyinstaller PdfDocumentClassifier.spec
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = collect_data_files("pypdfium2")
binaries = collect_dynamic_libs("pypdfium2")

a = Analysis(
    ["run_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=["PIL._tkinter_finder"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PDF Document Classifier",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
