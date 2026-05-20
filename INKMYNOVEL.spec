# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for INKMYNOVEL Uploader (cross-platform, single file).
#
# Build:
#   python -m PyInstaller --noconfirm --clean INKMYNOVEL.spec
#
# Output:
#   Windows : dist/INKMYNOVEL.exe   (portable single file, windowed)
#   macOS   : dist/INKMYNOVEL.app   (windowed app bundle)
#   Linux   : dist/INKMYNOVEL       (portable single file)
#
# Note: Google Chrome is NOT bundled — it must already be installed on the
# target machine. The matching chromedriver is fetched by Selenium Manager.

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

is_macos = sys.platform == "darwin"
is_windows = sys.platform == "win32"

# macOS build architecture. Set INKMYNOVEL_TARGET_ARCH=universal2 (or x86_64)
# in CI to produce one binary that runs on both Intel and Apple Silicon.
# Unset = native architecture of the build machine.
target_arch = os.environ.get("INKMYNOVEL_TARGET_ARCH") or None

# App version. Single source of truth is the VERSION file at the repo root
# (the CI stamps it from the git tag before building).
app_version = "0.0.0"
try:
    with open("VERSION", encoding="utf-8") as _vf:
        app_version = _vf.read().strip() or "0.0.0"
except OSError:
    pass

# Optional app icon: used only if the file exists under assets/.
icon_path = None
if is_windows and os.path.exists(os.path.join("assets", "icon.ico")):
    icon_path = os.path.join("assets", "icon.ico")
elif is_macos and os.path.exists(os.path.join("assets", "icon.icns")):
    icon_path = os.path.join("assets", "icon.icns")

# Selenium ships the "Selenium Manager" helper binary as package data;
# bundle it so the frozen app can still auto-resolve chromedriver.
datas = collect_data_files("selenium")

# Bundle the VERSION file so the app knows its own version at runtime
# (used for the GitHub release update check).
if os.path.exists("VERSION"):
    datas += [("VERSION", ".")]

# collect_submodules pulls in every selenium submodule. Selenium 4.x
# lazy-loads selenium.webdriver.chrome.* so PyInstaller's static analysis
# misses it on its own, causing "No module named ..." at runtime.
hiddenimports = [
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
] + collect_submodules("selenium")

a = Analysis(
    ["inkxmynovel_pyqt.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PyQt6.QtNetwork",
        "PyQt6.QtQml",
        "PyQt6.QtQuick",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtMultimedia",
        "PyQt6.QtSql",
        "PyQt6.QtTest",
        "PyQt6.Qt3DCore",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="INKMYNOVEL",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # keep off: UPX-packed exes trip some antivirus scanners
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,             # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=target_arch,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)

if is_macos:
    app = BUNDLE(
        exe,
        name="INKMYNOVEL.app",
        icon=icon_path,
        bundle_identifier="com.snibzyz.inkmynovel",
        info_plist={
            "CFBundleName": "INKMYNOVEL",
            "CFBundleDisplayName": "INKMYNOVEL Uploader",
            "CFBundleShortVersionString": app_version,
            "CFBundleVersion": app_version,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
