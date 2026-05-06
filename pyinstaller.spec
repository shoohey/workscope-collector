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
import hashlib
import os
import re

block_cipher = None


def _ascii_safe_customer_name(raw: str) -> str:
    """顧客名を Windows ファイル名に使える ASCII 文字列へ.

    AppVeyor の Windows PowerShell は UTF-8 env vars を `?` に文字化けする
    既知問題があり、`?` は Windows のファイル名で禁止文字 → ビルド失敗.
    対処として:
      1. ASCII 英数字+記号のみ抽出
      2. 何も残らない (= 全部非ASCII or 全部 `?`) なら MD5 先頭8文字を使う
      3. 結果を 30 文字以内に切る (path長対策)
    """
    if not raw:
        return ''
    safe = re.sub(r'[^A-Za-z0-9_\-]', '', raw)
    if not safe:
        # 元値から ASCII が完全に取れない場合 (大森薬局 → '' / ???? → '')
        # ハッシュベースのフォールバック
        h = hashlib.md5(raw.encode('utf-8', errors='replace')).hexdigest()[:8]
        safe = f'cust{h}'
    return safe[:30]


# EXE 名の動的決定 (AppVeyor の env vars と build_for_customer.sh の両方から制御可能)
_customer_raw = (os.environ.get('CUSTOMER_NAME') or '').strip()
_customer_safe = _ascii_safe_customer_name(_customer_raw)
_build_date = datetime.datetime.now().strftime('%Y%m%d')
EXE_NAME = f'WorkScope_{_customer_safe}_{_build_date}' if _customer_safe else 'WorkScope'

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
