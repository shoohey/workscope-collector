# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for WorkScope Collector (Windows, single-file, no console).

EXE 名は環境変数で動的に決まる:
- CUSTOMER_NAME 環境変数あり → WorkScope_<CUSTOMER_NAME>_<YYYYMMDD>.exe
- なし → WorkScope.exe (汎用ビルド・後方互換)

PyInstaller の制約: .spec ファイル指定時は --name コマンドライン引数が
渡せないため、ここ (.spec内 = Pythonコード) で動的決定する.
"""

import datetime
import glob
import os

block_cipher = None

# EXE 名の動的決定 (AppVeyor の env vars と build_for_customer.sh の両方から制御可能)
_customer = (os.environ.get('CUSTOMER_NAME') or '').strip()
_build_date = datetime.datetime.now().strftime('%Y%m%d')
EXE_NAME = f'WorkScope_{_customer}_{_build_date}' if _customer else 'WorkScope'

# 同梱する HTML ドキュメント（同意書・運用ガイド・データ取扱方針 等）
# tray.py の bundled_doc_path() が sys._MEIPASS/docs/<name> を参照する
_doc_files = []
for _p in glob.glob(os.path.join('docs', '*.html')):
    _doc_files.append((_p, 'docs'))

# 業界プロファイル JSON 同梱（profile_loader が sys._MEIPASS/profiles/ を参照）
# 顧客別ビルド時は scripts/build_for_customer.sh が profiles/ を該当業界1ファイルに絞って配置
for _p in glob.glob(os.path.join('profiles', '*.json')):
    _doc_files.append((_p, 'profiles'))

a = Analysis(
    ['src/main.py'],
    pathex=['src'],
    binaries=[],
    datas=_doc_files,
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
        # v1.0: tkinter は同意ダイアログで使用するため exclude しない
        'matplotlib', 'pandas', 'jupyter', 'notebook',
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
    name=EXE_NAME,
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
