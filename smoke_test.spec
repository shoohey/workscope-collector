# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for WorkScope SmokeTest (Windows, single-file, with console).

Built alongside the main WorkScope.exe to ship as a verifier tool.
Customer flow: ダウンロード → smoke_test を先にダブルクリック → 全部✅なら本体起動.
"""

import glob
import os

block_cipher = None

# 本体EXEと同じく docs/ と profiles/ を同梱（smoke_test も同じパスで参照）
_data_files = []
for _p in glob.glob(os.path.join('docs', '*.html')):
    _data_files.append((_p, 'docs'))
for _p in glob.glob(os.path.join('profiles', '*.json')):
    _data_files.append((_p, 'profiles'))
# src/app_rules.json も同梱 (_MEIPASS 直下に配置. app_classifier が
# _MEIPASS/app_rules.json を探すため)
if os.path.exists('src/app_rules.json'):
    _data_files.append(('src/app_rules.json', '.'))

a = Analysis(
    ['scripts/smoke_test.py'],
    pathex=['src'],
    binaries=[],
    datas=_data_files,
    hiddenimports=[
        'PIL._tkinter_finder',
        # SmokeTest が直接は使わないが、check_windows_bindings() で
        # import を試すため、本体と同じ Windows 依存を同梱しておく.
        'win32gui',
        'win32process',
        'win32con',
        'win32api',
        'pywintypes',
        'mss',
        'mss.tools',
        'psutil',
        'psutil._pswindows',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'pandas', 'jupyter', 'notebook',
        'pytest', 'sphinx', 'paddleocr', 'paddle',  # smoke testではOCR不要
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
    name='WorkScope_SmokeTest',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # smoke_test はコンソール出力も見せる（トラブル時の切分け用）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
