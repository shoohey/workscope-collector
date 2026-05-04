# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for WorkScope Collector (Windows, single-file, no console)."""

block_cipher = None

a = Analysis(
    ['src/main.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        # PaddleOCR resources are pulled at runtime; keep spec minimal
    ],
    hiddenimports=[
        'pystray._win32',
        'PIL._tkinter_finder',
        'paddleocr',
        'paddle',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'pandas', 'jupyter', 'notebook',
        'pytest', 'sphinx',
    ],
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
    name='WorkScope',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='installer/workscope.ico' if __import__('os').path.exists('installer/workscope.ico') else None,
)
