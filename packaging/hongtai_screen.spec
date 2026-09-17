# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller build spec for the desktop app -- produces a single
# "Hongtai Screen.exe" with no console window, the app icon baked in,
# and assets/icon.ico bundled as a resource (paths.py's
# _resource_path() finds it inside sys._MEIPASS at runtime).
#
# Build (Windows only, from the REPO ROOT -- not from this packaging/
# folder -- so the relative paths below resolve correctly; see
# BUILD.md):
#
#   pip install pyinstaller
#   pyinstaller packaging/hongtai_screen.spec
#
# Output lands in dist\Hongtai Screen.exe -- a single portable file.
# app_config.json is created next to whatever folder you put the exe
# in (see paths.py's _app_base_dir()), so it's fine to move the exe
# around after building; nothing else needs to travel with it -- the
# app package (src/) and icon are both baked in.

import sys

block_cipher = None

hidden_imports = [
    # PyInstaller's static import scanner can miss these -- they're
    # loaded conditionally/lazily by the libraries that use them.
    "pynvml",              # nvidia-ml-py's importable name (dashboard GPU stats)
    "winsdk",
    "winsdk.windows.media.control",
    "winsdk.windows.storage.streams",
    "pystray._win32",      # pystray picks its backend at import time
    "PIL._tkinter_finder",
]

a = Analysis(
    ["app.py"],
    # app.py (the repo-root launcher) does its own sys.path.insert(0,
    # ".../src") before importing hongtai_screen_app -- PyInstaller's
    # static analyzer can't follow that at scan time, so it's told
    # here explicitly instead (paths below are relative to wherever
    # `pyinstaller` is invoked from, i.e. the repo root -- see the
    # build command above).
    pathex=["src"],
    binaries=[],
    datas=[
        ("assets/icon.ico", "assets"),
        # The bundled dashboard background pictures (dashboard_theme.
        # py's BUNDLED_BACKGROUND_IMAGES) -- same "assets" dest dir as
        # the icon above, so paths.py's _resource_path() finds them at
        # sys._MEIPASS/assets/backgrounds/*.jpg the same way it finds
        # icon.ico.
        ("assets/backgrounds/*.jpg", "assets/backgrounds"),
        # The bundled display fonts (dashboard_theme.py's
        # FONT_FAMILIES), resolved through the same resource_path()
        # mechanism. Without these a frozen build silently falls back
        # to whatever generic sans Windows has, which is exactly the
        # "every theme looks the same" problem they were added to fix
        # -- so they're a real build dependency, not decoration.
        ("assets/fonts/*.ttf", "assets/fonts"),
        ("assets/fonts/*-OFL.txt", "assets/fonts"),
        ("assets/fonts/LICENSES.md", "assets/fonts"),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Hongtai Screen",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-compressing a Tkinter/opencv/playwright build
                         # is a common source of false-positive AV flags;
                         # leave it off for a release build
    console=False,       # no console window -- same effect as pythonw.exe
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)
