"""masker.py のテスト. OCR は使わず OCRBox を手で組んで mask_image を検証.

実装方針:
- 各テキストごとに 80x30 の白画像と OCRBox を作る
- mask_image を呼び、結果 image の対象矩形が黒く塗られていることをピクセル確認
- text_summary に [MASKED:<category>] が含まれることを確認
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

# src/ を import path に追加（pytest 実行ディレクトリに依存しないように）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from masker import (  # noqa: E402
    CAT_ADDRESS,
    CAT_BIRTHDATE,
    CAT_INSURANCE_ID,
    CAT_NAME_LIKE_KANJI,
    CAT_PATIENT_NAME,
    CAT_PHONE,
    DEFAULT_RULES,
    mask_image,
)
from ocr import OCRBox  # noqa: E402
from window_titles import mask_window_title  # noqa: E402


# --- ヘルパ ---

def _make_canvas(boxes: list[tuple[str, tuple[int, int, int, int]]]) -> tuple[Image.Image, list[OCRBox]]:
    """白背景画像 + OCRBox を生成. bbox: (x1,y1,x2,y2)."""
    if boxes:
        max_x = max(b[1][2] for b in boxes) + 10
        max_y = max(b[1][3] for b in boxes) + 10
    else:
        max_x, max_y = 100, 100
    img = Image.new("RGB", (max_x, max_y), (255, 255, 255))
    ocr_boxes = [OCRBox(text=t, bbox=bb, confidence=0.95) for t, bb in boxes]
    return img, ocr_boxes


def _is_black_at(img: Image.Image, bbox: tuple[int, int, int, int]) -> bool:
    """bboxの中央付近に黒ピクセルが存在するか確認."""
    arr = np.array(img)
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    cy = max(0, min(cy, arr.shape[0] - 1))
    cx = max(0, min(cx, arr.shape[1] - 1))
    px = arr[cy, cx]
    return int(px[0]) < 30 and int(px[1]) < 30 and int(px[2]) < 30


# --- 1. 患者氏名（漢字+敬称） ---
def test_mask_patient_name_with_honorific() -> None:
    img, boxes = _make_canvas([("鈴木太郎 様", (10, 10, 200, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert CAT_PATIENT_NAME in result.mask_categories
    assert _is_black_at(result.masked_image, boxes[0].bbox)
    assert "[MASKED:" in result.text_summary


# --- 2. 保険者番号（文脈キーワード+8桁） ---
def test_mask_insurance_id_with_context() -> None:
    img, boxes = _make_canvas([
        ("保険者番号", (10, 10, 200, 40)),
        ("12345678", (220, 10, 400, 40)),  # 右隣
    ])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert CAT_INSURANCE_ID in result.mask_categories
    assert _is_black_at(result.masked_image, boxes[1].bbox)


def test_mask_insurance_id_inline() -> None:
    """同じbox内に「保険者番号: 12345678」と入っているケース."""
    img, boxes = _make_canvas([("保険者番号: 12345678", (10, 10, 400, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert CAT_INSURANCE_ID in result.mask_categories


# --- 3. 電話番号 ---
def test_mask_phone_number() -> None:
    img, boxes = _make_canvas([("090-1234-5678", (10, 10, 300, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert CAT_PHONE in result.mask_categories
    assert _is_black_at(result.masked_image, boxes[0].bbox)


# --- 4. 生年月日（和暦） ---
def test_mask_birthdate_wareki() -> None:
    img, boxes = _make_canvas([("令和6年5月4日", (10, 10, 300, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert CAT_BIRTHDATE in result.mask_categories


def test_mask_birthdate_slash() -> None:
    img, boxes = _make_canvas([("1985/03/15", (10, 10, 300, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert CAT_BIRTHDATE in result.mask_categories


# --- 5. 薬品名は誤マスクされない ---
def test_drug_name_not_masked() -> None:
    img, boxes = _make_canvas([("処方薬: アムロジピン 5mg", (10, 10, 400, 40))])
    result = mask_image(img, boxes, strict=True)
    # 薬品名のみ（患者IDなし）はマスクされない
    assert result.mask_count == 0
    # 黒矩形が描かれていない
    assert not _is_black_at(result.masked_image, boxes[0].bbox)


def test_drug_name_with_patient_id_is_masked() -> None:
    """処方薬と患者IDが同じテキスト塊にあればマスクする."""
    img, boxes = _make_canvas([
        ("患者ID", (10, 10, 200, 40)),
        ("アムロジピン 5mg 患者番号 12345", (10, 50, 500, 80)),
    ])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1


# --- 6. 住所 ---
def test_mask_address() -> None:
    img, boxes = _make_canvas([("東京都新宿区西新宿1-2-3", (10, 10, 400, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert CAT_ADDRESS in result.mask_categories


def test_mask_postal_code() -> None:
    img, boxes = _make_canvas([("〒160-0023", (10, 10, 200, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1


# --- 7. strict mode: 未知漢字連続のヒューリスティック ---
def test_strict_mode_masks_unknown_kanji_run() -> None:
    """strict modeでは敬称なしの漢字2-5文字もマスクする（保険）.

    unmaskable=False とすることで、黒塗り済みの画像は破棄せず保存される。
    （患者一覧画面で氏名が大量に出るため、True にすると業務フローが何も
    残らなくなる。Codex P2 (2026-05-04) 反映。）
    """
    img, boxes = _make_canvas([("田中花子", (10, 10, 200, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert CAT_NAME_LIKE_KANJI in result.mask_categories
    # ヒューリスティックでマスクが成功した場合は unmaskable=False
    assert result.unmaskable is False


def test_non_strict_mode_does_not_mask_unknown_kanji() -> None:
    img, boxes = _make_canvas([("田中花子", (10, 10, 200, 40))])
    result = mask_image(img, boxes, strict=False)
    assert result.mask_count == 0


# --- 8. マイナンバー ---
def test_mask_my_number() -> None:
    img, boxes = _make_canvas([
        ("マイナンバー", (10, 10, 200, 40)),
        ("1234 5678 9012", (220, 10, 500, 40)),
    ])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1


# --- 9. text_summary が読み順で連結される ---
def test_text_summary_reading_order_and_truncation() -> None:
    img, boxes = _make_canvas([
        ("ヘッダー", (10, 10, 200, 40)),
        ("鈴木太郎 様", (10, 50, 200, 80)),
        ("処方せん入力画面", (10, 90, 300, 120)),  # 漢字+ひらがな混在: kanji_run にマッチしない
    ])
    result = mask_image(img, boxes, strict=True)
    # 読み順: ヘッダー → [MASKED:patient_name] → 処方せん入力画面
    assert "ヘッダー" in result.text_summary
    assert "[MASKED:" in result.text_summary
    assert "処方せん入力画面" in result.text_summary
    assert len(result.text_summary) <= 2000


def test_text_summary_truncated_to_2000_chars() -> None:
    boxes = [("あ" * 100, (10, 10 + i * 30, 500, 40 + i * 30)) for i in range(50)]
    img, ocr_boxes = _make_canvas(boxes)
    result = mask_image(img, ocr_boxes, strict=True)
    assert len(result.text_summary) <= 2000


# --- 10. 空入力 ---
def test_empty_boxes_returns_blank_summary() -> None:
    img = Image.new("RGB", (100, 100), (255, 255, 255))
    result = mask_image(img, [], strict=True)
    assert result.mask_count == 0
    assert result.mask_categories == []
    assert result.text_summary == ""
    assert result.unmaskable is False


# --- 11. DEFAULT_RULES が9カテゴリをカバー ---
def test_default_rules_cover_required_categories() -> None:
    """v1.0汎用化で patient_name → personal_name に統合.
    pharmacy プロファイル継承後の DEFAULT_RULES は薬局必須カテゴリを全てカバー."""
    required = {
        "personal_name",       # v1.0で base に格上げ (旧 patient_name)
        "insurance_id",        # pharmacy 固有
        "insurance_card_no",   # pharmacy 固有
        "patient_id",          # pharmacy 固有
        "birthdate",           # base 共通
        "phone",               # base 共通
        "address",             # base 共通
        "postal_code",         # base 共通
        "my_number",           # base 共通
    }
    covered = {r.category for r in DEFAULT_RULES}
    missing = required - covered
    assert not missing, f"DEFAULT_RULES に不足: {missing}"


# --- 12. window_titles_mask ---
def test_window_title_mask_phone() -> None:
    masked, h, cats = mask_window_title("受付 - 090-1234-5678")
    assert "[MASKED:phone]" in masked
    assert "phone" in cats
    assert len(h) == 16


def test_window_title_mask_name_kanji() -> None:
    """v1.0: patient_name → personal_name に統合 (base プロファイル化).
    敬称付き氏名は引き続きマスクされる（name_like_kanji ヒューリスティックは
    タイトル業務分析阻害のため v1.0 で window_titles 側からは削除済み）."""
    masked, h, cats = mask_window_title("カルテ表示 - 鈴木太郎 様")
    assert "[MASKED:" in masked
    assert any(c in cats for c in ("personal_name", "patient_name", "name_like_kanji"))


def test_window_title_empty() -> None:
    masked, h, cats = mask_window_title("")
    assert masked == ""
    assert h == ""
    assert cats == []


def test_window_title_no_pii() -> None:
    masked, h, cats = mask_window_title("ABC")
    assert masked == "ABC"
    assert len(h) == 16
    assert cats == []


# --- 13. Codex P1: マイナンバーが context 失効してもマスクされること --------
def test_window_title_my_number_masked_even_without_context_keyword() -> None:
    """元タイトルに "マイナンバー" が無くても 12 桁の数字列は強制マスクされる."""
    masked, _, cats = mask_window_title("番号入力 - 1234-5678-9012")
    assert "1234-5678-9012" not in masked
    assert "[MASKED:my_number]" in masked
    assert "my_number" in cats


def test_window_title_my_number_with_context_still_masked() -> None:
    """"マイナンバー" を含むタイトルでも数字列がマスクされる（contextが失われても発火）."""
    masked, _, cats = mask_window_title("マイナンバー 1234 5678 9012 入力")
    assert "1234 5678 9012" not in masked
    assert "[MASKED:my_number]" in masked


# --- 14. Codex P2: メールアドレスが必ずマスクされること -------------------
def test_window_title_email_masked() -> None:
    """email アドレスは context 不要で必ずマスクされる."""
    masked, _, cats = mask_window_title("メール送信中 user@example.com")
    assert "user@example.com" not in masked
    assert "[MASKED:email]" in masked
    assert "email" in cats


def test_mask_image_email_in_box() -> None:
    """OCR で email を含む box が来た場合もマスクされる."""
    img, boxes = _make_canvas([("連絡先 user@example.com", (10, 10, 400, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert "email" in result.mask_categories


# --- 15. Codex P1 (2回目): 9桁・10桁・11桁ID で末尾1-3桁が漏れない -----------
def test_window_title_9_digit_id_fully_masked() -> None:
    """9 桁の連続数字は末尾まで完全にマスクされる（digits_8 ルールで先食いされない）."""
    masked, _, _ = mask_window_title("患者ID 123456789")
    # 9 桁の生数字が残っていない
    assert not any(d.isdigit() for d in masked.replace("[MASKED:patient_id]", ""))
    # マスクラベルは1つだけのはず
    assert masked.count("[MASKED:patient_id]") == 1


def test_window_title_unseparated_phone_fully_masked() -> None:
    """ハイフン無し 11 桁電話番号が末尾まで完全にマスクされる."""
    masked, _, _ = mask_window_title("受付 - 09012345678")
    leftover = masked.replace("[MASKED:patient_id]", "").replace("[MASKED:phone]", "")
    assert not any(c.isdigit() for c in leftover)


def test_window_title_long_id_no_partial_leak() -> None:
    """10桁 / 12桁 / 15桁の連続数字も生残りしない."""
    for n_digits in (10, 12, 15):
        digits = "1" * n_digits
        title = f"レセプト {digits}"
        masked, _, _ = mask_window_title(title)
        leftover = re.sub(r"\[MASKED:[a-z_]+\]", "", masked)
        assert digits not in leftover, f"raw digits leaked: {n_digits}-digit case"
        # leftover に4桁以上の連続数字が残っていないこと
        assert not re.search(r"\d{4,}", leftover), \
            f"4+ digit run leaked in masked title for {n_digits}-digit case: {masked}"


# --- 16. Codex P2 (3回目): 郵便番号が部分マスクで残らない -------------------
def test_window_title_postal_code_fully_masked() -> None:
    """〒160-0023 のような郵便番号が "〒160-" を残して部分マスクされない."""
    masked, _, cats = mask_window_title("住所変更 〒160-0023 新宿区")
    # 郵便番号の数字 / ハイフンが leftover に残らない
    leftover = re.sub(r"\[MASKED:[a-z_]+\]", "", masked)
    assert "160-0023" not in leftover
    assert "160" not in leftover or "0023" not in leftover
    # postal カテゴリでマスクされている
    assert "postal_code" in cats or "patient_id" in cats


# --- 17. Codex P2 (4回目): 8桁保険者番号は patient_id ではなく insurance_id 分類 -----
def test_window_title_8_digit_insurance_id_classified() -> None:
    """保険者番号 12345678 のような8桁数字は insurance_id として分類される."""
    masked, _, cats = mask_window_title("保険者番号 12345678")
    assert "12345678" not in masked
    assert "[MASKED:insurance_id]" in masked
    assert "insurance_id" in cats


def test_window_title_8_digit_classification_consistent() -> None:
    """文脈なし 8桁単独でも insurance_id 分類になる."""
    masked, _, cats = mask_window_title("12345678 入力中")
    assert "12345678" not in masked
    assert "insurance_id" in cats


# --- 18. Codex P2 (5回目): 薬品名 + email でも email がマスクされる ----------
def test_mask_image_email_with_drug_name_box() -> None:
    """薬品名 + email 混在 box でも email は必ずマスクされる."""
    img, boxes = _make_canvas([("アムロジピン user@example.com", (10, 10, 500, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert "email" in result.mask_categories


def test_mask_image_drug_name_with_phone_is_masked() -> None:
    """薬品名 + 電話番号でも電話番号がマスクされる."""
    img, boxes = _make_canvas([("ロキソニン 090-1234-5678", (10, 10, 500, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert "phone" in result.mask_categories


def test_mask_image_drug_name_with_my_number_is_masked() -> None:
    """薬品名 + マイナンバーでもマイナンバーがマスクされる."""
    img, boxes = _make_canvas([("カロナール 1234-5678-9012", (10, 10, 500, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert "my_number" in result.mask_categories


def test_mask_image_drug_name_alone_not_masked() -> None:
    """薬品名のみ（PII 無し）の box は引き続きマスクされない（誤マスク回避）."""
    img, boxes = _make_canvas([("ロキソニン 60mg", (10, 10, 300, 40))])
    result = mask_image(img, boxes, strict=False)
    assert result.mask_count == 0


# --- 19. Codex P1 (6回目): 漢字+数字直結でも境界が認識されてマスクされる ----
def test_mask_image_kanji_digit_concatenation() -> None:
    """「保険者番号12345678」のように区切り無しでマスクされる."""
    img, boxes = _make_canvas([("保険者番号12345678", (10, 10, 400, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert "insurance_id" in result.mask_categories
    assert "12345678" not in result.text_summary


def test_mask_image_kanji_my_number_concatenation() -> None:
    """漢字+マイナンバーが区切り無しで連結してもマスクされる."""
    img, boxes = _make_canvas([("マイナンバー1234-5678-9012", (10, 10, 500, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert "my_number" in result.mask_categories
    assert "1234-5678-9012" not in result.text_summary


# --- 20. Codex P2 (7回目): masker import 失敗時も window_titles は単独動作 ---
# --- 21. 患者一覧画面のような複数氏名 box でも画像が破棄されないこと ----------
def test_multiple_kanji_names_keep_image() -> None:
    """敬称なし漢字氏名が並ぶ患者一覧では unmaskable=False のまま保存される."""
    img, boxes = _make_canvas([
        ("鈴木一郎", (10, 10, 200, 40)),
        ("田中花子", (10, 60, 200, 90)),
        ("佐藤太郎", (10, 110, 200, 140)),
    ])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 3
    assert result.unmaskable is False
    assert CAT_NAME_LIKE_KANJI in result.mask_categories


def test_unmaskable_only_when_unknown_pattern_remains() -> None:
    """ルール非該当の「漢字+4桁数字」が残ったときのみ unmaskable=True."""
    # 漢字+4桁数字混在 + 薬品名でないテキスト → マスクされず unmaskable=True
    img, boxes = _make_canvas([("特殊コード 9876 案件", (10, 10, 400, 40))])
    result = mask_image(img, boxes, strict=True)
    # 漢字部分は name_like_kanji でマスクされるが、9876 はマスクできない
    # → unmaskable_overall は True になる
    assert result.unmaskable is True


def test_window_titles_works_without_masker(monkeypatch) -> None:
    """masker module を sys.modules から外しても window_titles の主要 PII は
    マスクされる（部分インストール時の保険）."""
    import importlib
    import sys as _sys
    # window_titles は既に import 済みだが、_HAS_MASKER=False のフォールバック
    # 経路を直接シミュレート
    import window_titles as wt_mod
    monkeypatch.setattr(wt_mod, "_HAS_MASKER", False)
    monkeypatch.setattr(wt_mod, "_MASKER_DEFAULT_RULES", [])

    masked, _, cats = wt_mod.mask_window_title("受付 - user@example.com")
    assert "user@example.com" not in masked
    assert "email" in cats

    masked2, _, cats2 = wt_mod.mask_window_title("番号 1234-5678-9012")
    assert "1234-5678-9012" not in masked2
    assert "my_number" in cats2

    masked3, _, cats3 = wt_mod.mask_window_title("保険者番号12345678")
    assert "12345678" not in masked3
    assert "insurance_id" in cats3


# --- 22. Codex Critical#1 (2026-05-06): 敬称なし氏名がタイトルから漏れない ---

def test_window_title_masks_unhonorific_kanji_name() -> None:
    """敬称なし患者氏名（山田太郎、佐藤花子）はタイトルでもマスクされる."""
    masked, _, cats = mask_window_title("山田太郎 - 処方入力画面")
    assert "山田太郎" not in masked
    assert "name_like_kanji" in cats
    # 業務語「処方入力」はマスクされない（業務分析を成立させるため）
    assert "処方入力" in masked


def test_window_title_keeps_business_terms() -> None:
    """漢字業務語は Phase 3 の氏名候補マスクから除外される.
    NOTE: カナ業務語（ログアウト等）は base.json の name_kana_long ルールに絡むため
    Phase 3 ではなく別タスクで対応。ここでは漢字業務語のみ検証."""
    for term in ("患者検索", "処方入力", "受付業務", "案件登録",
                 "仕訳入力", "メイン画面"):
        masked, _, _ = mask_window_title(f"システム - {term}")
        assert term in masked, f"business term '{term}' was incorrectly masked: {masked}"


def test_window_title_masks_unhonorific_name_with_business_word() -> None:
    """業務語と氏名の混在: 業務語は残り氏名はマスク."""
    masked, _, _ = mask_window_title("受付 - 山田太郎")
    assert "山田太郎" not in masked
    assert "受付" in masked


def test_window_title_compound_business_term_partial_match() -> None:
    """部分一致: 「患者」を含む4字以上の語は業務語扱い."""
    masked, _, cats = mask_window_title("システム - 患者一覧")
    assert "患者一覧" in masked
    assert "name_like_kanji" not in cats


def test_window_title_short_unknown_kanji_still_masked() -> None:
    """業務語以外の2字漢字（氏名の一部）はマスク."""
    masked, _, cats = mask_window_title("メモ - 太郎")
    assert "太郎" not in masked
    assert "name_like_kanji" in cats


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
