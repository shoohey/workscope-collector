"""uninstaller.py のテスト. 完全アンインストール処理を検証.

winreg は実 OS に副作用を出さないようモックする。
AppData ルートは tmp_path を引数で直接渡すことで隔離する。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import uninstaller  # noqa: E402


# ---- 1. delete_appdata_dir: tmp_path 配下を全削除し件数を返す ------------

def test_delete_appdata_dir_removes_all_files(tmp_path):
    """tmp_path/WorkScope 配下の全ファイル/ディレクトリを削除し、ファイル数を返す."""
    root = tmp_path / "WorkScope"
    (root / "data" / "screenshots").mkdir(parents=True)
    (root / "data" / "events").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    (root / "data" / "screenshots" / "a.jpg").write_bytes(b"x")
    (root / "data" / "screenshots" / "b.jpg").write_bytes(b"y")
    (root / "data" / "events" / "2026-05-28.jsonl").write_text("{}\n")
    (root / "logs" / "main.log").write_text("ok\n")
    (root / "consent_signed.json").write_text("{}")
    (root / "config.json").write_text("{}")

    count = uninstaller.delete_appdata_dir(appdata_root=root)

    assert count == 6  # 6 ファイル
    assert not root.exists()


def test_delete_appdata_dir_returns_zero_when_missing(tmp_path):
    """対象ディレクトリが存在しなくても 0 を返してエラーにしない."""
    nonexistent = tmp_path / "WorkScope-missing"
    count = uninstaller.delete_appdata_dir(appdata_root=nonexistent)
    assert count == 0


# ---- 2. delete_appdata_dir: "Roaming" 配下でない実パスは安全装置で中断 ----

def test_delete_appdata_dir_aborts_when_parent_not_roaming(monkeypatch, tmp_path):
    """appdata_root を省略し、app_data_dir() の親が 'Roaming' でない場合、
    -1 を返して中断する (誤削除防止)."""
    # config.app_data_dir() が tmp_path/WorkScope を返すようにする (親は tmp_path で "Roaming" でない)
    fake_root = tmp_path / "WorkScope"
    fake_root.mkdir()
    (fake_root / "dummy.txt").write_text("must not be deleted")

    monkeypatch.setattr(uninstaller, "_resolve_appdata_root",
                        lambda x: fake_root if x is None else Path(x))

    # appdata_root=None なら安全装置発動
    count = uninstaller.delete_appdata_dir(appdata_root=None)

    assert count == -1
    # ファイルが残っていること（=削除されていない）
    assert (fake_root / "dummy.txt").exists()


def test_delete_appdata_dir_allows_when_parent_is_roaming(monkeypatch, tmp_path):
    """親ディレクトリ名が 'Roaming' の場合は appdata_root=None でも削除実行."""
    roaming = tmp_path / "Roaming"
    root = roaming / "WorkScope"
    root.mkdir(parents=True)
    (root / "x.txt").write_text("ok")

    monkeypatch.setattr(uninstaller, "_resolve_appdata_root",
                        lambda x: root if x is None else Path(x))

    count = uninstaller.delete_appdata_dir(appdata_root=None)
    assert count == 1
    assert not root.exists()


# ---- 3. remove_startup_registry: winreg.DeleteValue が呼ばれる ----------

def test_remove_startup_registry_calls_delete_value(monkeypatch):
    """winreg があるとき DeleteValue が "WorkScope" 値名で呼ばれる."""
    fake_winreg = mock.MagicMock()
    fake_key = mock.MagicMock()
    # OpenKey はコンテキストマネージャ
    fake_winreg.OpenKey.return_value.__enter__.return_value = fake_key
    fake_winreg.OpenKey.return_value.__exit__.return_value = False
    fake_winreg.HKEY_CURRENT_USER = "HKCU_SENTINEL"
    fake_winreg.KEY_SET_VALUE = 0x0002

    monkeypatch.setattr(uninstaller, "_HAS_WINREG", True)
    monkeypatch.setattr(uninstaller, "winreg", fake_winreg, raising=False)

    ok = uninstaller.remove_startup_registry()

    assert ok is True
    fake_winreg.OpenKey.assert_called_once_with(
        "HKCU_SENTINEL",
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        0x0002,
    )
    fake_winreg.DeleteValue.assert_called_once_with(fake_key, "WorkScope")


# ---- 4. remove_startup_registry: 値が無い場合 (FileNotFoundError) も True --

def test_remove_startup_registry_returns_true_when_value_missing(monkeypatch):
    """DeleteValue が FileNotFoundError を投げても True を返す (既に外れている)."""
    fake_winreg = mock.MagicMock()
    fake_key = mock.MagicMock()
    fake_winreg.OpenKey.return_value.__enter__.return_value = fake_key
    fake_winreg.OpenKey.return_value.__exit__.return_value = False
    fake_winreg.DeleteValue.side_effect = FileNotFoundError("value missing")

    monkeypatch.setattr(uninstaller, "_HAS_WINREG", True)
    monkeypatch.setattr(uninstaller, "winreg", fake_winreg, raising=False)

    ok = uninstaller.remove_startup_registry()
    assert ok is True


def test_remove_startup_registry_returns_true_when_run_key_missing(monkeypatch):
    """Run キー自体が無い場合 (OpenKey が FileNotFoundError) も True を返す."""
    fake_winreg = mock.MagicMock()
    fake_winreg.OpenKey.side_effect = FileNotFoundError("Run key missing")

    monkeypatch.setattr(uninstaller, "_HAS_WINREG", True)
    monkeypatch.setattr(uninstaller, "winreg", fake_winreg, raising=False)

    ok = uninstaller.remove_startup_registry()
    assert ok is True


def test_remove_startup_registry_returns_false_on_other_oserror(monkeypatch):
    """予期せぬ OSError(PermissionError 等) は False を返す."""
    fake_winreg = mock.MagicMock()
    fake_winreg.OpenKey.side_effect = PermissionError("denied")

    monkeypatch.setattr(uninstaller, "_HAS_WINREG", True)
    monkeypatch.setattr(uninstaller, "winreg", fake_winreg, raising=False)

    ok = uninstaller.remove_startup_registry()
    assert ok is False


# ---- 5. uninstall(): delete_appdata=False で AppData を消さない ----------

def test_uninstall_skips_appdata_when_delete_appdata_false(tmp_path, monkeypatch):
    """delete_appdata=False の場合は AppData ディレクトリを削除しない."""
    root = tmp_path / "WorkScope"
    root.mkdir()
    (root / "x.txt").write_text("preserved")

    # remove_startup_registry はモック (Mac でも動かすため)
    monkeypatch.setattr(uninstaller, "remove_startup_registry", lambda: True)

    result = uninstaller.uninstall(
        delete_appdata=False,
        remove_startup=True,
        exit_after=False,
        appdata_root=root,
    )

    assert result["appdata_deleted"] == 0
    assert result["startup_removed"] is True
    assert result["exit_called"] is False
    assert result["errors"] == []
    # ファイルが残っていること
    assert (root / "x.txt").exists()


# ---- 6. uninstall(): exit_after=False で sys.exit が呼ばれない -----------

def test_uninstall_does_not_call_exit_when_exit_after_false(tmp_path, monkeypatch):
    """exit_after=False の場合は sys.exit を呼ばずに dict を返す."""
    root = tmp_path / "WorkScope"
    root.mkdir()
    (root / "a.txt").write_text("a")

    monkeypatch.setattr(uninstaller, "remove_startup_registry", lambda: True)

    # sys.exit が呼ばれたら検知できるようパッチ
    exit_called = {"v": False}
    def _fake_exit(code=0):  # noqa: ARG001
        exit_called["v"] = True
        raise SystemExit(code)
    monkeypatch.setattr(uninstaller.sys, "exit", _fake_exit)

    result = uninstaller.uninstall(
        delete_appdata=True,
        remove_startup=True,
        exit_after=False,
        appdata_root=root,
    )

    assert exit_called["v"] is False
    assert result["exit_called"] is False
    assert result["appdata_deleted"] == 1
    assert not root.exists()


def test_uninstall_calls_exit_when_exit_after_true(tmp_path, monkeypatch):
    """exit_after=True の場合は sys.exit(0) が呼ばれる."""
    root = tmp_path / "WorkScope"
    root.mkdir()

    monkeypatch.setattr(uninstaller, "remove_startup_registry", lambda: True)

    fake_exit = mock.MagicMock(side_effect=SystemExit(0))
    monkeypatch.setattr(uninstaller.sys, "exit", fake_exit)

    with pytest.raises(SystemExit) as excinfo:
        uninstaller.uninstall(
            delete_appdata=True,
            remove_startup=True,
            exit_after=True,
            appdata_root=root,
        )

    assert excinfo.value.code == 0
    fake_exit.assert_called_once_with(0)


# ---- 7. _HAS_WINREG=False 環境でも例外なく動作 -------------------------

def test_remove_startup_registry_noop_when_winreg_unavailable(monkeypatch):
    """winreg が import できない環境では何もせず True を返す."""
    monkeypatch.setattr(uninstaller, "_HAS_WINREG", False)
    # winreg 属性は None でも素通り
    monkeypatch.setattr(uninstaller, "winreg", None, raising=False)

    ok = uninstaller.remove_startup_registry()
    assert ok is True


def test_uninstall_works_when_winreg_unavailable(tmp_path, monkeypatch):
    """winreg が無くても uninstall() 全体が例外なく動く (Mac/Linux 開発機想定)."""
    monkeypatch.setattr(uninstaller, "_HAS_WINREG", False)
    monkeypatch.setattr(uninstaller, "winreg", None, raising=False)

    root = tmp_path / "WorkScope"
    root.mkdir()
    (root / "data.txt").write_text("ok")

    result = uninstaller.uninstall(
        delete_appdata=True,
        remove_startup=True,
        exit_after=False,
        appdata_root=root,
    )

    assert result["startup_removed"] is True   # noop でも True 扱い
    assert result["appdata_deleted"] == 1
    assert result["errors"] == []
    assert not root.exists()


# ---- 8. schedule_self_delete_exe は no-op で False --------------------

def test_schedule_self_delete_exe_is_noop():
    """第一版は no-op で False を返す."""
    assert uninstaller.schedule_self_delete_exe() is False
    assert uninstaller.schedule_self_delete_exe(Path("/tmp/dummy.exe")) is False


# ---- 9. uninstall(): エラー集約の確認 ----------------------------------

def test_uninstall_collects_errors_when_startup_fails(tmp_path, monkeypatch):
    """remove_startup_registry が False を返したら errors に記録される."""
    root = tmp_path / "WorkScope"
    root.mkdir()

    monkeypatch.setattr(uninstaller, "remove_startup_registry", lambda: False)

    result = uninstaller.uninstall(
        delete_appdata=False,
        remove_startup=True,
        exit_after=False,
        appdata_root=root,
    )

    assert result["startup_removed"] is False
    assert any("startup" in e for e in result["errors"])


def test_uninstall_collects_errors_when_safety_guard_aborts(tmp_path, monkeypatch):
    """安全装置で AppData 削除が中断されたら errors に記録される."""
    fake_root = tmp_path / "WorkScope"
    fake_root.mkdir()

    # _resolve_appdata_root が "Roaming" 配下でないパスを返す → 安全装置発動
    monkeypatch.setattr(uninstaller, "_resolve_appdata_root",
                        lambda x: fake_root if x is None else Path(x))
    monkeypatch.setattr(uninstaller, "remove_startup_registry", lambda: True)

    result = uninstaller.uninstall(
        delete_appdata=True,
        remove_startup=False,
        exit_after=False,
        appdata_root=None,  # None で安全装置を効かせる
    )

    assert result["appdata_deleted"] == -1
    assert any("safety guard" in e.lower() or "roaming" in e.lower()
               for e in result["errors"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
