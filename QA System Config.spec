# -*- mode: python ; coding: utf-8 -*-
# GhostConfig — PyInstaller build spec
# Build:  pyinstaller "GhostConfig.spec"  (run from qa_system_config/)

block_cipher = None

a = Analysis(
    ['gui.py'],                          # entry point is the GUI, not config_utility
    pathex=['.'],
    binaries=[],
    datas=[
        ('trial_limits.json', '.'),      # bundled next to the exe in dist/
        ('ghost_updater.py',  '.'),      # auto-update helper — must sit next to GhostConfig.exe
    ],
    hiddenimports=[
        'config_utility',                # bundled alongside gui.py
        'keygen',                        # key management module
        'license_manager',               # role-based permission system
        'devices',                       # hardware info collectors
        'activity_log',                  # login / registration activity logger
        'winreg',
        'ctypes',
        'ctypes.wintypes',
        'tkinter',
        'tkinter.ttk',
        'tkinter.font',
        'tkinter.messagebox',
        'tkinter.scrolledtext',
        'tkinter.simpledialog',
        'psutil',                        # Task Manager page
        'psutil._pswindows',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GhostConfig',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                       # windowed — no console popup
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Request UAC elevation so the exe always runs as Administrator
    uac_admin=True,
)
