"""remote_control のテスト.

- google API 依存は import 自体は OK (環境依存)。
  Drive アクセス系メソッドは fetch_now/apply_control 経由でモックする。
- pause_flag_file() は APPDATA を tmp_path に差し替えることで隔離する
  (test_uploader.py と同じパターン)。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _isolate(tmp: Path) -> None:
    """APPDATA を tmp に固定し、config/remote_control を再 import させる."""
    os.environ["APPDATA"] = str(tmp)
    for m in ("remote_control", "config"):
        sys.modules.pop(m, None)


@pytest.fixture()
def isolated(tmp_path):
    _isolate(tmp_path)
    yield tmp_path


def _now_iso(offset_seconds: int = 0) -> str:
    """UTC の現在時刻 ± offset を ISO8601 (Z) で返す."""
    dt = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _make_scheduler(
    *,
    folder_id: str = "folder-id",
    key_b64: str = "key-b64",
    customer_id: str = "tribe-001",
    poll_interval_minutes: int = 5,
    force_upload_callback=None,
    uninstall_callback=None,
    grace_period_minutes: int = 10,
):
    from remote_control import RemoteControlScheduler  # type: ignore
    return RemoteControlScheduler(
        folder_id=folder_id,
        service_account_key_b64=key_b64,
        customer_id=customer_id,
        poll_interval_minutes=poll_interval_minutes,
        force_upload_callback=force_upload_callback,
        uninstall_callback=uninstall_callback,
        grace_period_minutes=grace_period_minutes,
    )


# ============================================================================
# 1. configured: 全パラメータが揃ってる時のみ True
# ============================================================================

def test_configured_false_when_folder_id_missing(isolated):
    sched = _make_scheduler(folder_id="")
    assert sched.configured is False


def test_configured_false_when_key_missing(isolated):
    sched = _make_scheduler(key_b64="")
    assert sched.configured is False


def test_configured_false_when_customer_id_missing(isolated):
    sched = _make_scheduler(customer_id="")
    assert sched.configured is False


def test_configured_true_when_all_present_and_gdrive_available(isolated, monkeypatch):
    """google API ライブラリが import 可能な前提で全パラメータ揃ってる場合."""
    import remote_control  # type: ignore
    # ライブラリ未インストール環境でも True を返せるようにフラグを差し替え
    monkeypatch.setattr(remote_control, "_HAS_GDRIVE", True)
    sched = _make_scheduler()
    assert sched.configured is True


def test_configured_false_when_gdrive_unavailable(isolated, monkeypatch):
    import remote_control  # type: ignore
    monkeypatch.setattr(remote_control, "_HAS_GDRIVE", False)
    sched = _make_scheduler()
    assert sched.configured is False


# ============================================================================
# 2. status="paused" で pause_flag_file 作成
# ============================================================================

def test_status_paused_creates_pause_flag(isolated):
    from config import pause_flag_file  # type: ignore
    sched = _make_scheduler()
    control = {
        "status": "paused",
        "action": None,
        "updated_at": _now_iso(),
    }
    sched.apply_control(control)
    assert pause_flag_file().exists()


# ============================================================================
# 3. status="active" で pause_flag_file 削除
# ============================================================================

def test_status_active_removes_pause_flag(isolated):
    from config import pause_flag_file  # type: ignore
    # 事前に PAUSED ファイルを作っておく
    flag = pause_flag_file()
    flag.write_text("manually paused", encoding="utf-8")
    assert flag.exists()

    sched = _make_scheduler()
    control = {
        "status": "active",
        "action": None,
        "updated_at": _now_iso(),
    }
    sched.apply_control(control)
    assert not flag.exists()


# ============================================================================
# 4. action="force_upload" で callback が呼ばれる
# ============================================================================

def test_action_force_upload_invokes_callback(isolated):
    callback = MagicMock(return_value=True)
    sched = _make_scheduler(force_upload_callback=callback)
    control = {
        "status": "active",
        "action": "force_upload",
        "updated_at": _now_iso(),
    }
    sched.apply_control(control)
    callback.assert_called_once_with()


def test_action_force_upload_without_callback_does_not_raise(isolated):
    sched = _make_scheduler(force_upload_callback=None)
    control = {
        "status": "active",
        "action": "force_upload",
        "updated_at": _now_iso(),
    }
    # 例外にならず、ログ警告だけ出る
    sched.apply_control(control)


def test_action_force_upload_callback_exception_swallowed(isolated):
    callback = MagicMock(side_effect=RuntimeError("boom"))
    sched = _make_scheduler(force_upload_callback=callback)
    control = {
        "status": "active",
        "action": "force_upload",
        "updated_at": _now_iso(),
    }
    # callback が例外を投げてもループは止めない
    sched.apply_control(control)
    callback.assert_called_once_with()


# ============================================================================
# 5. action="uninstall" で猶予分の sleep 後に callback が呼ばれる
# ============================================================================

def test_action_uninstall_invokes_callback_after_grace(isolated):
    invoked = threading.Event()

    def _on_uninstall():
        invoked.set()

    # grace_period=0 にして即実行 (threading.Timer 経由なので別スレッドで動く)
    sched = _make_scheduler(
        uninstall_callback=_on_uninstall,
        grace_period_minutes=0,
    )
    control = {
        "status": "active",
        "action": "uninstall",
        "updated_at": _now_iso(),
    }
    sched.apply_control(control)
    # Timer は別スレッドなので少し待つ (テスト用 grace=0min)
    assert invoked.wait(timeout=2.0), "uninstall callback should be invoked"


def test_action_uninstall_schedules_timer(isolated):
    """grace_period > 0 のときは Timer がスケジュールされ、その時点では callback は未呼出."""
    callback = MagicMock()
    sched = _make_scheduler(
        uninstall_callback=callback,
        grace_period_minutes=10,
    )
    control = {
        "status": "active",
        "action": "uninstall",
        "updated_at": _now_iso(),
    }
    sched.apply_control(control)
    # まだ呼ばれてない (10分待たない)
    callback.assert_not_called()
    # Timer が登録されている
    assert sched._uninstall_timer is not None
    # 後片付け
    sched._cancel_pending_uninstall(reason="test cleanup")


# ============================================================================
# 6. 猶予中に action がキャンセルされた場合 uninstall callback が呼ばれない
# ============================================================================

def test_uninstall_canceled_when_action_changes(isolated):
    callback = MagicMock()
    sched = _make_scheduler(
        uninstall_callback=callback,
        grace_period_minutes=10,  # 長めにして cancel が間に合う様に
    )
    # まず uninstall をスケジュール
    sched.apply_control({
        "status": "active",
        "action": "uninstall",
        "updated_at": _now_iso(),
    })
    assert sched._uninstall_timer is not None

    # その後 action=None の control が来たら猶予タイマーをキャンセル
    sched.apply_control({
        "status": "active",
        "action": None,
        "updated_at": _now_iso(offset_seconds=1),
    })
    assert sched._uninstall_timer is None

    # 少し待ってもコールバックは呼ばれない (キャンセルされたので)
    time.sleep(0.3)
    callback.assert_not_called()


def test_uninstall_canceled_when_status_changes_to_paused(isolated):
    """status=paused & action=None でもキャンセルされる (action!='uninstall' なので)."""
    callback = MagicMock()
    sched = _make_scheduler(
        uninstall_callback=callback,
        grace_period_minutes=10,
    )
    sched.apply_control({
        "status": "active",
        "action": "uninstall",
        "updated_at": _now_iso(),
    })
    assert sched._uninstall_timer is not None

    sched.apply_control({
        "status": "paused",
        "action": None,
        "updated_at": _now_iso(offset_seconds=1),
    })
    assert sched._uninstall_timer is None
    time.sleep(0.2)
    callback.assert_not_called()


# ============================================================================
# 7. updated_at が未来 → 反映されない
# ============================================================================

def test_future_updated_at_is_rejected(isolated):
    from config import pause_flag_file  # type: ignore
    sched = _make_scheduler()
    # 1時間後の updated_at (許容は+5分)
    control = {
        "status": "paused",
        "action": None,
        "updated_at": _now_iso(offset_seconds=3600),
    }
    sched.apply_control(control)
    # paused が反映されていないので flag は作られていない
    assert not pause_flag_file().exists()


def test_future_updated_at_does_not_trigger_force_upload(isolated):
    callback = MagicMock()
    sched = _make_scheduler(force_upload_callback=callback)
    control = {
        "status": "active",
        "action": "force_upload",
        "updated_at": _now_iso(offset_seconds=3600),
    }
    sched.apply_control(control)
    callback.assert_not_called()


# ============================================================================
# 8. updated_at が30日以上前 → 反映されない
# ============================================================================

def test_stale_updated_at_is_rejected(isolated):
    from config import pause_flag_file  # type: ignore
    sched = _make_scheduler()
    # 31日前 (30日 + 1日)
    stale = datetime.now(timezone.utc) - timedelta(days=31)
    control = {
        "status": "paused",
        "action": None,
        "updated_at": stale.replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
    }
    sched.apply_control(control)
    assert not pause_flag_file().exists()


def test_unparsable_updated_at_is_rejected(isolated):
    from config import pause_flag_file  # type: ignore
    sched = _make_scheduler()
    control = {
        "status": "paused",
        "action": None,
        "updated_at": "not-a-date",
    }
    sched.apply_control(control)
    assert not pause_flag_file().exists()


def test_missing_updated_at_is_rejected(isolated):
    from config import pause_flag_file  # type: ignore
    sched = _make_scheduler()
    control = {
        "status": "paused",
        "action": None,
        # updated_at 無し
    }
    sched.apply_control(control)
    assert not pause_flag_file().exists()


# ============================================================================
# 9. status 想定外 → "active" 扱い
# ============================================================================

def test_unexpected_status_treated_as_active(isolated):
    from config import pause_flag_file  # type: ignore
    # 事前に pause flag を置いておく
    pause_flag_file().write_text("paused", encoding="utf-8")

    sched = _make_scheduler()
    control = {
        "status": "destroy_the_world",  # 想定外
        "action": None,
        "updated_at": _now_iso(),
    }
    sched.apply_control(control)
    # active 扱いなので flag は削除される
    assert not pause_flag_file().exists()


# ============================================================================
# 10. poll_interval_minutes の範囲外は無視
# ============================================================================

def test_poll_interval_out_of_range_is_ignored(isolated):
    sched = _make_scheduler(poll_interval_minutes=5)
    assert sched._poll_interval_minutes == 5

    # 0 (下限未満) は無視
    sched.apply_control({
        "status": "active",
        "action": None,
        "updated_at": _now_iso(),
        "poll_interval_minutes": 0,
    })
    assert sched._poll_interval_minutes == 5

    # 61 (上限超過) は無視
    sched.apply_control({
        "status": "active",
        "action": None,
        "updated_at": _now_iso(offset_seconds=1),
        "poll_interval_minutes": 61,
    })
    assert sched._poll_interval_minutes == 5

    # 文字列は無視
    sched.apply_control({
        "status": "active",
        "action": None,
        "updated_at": _now_iso(offset_seconds=2),
        "poll_interval_minutes": "abc",
    })
    assert sched._poll_interval_minutes == 5


def test_poll_interval_in_range_is_applied(isolated):
    sched = _make_scheduler(poll_interval_minutes=5)
    sched.apply_control({
        "status": "active",
        "action": None,
        "updated_at": _now_iso(),
        "poll_interval_minutes": 15,
    })
    assert sched._poll_interval_minutes == 15


def test_constructor_sanitizes_out_of_range_poll_interval(isolated):
    """コンストラクタに範囲外を渡したら既定 5 にフォールバック."""
    sched = _make_scheduler(poll_interval_minutes=999)
    assert sched._poll_interval_minutes == 5

    sched2 = _make_scheduler(poll_interval_minutes=0)
    assert sched2._poll_interval_minutes == 5


# ============================================================================
# 補助テスト: apply_control の引数が dict でない
# ============================================================================

def test_apply_control_with_non_dict_is_safe(isolated):
    sched = _make_scheduler()
    # 例外を投げない
    sched.apply_control(None)  # type: ignore[arg-type]
    sched.apply_control("garbage")  # type: ignore[arg-type]
    sched.apply_control(123)  # type: ignore[arg-type]


# ============================================================================
# 補助テスト: start/stop は configured=False では何もしない
# ============================================================================

def test_start_does_nothing_when_not_configured(isolated):
    sched = _make_scheduler(folder_id="")
    assert sched.configured is False
    sched.start()  # no-op
    assert sched._thread is None


def test_stop_cancels_pending_uninstall(isolated):
    callback = MagicMock()
    sched = _make_scheduler(
        uninstall_callback=callback,
        grace_period_minutes=10,
    )
    sched.apply_control({
        "status": "active",
        "action": "uninstall",
        "updated_at": _now_iso(),
    })
    assert sched._uninstall_timer is not None
    sched.stop()
    assert sched._uninstall_timer is None
    time.sleep(0.2)
    callback.assert_not_called()


# ============================================================================
# 補助テスト: fetch_now の上位ハンドリング (configured=False ならスキップ)
# ============================================================================

def test_fetch_now_returns_none_when_not_configured(isolated):
    sched = _make_scheduler(folder_id="")
    assert sched.fetch_now() is None


# ============================================================================
# 補助テスト: 連続適用で同じ updated_at でも問題なく動く
# ============================================================================

def test_repeated_apply_with_same_control_is_idempotent(isolated):
    from config import pause_flag_file  # type: ignore
    sched = _make_scheduler()
    control = {
        "status": "paused",
        "action": None,
        "updated_at": _now_iso(),
    }
    sched.apply_control(control)
    assert pause_flag_file().exists()
    sched.apply_control(control)
    assert pause_flag_file().exists()


# ============================================================================
# 補助テスト: action=force_upload + status=paused で両方反映される
# ============================================================================

def test_force_upload_while_paused(isolated):
    from config import pause_flag_file  # type: ignore
    callback = MagicMock(return_value=True)
    sched = _make_scheduler(force_upload_callback=callback)
    control = {
        "status": "paused",
        "action": "force_upload",
        "updated_at": _now_iso(),
    }
    sched.apply_control(control)
    assert pause_flag_file().exists()
    callback.assert_called_once_with()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
