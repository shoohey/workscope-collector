"""collector v1.0 schema v2 のテスト.

テスト対象:
- window_focus イベントに schema_version=2, app.category, app.rpa_target が含まれる
- feed_key_event / feed_mouse_event が key_typed/mouse_click イベントとして書ける
- is_password_field_active のゲートが動作する
- 既存 v1 互換イベントとの混在が問題ない
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _isolate_appdata(tmp: Path) -> None:
    os.environ["APPDATA"] = str(tmp)
    for m in ("storage", "collector", "config", "window_titles", "masker", "ocr",
              "profile_loader", "app_classifier", "uia_capture", "input_events"):
        sys.modules.pop(m, None)


class _StubOCR:
    def __init__(self, boxes: list) -> None:
        self._boxes = boxes
    def extract(self, _image) -> list:
        return list(self._boxes)


def _white(w: int = 600, h: int = 400) -> Image.Image:
    return Image.new("RGB", (w, h), (255, 255, 255))


@pytest.fixture()
def isolated_env(tmp_path):
    _isolate_appdata(tmp_path)
    yield tmp_path


def _make_collector(stub_boxes: list, **cfg):
    import config as cfg_mod  # type: ignore
    import collector as collector_mod  # type: ignore
    base = {"min_dwell_seconds_for_capture": 0.0, "max_capture_per_minute": 120}
    base.update(cfg)
    return collector_mod, collector_mod.Collector(
        cfg=cfg_mod.CollectorConfig(**base),
        ocr_engine=_StubOCR(stub_boxes),
    )


def _info(collector_mod, hwnd: int = 1, title: str = "処方せん入力", proc: str = "ReceptyNEXT.exe"):
    return collector_mod.WindowInfo(
        hwnd=hwnd, title=title, process_name=proc,
        process_path=f"C:\\{proc}", pid=999,
        rect=(0, 0, 1920, 1080), monitor=1,
    )


def _patch_capture(collector_mod, monkeypatch):
    monkeypatch.setattr(collector_mod, "capture_active",
                        lambda _info=None: _white())


# ============================================================================
# 1. window_focus イベントに schema_version=2 と app.category が入る
# ============================================================================

def test_window_focus_event_has_schema_version_2(isolated_env, monkeypatch):
    collector_mod, collector = _make_collector(stub_boxes=[])
    _patch_capture(collector_mod, monkeypatch)

    info = _info(collector_mod)
    event = collector.process(info)

    assert event is not None
    assert event["schema_version"] == 2
    assert event["event_type"] == "window_focus"


def test_window_focus_event_has_app_category(isolated_env, monkeypatch):
    """ReceptyNEXT.exe + 処方せん入力 → industry_medical / pywinauto."""
    collector_mod, collector = _make_collector(stub_boxes=[])
    _patch_capture(collector_mod, monkeypatch)

    info = _info(collector_mod, title="処方せん入力", proc="ReceptyNEXT.exe")
    event = collector.process(info)

    assert event["app"]["category"] == "industry_medical"
    assert event["app"]["rpa_target"] == "pywinauto"


def test_window_focus_event_has_focused_control_field(isolated_env, monkeypatch):
    """focused_control は Mac では None（後方互換: フィールド存在のみ確認）."""
    collector_mod, collector = _make_collector(stub_boxes=[])
    _patch_capture(collector_mod, monkeypatch)

    info = _info(collector_mod)
    event = collector.process(info)

    # フィールド自体は存在する（値は環境依存）
    assert "focused_control" in event


# ============================================================================
# 2. feed_key_event: KeyEvent を JSONL に書ける
# ============================================================================

def test_feed_key_event_writes_key_typed_event(isolated_env, monkeypatch):
    collector_mod, collector = _make_collector(stub_boxes=[])

    from input_events import KeyEvent  # type: ignore
    ev = KeyEvent(ts=123.0, event_type="key_typed", key_name="",
                  text_keys_count=8, modifiers=[])
    out = collector.feed_key_event(ev)

    assert out is not None
    assert out["schema_version"] == 2
    assert out["event_type"] == "key_typed"
    assert out["input"]["text_keys_count"] == 8


def test_feed_key_event_writes_key_combo_event(isolated_env, monkeypatch):
    collector_mod, collector = _make_collector(stub_boxes=[])

    from input_events import KeyEvent  # type: ignore
    ev = KeyEvent(ts=123.0, event_type="key_combo", key_name="c",
                  text_keys_count=0, modifiers=["ctrl"])
    out = collector.feed_key_event(ev)

    assert out["event_type"] == "key_combo"
    assert out["input"]["modifiers"] == ["ctrl"]


def test_feed_key_event_none_returns_none(isolated_env):
    _, collector = _make_collector(stub_boxes=[])
    assert collector.feed_key_event(None) is None


# ============================================================================
# 3. feed_mouse_event: MouseEvent を JSONL に書け、対象テキスト推定も動く
# ============================================================================

def test_feed_mouse_event_basic(isolated_env, monkeypatch):
    collector_mod, collector = _make_collector(stub_boxes=[])

    from input_events import MouseEvent  # type: ignore
    ev = MouseEvent(ts=456.0, event_type="mouse_click", button="left",
                    coords=(100, 200))
    out = collector.feed_mouse_event(ev)

    assert out is not None
    assert out["schema_version"] == 2
    assert out["event_type"] == "mouse_click"
    assert out["input"]["coords"] == [100, 200]


def test_feed_mouse_event_with_ocr_resolves_target(isolated_env, monkeypatch):
    collector_mod, collector = _make_collector(stub_boxes=[])
    from ocr import OCRBox  # type: ignore
    from input_events import MouseEvent  # type: ignore

    boxes = [OCRBox(text="保存", bbox=(100, 50, 200, 100), confidence=0.95)]
    ev = MouseEvent(ts=456.0, event_type="mouse_click", button="left",
                    coords=(150, 75))
    out = collector.feed_mouse_event(ev, ocr_boxes=boxes)

    # クリック対象テキストが推定される（マスク後）
    assert out["input"].get("target_text_masked") == "保存"


def test_feed_mouse_event_pii_in_target_text_is_masked(isolated_env, monkeypatch):
    """クリック対象テキストにPIIが含まれる場合はマスクされる."""
    collector_mod, collector = _make_collector(stub_boxes=[])
    from ocr import OCRBox  # type: ignore
    from input_events import MouseEvent  # type: ignore

    boxes = [OCRBox(text="鈴木太郎 様", bbox=(100, 50, 300, 100), confidence=0.95)]
    ev = MouseEvent(ts=456.0, event_type="mouse_click", button="left",
                    coords=(200, 75))
    out = collector.feed_mouse_event(ev, ocr_boxes=boxes)

    # PIIが含まれていれば [MASKED:...] になる
    target = out["input"].get("target_text_masked", "")
    assert "鈴木太郎" not in target, f"PII LEAK: 鈴木太郎 leaked into target: {target}"


# ============================================================================
# 4. is_password_field_active ゲート
# ============================================================================

def test_is_password_field_active_default_false(isolated_env):
    _, collector = _make_collector(stub_boxes=[])
    # UIA未取得のデフォルトは False
    assert collector.is_password_field_active() is False


# ============================================================================
# 5. start_input_logger: ライブラリ無しなら False を返す
# ============================================================================

def test_start_input_logger_without_libs_returns_false(isolated_env, monkeypatch):
    """Mac環境では pynput が無い → start_input_logger は False を返す."""
    import collector as collector_mod  # type: ignore
    monkeypatch.setattr(collector_mod, "_HAS_INPUT_EVENTS", False)

    _, collector = _make_collector(stub_boxes=[])
    result = collector.start_input_logger()
    assert result is False


# ============================================================================
# 6. event_seq が連続している（key/mouse/window_focus 混在でも）
# ============================================================================

def test_event_seq_monotonically_increasing(isolated_env, monkeypatch):
    collector_mod, collector = _make_collector(stub_boxes=[])
    _patch_capture(collector_mod, monkeypatch)
    from input_events import KeyEvent, MouseEvent  # type: ignore

    e1 = collector.process(_info(collector_mod, hwnd=1))
    e2 = collector.feed_key_event(KeyEvent(ts=1.0, event_type="key_typed",
                                            text_keys_count=5))
    e3 = collector.feed_mouse_event(MouseEvent(ts=2.0, event_type="mouse_click",
                                                button="left", coords=(10, 10)))
    e4 = collector.process(_info(collector_mod, hwnd=2, title="次の画面"))

    seqs = [e1["event_seq"], e2["event_seq"], e3["event_seq"], e4["event_seq"]]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 4  # 重複なし


# ============================================================================
# 7. 既存 v0.1.0 互換: window_focus イベントの主要フィールド維持
# ============================================================================

def test_v01_compat_fields_still_present(isolated_env, monkeypatch):
    """v0.1.0時代の主要フィールドが schema v2 でも維持されている."""
    collector_mod, collector = _make_collector(stub_boxes=[])
    _patch_capture(collector_mod, monkeypatch)

    info = _info(collector_mod)
    event = collector.process(info)

    # v0.1.0互換フィールド
    assert "session_id" in event
    assert "event_seq" in event
    assert "ts" in event
    assert event["event_type"] == "window_focus"
    assert "process_name" in event["app"]
    assert "process_path" in event["app"]
    assert "title" in event["window"]
    assert "title_raw_hash" in event["window"]
    assert "dwell_ms_prev" in event
    assert "screenshot" in event
    assert "transition_from_app" in event


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
