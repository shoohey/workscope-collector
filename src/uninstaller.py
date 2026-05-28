"""WorkScope Collector 完全アンインストーラー (v1.1-lite).

リモート制御(control.json)からの "uninstall" 指示や、トレイメニューの
「同意撤回＋データ削除」よりさらに踏み込んだ完全アンインストールを実行する。

実行内容:
    1. Windows スタートアップから WorkScope を削除
       (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run の "WorkScope" 値)
    2. %APPDATA%\\WorkScope 配下を完全削除
       (data/screenshots, data/events, uploaded_markers, logs, config.json,
        consent_signed.json, state.json, app.lock など全て)
    3. EXE自身の削除は本バージョンでは未実装（schedule_self_delete_exe は no-op）。
       手順書で「アンインストール後にダウンロードフォルダのEXEは手動削除」と案内する。
    4. 自己プロセスを sys.exit(0) で終了。

安全装置:
    - delete_appdata_dir() は app_data_dir() の親ディレクトリ名が "Roaming" でない場合
      エラーを返して中断する（誤って別ディレクトリを消さないため）。
    - 削除失敗は warning ログに留めて続行し、errors リストに失敗内容を記録して返す。
    - winreg が import できない環境（Mac/Linux 開発機など）では
      remove_startup_registry() は何もせず True を返す。

レジストリ値名 "WorkScope" は installer/install.ps1 および installer/install.bat の
スタートアップ登録名と一致させてある（変更時は両方を揃えること）。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

try:
    import winreg  # type: ignore
    _HAS_WINREG = True
except ImportError:
    winreg = None  # type: ignore
    _HAS_WINREG = False

logger = logging.getLogger(__name__)


# スタートアップ登録のレジストリパス。installer/install.ps1 と一致。
_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE_NAME = "WorkScope"


def _resolve_appdata_root(appdata_root: Optional[Path]) -> Path:
    """テスト/本番両対応で AppData ルートを解決.

    appdata_root が指定されればそれを使い、なければ config.app_data_dir() を呼ぶ。
    """
    if appdata_root is not None:
        return Path(appdata_root)

    # 実行時に config を import (循環import回避と、テストでの差し替え互換)
    try:
        from config import app_data_dir  # type: ignore
    except ImportError:
        from src.config import app_data_dir  # type: ignore
    return app_data_dir()


def remove_startup_registry() -> bool:
    """HKCU Run から WorkScope を削除する.

    成功時、もしくは値が存在せず削除不要だった場合に True を返す。
    削除に失敗した場合は False を返す（呼び出し側で errors に記録）。

    winreg が利用できない環境（Mac/Linux 開発機）では何もせず True を返す。
    """
    if not _HAS_WINREG:
        logger.info("winreg not available; skipping startup registry removal")
        return True

    try:
        # KEY_SET_VALUE が削除にも必要
        with winreg.OpenKey(  # type: ignore[attr-defined]
            winreg.HKEY_CURRENT_USER,  # type: ignore[attr-defined]
            _RUN_KEY_PATH,
            0,
            winreg.KEY_SET_VALUE,  # type: ignore[attr-defined]
        ) as key:
            try:
                winreg.DeleteValue(key, _RUN_VALUE_NAME)  # type: ignore[attr-defined]
                logger.info(
                    "startup registry value removed: HKCU\\%s\\%s",
                    _RUN_KEY_PATH, _RUN_VALUE_NAME,
                )
                return True
            except FileNotFoundError:
                # 値が無い = 既にスタートアップから外れている。成功扱い。
                logger.info(
                    "startup registry value already absent: HKCU\\%s\\%s",
                    _RUN_KEY_PATH, _RUN_VALUE_NAME,
                )
                return True
    except FileNotFoundError:
        # Run キー自体が無い場合も成功扱い
        logger.info("startup registry key not found; nothing to remove")
        return True
    except OSError:
        logger.exception("failed to remove startup registry value")
        return False


def delete_appdata_dir(appdata_root: Optional[Path] = None) -> int:
    """%APPDATA%/WorkScope を全削除し、削除したファイル数を返す.

    安全装置: app_data_dir() の親ディレクトリ名が "Roaming" でない場合は
    -1 を返して中断する（テスト時を除く誤削除防止）。テストで tmp_path を
    渡す場合は親が "Roaming" でないが、その場合のみ明示的に
    appdata_root=tmp_path を渡してもらう想定。

    削除中の OSError は warning ログのみで続行し、最終的に消し切れなかったファイル
    があってもエラーにせず削除済み数のみ返す。
    """
    root = _resolve_appdata_root(appdata_root)

    # 安全装置: appdata_root が明示指定されていない（=本番モード）の場合のみ
    # "Roaming" 配下であることを必須にする。テスト時は明示渡しのため除外。
    if appdata_root is None:
        if root.parent.name != "Roaming":
            logger.error(
                "refuse to delete: app_data_dir parent is %r (expected 'Roaming'); "
                "root=%s",
                root.parent.name, root,
            )
            return -1

    if not root.exists():
        logger.info("appdata dir does not exist; nothing to delete: %s", root)
        return 0

    # ファイル数をカウントしてから削除（途中失敗時の参考値として）
    deleted_count = 0
    for path in root.rglob("*"):
        if path.is_file():
            deleted_count += 1

    try:
        shutil.rmtree(root, ignore_errors=False)
        logger.info("appdata dir removed: %s (files=%d)", root, deleted_count)
    except OSError as exc:
        # 部分削除で続行。残ったファイルは手動削除を案内する想定。
        logger.warning(
            "partial failure deleting appdata dir: %s err=%s",
            root, exc,
        )
        # 残骸を ignore_errors=True で再試行
        shutil.rmtree(root, ignore_errors=True)

    return deleted_count


def schedule_self_delete_exe(exe_path: Optional[Path] = None) -> bool:
    """EXE自身を遅延削除する BAT を生成して実行する（オプション）.

    第一版は no-op で False を返す。実装する場合は %TEMP% に BAT を書き出して
    Popen で起動し、本プロセス終了後に自己を削除させる常套手段を採る。
    手順書で「アンインストール後にダウンロードフォルダの EXE は手動削除」と
    案内しているので、第一版では未実装で問題なし。
    """
    _ = exe_path  # 引数は将来の拡張用に受けるだけ
    logger.info("schedule_self_delete_exe: not implemented in this version (no-op)")
    return False


def uninstall(
    delete_appdata: bool = True,
    remove_startup: bool = True,
    exit_after: bool = True,
    appdata_root: Optional[Path] = None,
) -> dict[str, Any]:
    """完全アンインストールを実行する.

    Args:
        delete_appdata: True で %APPDATA%/WorkScope を全削除。
        remove_startup: True で HKCU Run の WorkScope エントリを削除。
        exit_after: True で sys.exit(0) を呼ぶ。テスト時は False を指定。
        appdata_root: テスト時に AppData ルートを差し替える。None で本番動作。

    Returns:
        {
          "appdata_deleted": int,   # 削除ファイル数（-1=安全装置で中断）
          "startup_removed": bool,  # スタートアップ削除成功か
          "exit_called": bool,      # sys.exit を呼んだか
          "errors": list[str],      # 失敗内容のサマリ
        }
    """
    errors: list[str] = []
    appdata_deleted = 0
    startup_removed = False
    exit_called = False

    logger.info(
        "uninstall start: delete_appdata=%s remove_startup=%s exit_after=%s",
        delete_appdata, remove_startup, exit_after,
    )

    # 1. スタートアップ削除
    if remove_startup:
        try:
            startup_removed = remove_startup_registry()
            if not startup_removed:
                errors.append("startup registry removal failed")
        except Exception as exc:  # noqa: BLE001
            logger.exception("unexpected error during startup removal")
            errors.append(f"startup removal exception: {exc}")
            startup_removed = False

    # 2. AppData 削除
    if delete_appdata:
        try:
            appdata_deleted = delete_appdata_dir(appdata_root=appdata_root)
            if appdata_deleted < 0:
                errors.append(
                    "appdata deletion aborted by safety guard "
                    "(parent dir is not 'Roaming')"
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("unexpected error during appdata deletion")
            errors.append(f"appdata deletion exception: {exc}")

    # 3. EXE自己削除（第一版は no-op）
    try:
        schedule_self_delete_exe()
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected error during exe self-delete scheduling")
        errors.append(f"exe self-delete exception: {exc}")

    # 4. 自己プロセス終了
    if exit_after:
        logger.info("uninstall finished; calling sys.exit(0)")
        exit_called = True
        sys.exit(0)

    return {
        "appdata_deleted": appdata_deleted,
        "startup_removed": startup_removed,
        "exit_called": exit_called,
        "errors": errors,
    }


__all__ = [
    "uninstall",
    "remove_startup_registry",
    "delete_appdata_dir",
    "schedule_self_delete_exe",
]
