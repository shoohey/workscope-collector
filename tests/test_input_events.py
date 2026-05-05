"""input_events のテスト. 主にロジック層 (NAV_KEYS判定/click resolver/PII保護) を検証.

実機キーフックは Mac/CI 環境では unstable なので、InputEventLogger.start() は
ライブラリ無し環境で no-op になる事のみ確認し、内部ロジックは直接呼ぶ。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from input_events import (  # noqa: E402
    KeyEvent,
    MODIFIER_KEYS,
    MouseEvent,
    NAV_KEYS,
    InputEventLogger,
    resolve_click_target,
)


@dataclass
class _Box:
    text: str
    bbox: tuple[int, int, int, int]


# ============================================================================
# 1. NAV_KEYS / MODIFIER_KEYS 定義の網羅
# ============================================================================

def test_nav_keys_includes_essentials() -> None:
    must_have = {"tab", "enter", "esc", "backspace", "f1", "f12",
                 "up", "down", "left", "right"}
    missing = must_have - NAV_KEYS
    assert not missing, f"NAV_KEYS missing: {missing}"


def test_modifier_keys_includes_essentials() -> None:
    must_have = {"ctrl", "shift", "alt"}
    missing = must_have - MODIFIER_KEYS
    assert not missing, f"MODIFIER_KEYS missing: {missing}"


# ============================================================================
# 2. KeyEvent / MouseEvent シリアライズ
# ============================================================================

def test_key_event_to_dict() -> None:
    ev = KeyEvent(ts=123.456, event_type="key_combo", key_name="c",
                  modifiers=["Ctrl"], text_keys_count=0)
    d = ev.to_dict()
    assert d["event_type"] == "key_combo"
    assert d["key_name"] == "c"
    assert d["modifiers"] == ["Ctrl"]
    assert d["text_keys_count"] == 0


def test_key_event_text_typed() -> None:
    ev = KeyEvent(ts=123.456, event_type="key_typed", key_name="",
                  modifiers=[], text_keys_count=8)
    d = ev.to_dict()
    assert d["text_keys_count"] == 8
    assert d["key_name"] == ""


def test_mouse_event_to_dict() -> None:
    ev = MouseEvent(ts=999.0, event_type="mouse_click", button="left",
                    coords=(100, 200), target_text_masked="[MASKED:patient_id]")
    d = ev.to_dict()
    assert d["coords"] == [100, 200]
    assert d["button"] == "left"
    assert d["target_text_masked"] == "[MASKED:patient_id]"


# ============================================================================
# 3. InputEventLogger: パスワードフィールド中はロギング停止（最重要）
# ============================================================================

def test_logger_does_not_record_when_password_active() -> None:
    """is_password=True を返す関数を渡すと、内部の key handler が早期 return."""
    captured_keys: list[KeyEvent] = []
    captured_mouse: list[MouseEvent] = []

    logger = InputEventLogger(
        on_key=captured_keys.append,
        on_mouse=captured_mouse.append,
        is_password_field_active=lambda: True,
    )

    # _handle_key_pynput を直接呼んでテスト（実機フックは使わない）
    if hasattr(logger, "_handle_key_pynput"):
        # ダミーキーオブジェクト
        class _Key:
            name = "a"
        logger._handle_key_pynput(_Key(), pressed=True)

    assert captured_keys == [], "PII LEAK: password中なのにキーが記録された"


def test_logger_records_when_password_inactive() -> None:
    """is_password=False の時は通常記録される."""
    captured_keys: list[KeyEvent] = []
    logger = InputEventLogger(
        on_key=captured_keys.append,
        on_mouse=lambda _: None,
        is_password_field_active=lambda: False,
    )

    class _NavKey:
        name = "tab"
    logger._handle_key_pynput(_NavKey(), pressed=True)
    assert len(captured_keys) >= 1
    assert captured_keys[0].key_name == "tab"


# ============================================================================
# 4. 文字キーは桁数のみ記録（値は捨てる）
# ============================================================================

def test_text_keys_count_aggregates_and_no_chars_recorded() -> None:
    """文字キーは桁数カウントのみ、ナビキー入力時に flush される."""
    captured: list[KeyEvent] = []
    logger = InputEventLogger(
        on_key=captured.append, on_mouse=lambda _: None,
        is_password_field_active=lambda: False,
    )

    class _Char:
        def __init__(self, c: str) -> None:
            self.char = c
            self.name = ""

    # 文字キー5回打つ
    for c in "abcde":
        logger._handle_key_pynput(_Char(c), pressed=True)

    # まだ flush されていない
    assert captured == []

    # Tab を押すと文字数が flush される
    class _Tab:
        name = "tab"
    logger._handle_key_pynput(_Tab(), pressed=True)

    # 1本目: 文字キー5桁、2本目: tab
    assert len(captured) == 2
    assert captured[0].text_keys_count == 5
    assert captured[0].key_name == ""  # 文字キーバッチは key_name 空
    assert captured[1].key_name == "tab"
    # 文字キーバッチイベント（key_typed かつ text_keys_count > 0）の key_name に
    # 入力文字（"a"〜"e"）が一切残っていないこと
    text_batch_events = [e for e in captured if e.text_keys_count > 0]
    for ev in text_batch_events:
        for c in "abcde":
            assert c not in ev.key_name, \
                f"PII LEAK: char '{c}' leaked to text-batch event key_name='{ev.key_name}'"


def test_modifier_combination_recorded_as_combo() -> None:
    """Ctrl+C のような組み合わせは key_combo として記録される."""
    captured: list[KeyEvent] = []
    logger = InputEventLogger(
        on_key=captured.append, on_mouse=lambda _: None,
        is_password_field_active=lambda: False,
    )

    class _Ctrl:
        name = "ctrl"
    class _C:
        char = "c"
        name = ""

    logger._handle_key_pynput(_Ctrl(), pressed=True)   # Ctrl 押下
    logger._handle_key_pynput(_C(), pressed=True)      # C 押下
    logger._handle_key_pynput(_Ctrl(), pressed=False)  # Ctrl 離す

    # combo として記録され、modifiers に "ctrl" が入る
    combos = [e for e in captured if e.event_type == "key_combo"]
    assert len(combos) >= 1
    assert "ctrl" in combos[0].modifiers


# ============================================================================
# 5. resolve_click_target: クリック対象テキスト推定
# ============================================================================

def test_resolve_click_inside_box() -> None:
    """クリック座標が box の中なら、その box の text を返す."""
    boxes = [
        _Box(text="保存", bbox=(100, 50, 200, 100)),
        _Box(text="キャンセル", bbox=(300, 50, 450, 100)),
    ]
    text = resolve_click_target((150, 75), boxes)
    assert text == "保存"


def test_resolve_click_nearest_within_50px() -> None:
    """boxの外でも50px以内なら最も近い box を返す."""
    boxes = [
        _Box(text="送信", bbox=(100, 50, 200, 100)),
    ]
    text = resolve_click_target((220, 75), boxes)  # 20px外
    assert text == "送信"


def test_resolve_click_no_match_returns_none() -> None:
    """50px以上離れたら None."""
    boxes = [
        _Box(text="送信", bbox=(100, 50, 200, 100)),
    ]
    text = resolve_click_target((1000, 1000), boxes)
    assert text is None


def test_resolve_click_passes_through_mask_func() -> None:
    """mask_func が指定されればマスク済みテキストを返す."""
    boxes = [
        _Box(text="鈴木太郎 様", bbox=(100, 50, 300, 100)),
    ]
    text = resolve_click_target((150, 75), boxes,
                                  mask_func=lambda s: "[MASKED:personal_name]")
    assert text == "[MASKED:personal_name]"


def test_resolve_click_mask_func_exception_returns_none() -> None:
    """mask_func 例外時は None で安全側."""
    boxes = [
        _Box(text="患者氏名", bbox=(100, 50, 300, 100)),
    ]

    def _bad(_s: str) -> str:
        raise RuntimeError("mask error")

    text = resolve_click_target((150, 75), boxes, mask_func=_bad)
    assert text is None


def test_resolve_click_empty_boxes_returns_none() -> None:
    assert resolve_click_target((100, 100), []) is None


# ============================================================================
# 6. InputEventLogger: ライブラリ無し環境では no-op
# ============================================================================

def test_logger_start_is_safe_without_libs(monkeypatch) -> None:
    """ライブラリ無し環境（Mac等）でも start() は例外を投げない."""
    import input_events as ie
    monkeypatch.setattr(ie, "_HAS_PYNPUT", False)
    monkeypatch.setattr(ie, "_HAS_KEYBOARD", False)

    captured: list = []
    logger = InputEventLogger(on_key=captured.append, on_mouse=lambda _: None)
    logger.start()  # 例外なし
    logger.stop()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
