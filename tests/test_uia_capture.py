"""uia_capture のテスト. Mac環境では実機UIAは無いのでスタブ動作と attach_masked_value の検証中心."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from uia_capture import (  # noqa: E402
    FocusedControl,
    attach_masked_value,
    get_focused_control,
    is_uia_available,
)


# ---- 1. Mac環境では None を返す（または実機UIA動作） -----------------------

def test_get_focused_control_returns_none_on_mac() -> None:
    """Mac/Linux 環境では UI Automation ライブラリが import 失敗 → None."""
    result = get_focused_control()
    if not is_uia_available():
        assert result is None
    else:
        # Windows実機環境では結果がある場合もない場合もある（フォーカス無いとNone）
        assert result is None or isinstance(result, FocusedControl)


# ---- 2. FocusedControl ベースクラスの動作 ----------------------------------

def test_focused_control_default_initialization() -> None:
    fc = FocusedControl()
    assert fc.automation_id == ""
    assert fc.name == ""
    assert fc.is_password is False
    assert fc.value_masked is None
    assert fc.parent_path == []


def test_focused_control_to_dict() -> None:
    fc = FocusedControl(
        automation_id="qty_input",
        name="数量",
        control_type="Edit",
        class_name="TEdit",
        parent_path=["処方入力", "メイン", "アプリ"],
        is_password=False,
    )
    d = fc.to_dict()
    assert d["automation_id"] == "qty_input"
    assert d["parent_path"] == ["処方入力", "メイン", "アプリ"]
    assert d["is_password"] is False


# ---- 3. attach_masked_value: 通常フィールドはマスカーを通過 ---------------

def test_attach_masked_value_normal_field() -> None:
    fc = FocusedControl(name="患者名", is_password=False)
    masked_fc = attach_masked_value(fc, "鈴木太郎",
                                     mask_func=lambda s: f"[MASKED:personal_name]")
    assert masked_fc.value_masked == "[MASKED:personal_name]"
    assert masked_fc.value_present is True


# ---- 4. attach_masked_value: パスワードフィールドは値を保存しない（最重要） ---

def test_attach_masked_value_password_field_never_records_raw() -> None:
    """PII保護: パスワードは絶対に生値を記録しない."""
    fc = FocusedControl(name="パスワード", is_password=True)
    raw = "Xy9secretValueZ1234"  # 「[PASSWORD:len=...]」と被らない文字列
    masked_fc = attach_masked_value(fc, raw, mask_func=lambda s: s)  # mask_func を NOOP にしても
    # 桁数情報のみ、生値の固有部分は絶対に含まれない
    assert "Xy9" not in str(masked_fc.value_masked)
    assert "secretValue" not in str(masked_fc.value_masked)
    assert "Z1234" not in str(masked_fc.value_masked)
    assert masked_fc.value_masked == f"[PASSWORD:len={len(raw)}]"
    assert masked_fc.value_present is True


# ---- 5. attach_masked_value: 空値は None ---------------------------------

def test_attach_masked_value_empty_string() -> None:
    fc = FocusedControl(name="メモ", is_password=False)
    masked_fc = attach_masked_value(fc, "", mask_func=lambda s: s)
    assert masked_fc.value_masked is None
    assert masked_fc.value_present is False


# ---- 6. attach_masked_value: mask_func 例外時は値を捨てる -----------------

def test_attach_masked_value_mask_func_exception_drops_value() -> None:
    """mask_func 例外時は value_masked=None で生値を絶対に残さない."""
    fc = FocusedControl(name="患者名", is_password=False)

    def _bad_mask(_s: str) -> str:
        raise RuntimeError("mask failed")

    masked_fc = attach_masked_value(fc, "鈴木太郎", mask_func=_bad_mask)
    assert masked_fc.value_masked is None
    # value_present は元値があった事を示す（業務分析用）
    assert masked_fc.value_present is True


# ---- 7. is_uia_available -------------------------------------------------

def test_is_uia_available_consistent() -> None:
    """環境に応じて bool が返る."""
    assert isinstance(is_uia_available(), bool)


# ---- 8. 親要素パス -------------------------------------------------------

def test_focused_control_parent_path_chain() -> None:
    """親要素は3階層まで保持される."""
    fc = FocusedControl(
        name="数量",
        parent_path=["処方詳細", "処方入力画面", "メインウィンドウ"],
    )
    assert len(fc.parent_path) == 3
    assert fc.parent_path[0] == "処方詳細"


# ---- 9. PII漏洩テスト: パスワードフィールドの値は絶対に記録されない -------

@pytest.mark.parametrize("password_value", [
    "Xy9p@ssw0rd",
    "very_unique_secret_token_!@#$%",
    "短いパス",
    "ZZ1234567890",
])
def test_password_field_no_value_leakage(password_value: str) -> None:
    """様々なパスワード値で、生値が value_masked に絶対残らないことを保証.
    生値の特徴的部分を 5 文字スライドで検査して漏洩を機械的に検出."""
    fc = FocusedControl(name="password", is_password=True)
    result = attach_masked_value(fc, password_value, mask_func=lambda s: s)

    # フォーマットは [PASSWORD:len=N] のみ
    assert result.value_masked.startswith("[PASSWORD:len=")

    # 生値の連続5文字スライスがどれも結果に含まれないこと
    masked_str = str(result.value_masked)
    for i in range(max(0, len(password_value) - 4)):
        slice_ = password_value[i:i + 5]
        if len(slice_) >= 5:
            assert slice_ not in masked_str, \
                f"PII LEAK: password slice '{slice_}' leaked into value_masked={masked_str}"


# ---- 10. Codex High#3: name / parent_path のマスク ---------------------

def test_apply_masks_to_focused_control_masks_name_and_parents() -> None:
    from uia_capture import apply_masks_to_focused_control  # type: ignore

    fc = FocusedControl(
        name="鈴木太郎",
        parent_path=["佐藤花子の薬歴", "メイン"],
    )
    masked_fc = apply_masks_to_focused_control(
        fc, mask_func=lambda s: f"[MASKED:test]" if s in ("鈴木太郎", "佐藤花子の薬歴") else s,
    )
    assert masked_fc.name == "[MASKED:test]"
    assert masked_fc.parent_path[0] == "[MASKED:test]"
    assert masked_fc.parent_path[1] == "メイン"


def test_apply_masks_drops_field_when_mask_func_raises() -> None:
    from uia_capture import apply_masks_to_focused_control  # type: ignore

    fc = FocusedControl(name="鈴木太郎", parent_path=["佐藤"])

    def _bad(_s: str) -> str:
        raise RuntimeError("mask error")

    masked_fc = apply_masks_to_focused_control(fc, mask_func=_bad)
    assert masked_fc.name == ""
    assert masked_fc.parent_path == [""]


# ---- 11. Codex High#2: パスワードヒント検知でフォールバック is_password=True ---

def test_password_hint_detection_in_metadata() -> None:
    from uia_capture import apply_masks_to_focused_control  # type: ignore

    fc = FocusedControl(automation_id="passwordBox1",
                        name="パスワード",
                        is_password=False)  # 元は False
    masked_fc = apply_masks_to_focused_control(fc, mask_func=lambda s: s)
    # password ヒントを検知して is_password=True に上がる
    assert masked_fc.is_password is True


def test_password_hint_japanese() -> None:
    from uia_capture import apply_masks_to_focused_control  # type: ignore
    fc = FocusedControl(name="暗証番号", is_password=False)
    masked_fc = apply_masks_to_focused_control(fc, mask_func=lambda s: s)
    assert masked_fc.is_password is True


def test_no_password_hint_keeps_false() -> None:
    from uia_capture import apply_masks_to_focused_control  # type: ignore
    fc = FocusedControl(name="患者名", control_type="Edit", is_password=False)
    masked_fc = apply_masks_to_focused_control(fc, mask_func=lambda s: s)
    assert masked_fc.is_password is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
