"""COLLECTION_MODE='lite' のとき collector がキャプチャ/OCR/マスクを行わないことを検証.

v1.1-lite は薬局向け v1.0 と同一コードベースで動作するが、ビルド時の
_build_constants.py 経由で COLLECTION_MODE='lite' が焼き付けられると
- スクリーンショット取得をスキップ
- OCR/マスクをスキップ
- screenshot=None, additional_screenshots=[] で metadata-only に降格
- rate limit カウントを進めない

このテストでは、collector._COLLECTION_MODE を 'lite' に差し替えて
これら全てが守られていることを検証する。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _isolate_appdata(tmp: Path) -> None:
    os.environ["APPDATA"] = str(tmp)
    for m in (
        "storage", "collector", "config", "window_titles", "masker", "ocr",
        "profile_loader", "app_classifier", "uia_capture", "input_events",
    ):
        sys.modules.pop(m, None)


@pytest.fixture()
def isolated_env(tmp_path):
    _isolate_appdata(tmp_path)
    yield tmp_path


def _make_collector_lite(**cfg):
    """COLLECTION_MODE='lite' を monkey-patch した collector インスタンスを作る."""
    import config as cfg_mod  # type: ignore
    import collector as collector_mod  # type: ignore
    # lite モードに差し替え（test 用、ビルド時定数の代替）
    collector_mod._COLLECTION_MODE = "lite"
    base = {"min_dwell_seconds_for_capture": 0.0, "max_capture_per_minute": 120}
    base.update(cfg)
    return collector_mod, collector_mod.Collector(cfg=cfg_mod.CollectorConfig(**base))


def _info(collector_mod, hwnd: int = 1, title: str = "見積_2026.xlsx", proc: str = "EXCEL.EXE"):
    return collector_mod.WindowInfo(
        hwnd=hwnd, title=title, process_name=proc, process_path=f"C:/{proc}",
        pid=1234, rect=(0, 0, 1280, 720), monitor=1,
    )


def test_lite_mode_emits_metadata_only_event(isolated_env):
    """lite モードでは screenshot=None でイベントが書き込まれる."""
    cm, col = _make_collector_lite()
    # 最初の hwnd 確立用イベント（diff 検出のため return None）
    col.process(_info(cm, hwnd=1))
    ev = col.process(_info(cm, hwnd=2))
    assert ev is not None, "lite mode でもイベントは書かれるべき"
    assert ev["schema_version"] == 2
    assert ev["event_type"] == "window_focus"
    assert ev["screenshot"] is None, "lite mode で screenshot は None"
    assert ev["additional_screenshots"] == [], "additional_screenshots も空"
    # アプリ分類等のメタデータは引き続き含まれる
    assert "app" in ev
    assert ev["app"]["process_name"] == "EXCEL.EXE"


def test_lite_mode_does_not_call_capture(isolated_env, monkeypatch):
    """lite モードでは capture_active / capture_all_monitors が呼ばれない."""
    cm, col = _make_collector_lite()
    capture_calls = {"active": 0, "all": 0}

    def _fake_capture_active(*_a, **_kw):
        capture_calls["active"] += 1
        raise AssertionError("capture_active must not be called in lite mode")

    def _fake_capture_all(*_a, **_kw):
        capture_calls["all"] += 1
        raise AssertionError("capture_all_monitors must not be called in lite mode")

    monkeypatch.setattr(cm, "capture_active", _fake_capture_active)
    monkeypatch.setattr(cm, "capture_all_monitors", _fake_capture_all)

    col.process(_info(cm, hwnd=1))
    col.process(_info(cm, hwnd=2))
    col.process(_info(cm, hwnd=3))
    assert capture_calls == {"active": 0, "all": 0}


def test_lite_mode_does_not_advance_rate_limit(isolated_env):
    """lite モードでは _capture_times に時刻が積まれない（rate limit を消費しない）."""
    cm, col = _make_collector_lite(max_capture_per_minute=2)
    # 5回イベント発生させても rate limit に達しない
    for hwnd in range(1, 7):
        col.process(_info(cm, hwnd=hwnd))
    assert len(col._capture_times) == 0


def test_lite_mode_respects_pause_flag(isolated_env):
    """lite モードでも pause_flag_file がある間はイベント書き込みをスキップする."""
    import config as cfg_mod  # type: ignore
    cm, col = _make_collector_lite()
    cfg_mod.pause_flag_file().write_text("paused", encoding="utf-8")
    col.process(_info(cm, hwnd=1))
    ev = col.process(_info(cm, hwnd=2))
    assert ev is None, "pause 中はイベントを書かない"


def test_lite_mode_respects_blocklist(isolated_env):
    """lite モードでも blocklist プロセス（パスワード管理アプリ等）はスキップ."""
    cm, col = _make_collector_lite()
    col.process(_info(cm, hwnd=1, proc="EXCEL.EXE"))
    # 1Password (blocklist 既定) はスキップされる
    ev = col.process(_info(cm, hwnd=2, proc="1Password.exe"))
    assert ev is None


def test_full_mode_still_attempts_capture(isolated_env, monkeypatch):
    """念のため: full モード（既定）では従来通り capture が試みられる（退行防止）."""
    import config as cfg_mod  # type: ignore
    import collector as collector_mod  # type: ignore
    # 明示的に full にリセット
    collector_mod._COLLECTION_MODE = "full"
    col = collector_mod.Collector(
        cfg=cfg_mod.CollectorConfig(
            min_dwell_seconds_for_capture=0.0, max_capture_per_minute=10,
        ),
    )
    capture_attempts = {"n": 0}

    def _fake_capture_active(*_a, **_kw):
        capture_attempts["n"] += 1
        return None  # capture 失敗扱い -> metadata-only

    def _fake_capture_all(*_a, **_kw):
        capture_attempts["n"] += 1
        return []

    monkeypatch.setattr(collector_mod, "capture_active", _fake_capture_active)
    monkeypatch.setattr(collector_mod, "capture_all_monitors", _fake_capture_all)

    col.process(_info(collector_mod, hwnd=1))
    col.process(_info(collector_mod, hwnd=2))
    assert capture_attempts["n"] >= 1, "full mode では capture が呼ばれる"
