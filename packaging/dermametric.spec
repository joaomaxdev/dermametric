# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec do DermaMetric (onedir + windowed).

Build local (Windows)::

    pyinstaller packaging/dermametric.spec --noconfirm

Resultado: ``dist/DermaMetric/DermaMetric.exe``.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).resolve().parent
ICON = ROOT / "packaging" / "dermametric.ico"

datas = [
    (str(ROOT / "frontend"), "frontend"),
    (str(ICON), "packaging"),
]

# Relatórios (reportlab) precisam das fontes/afm empacotadas.
datas += collect_data_files("reportlab")
datas += collect_data_files("certifi")

hiddenimports = [
    # Uvicorn: resolvido dinamicamente em runtime.
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # Multipart / uploads.
    "multipart",
    "python_multipart",
    # Visão computacional e relatórios.
    "cv2",
    "numpy",
    "reportlab",
    "reportlab.pdfgen.canvas",
    "reportlab.platypus",
    "reportlab.lib.pagesizes",
    "cryptography",
    "cryptography.fernet",
    "cryptography.hazmat.primitives.kdf.pbkdf2",
    # Certificados CA para HTTPS no Windows compilado (GitHub Releases).
    "certifi",
    # Interface desktop.
    "webview",
]

if sys.platform.startswith("win"):
    hiddenimports += [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr_loader",
        "pythonnet",
    ]
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]
else:
    hiddenimports += ["webview.platforms.gtk", "webview.platforms.qt"]


a = Analysis(
    [str(ROOT / "desktop_launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DermaMetric",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DermaMetric",
)
