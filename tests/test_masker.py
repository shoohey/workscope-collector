"""masker.py のテスト. OCR は使わず OCRBox を手で組んで mask_image を検証.

実装方針:
- 各テキストごとに 80x30 の白画像と OCRBox を作る
- mask_image を呼び、結果 image の対象矩形が黒く塗られていることをピクセル確認
- text_summary に [MASKED:<category>] が含まれることを確認
"""

from __future__ import annotations

import os
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
from window_titles_mask import mask_window_title  # noqa: E402


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
    """strict modeでは敬称なしの漢字2-5文字もマスクする（保険）."""
    img, boxes = _make_canvas([("田中花子", (10, 10, 200, 40))])
    result = mask_image(img, boxes, strict=True)
    assert result.mask_count >= 1
    assert CAT_NAME_LIKE_KANJI in result.mask_categories
    assert result.unmaskable is True


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
    required = {
        "patient_name",
        "insurance_id",
        "insurance_card_no",
        "patient_id",
        "birthdate",
        "phone",
        "address",
        "postal_code",
        "my_number",
    }
    covered = {r.category for r in DEFAULT_RULES}
    missing = required - covered
    assert not missing, f"DEFAULT_RULES に不足: {missing}"


# --- 12. window_titles_mask ---
def test_window_title_mask_phone() -> None:
    masked, h, cats = mask_window_title("受付 - 090-1234-5678")
    assert "[MASKED:phone]" in masked
    assert "phone" in cats
    assert len(h) == 8


def test_window_title_mask_name_kanji() -> None:
    masked, h, cats = mask_window_title("カルテ表示 - 鈴木太郎 様")
    assert "[MASKED:" in masked
    assert any(c in cats for c in ("patient_name", "name_like_kanji"))


def test_window_title_empty() -> None:
    masked, h, cats = mask_window_title("")
    assert masked == ""
    assert h == ""
    assert cats == []


def test_window_title_no_pii() -> None:
    masked, h, cats = mask_window_title("ABC")
    assert masked == "ABC"
    assert len(h) == 8
    assert cats == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
