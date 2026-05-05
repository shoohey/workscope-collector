"""業界プロファイル別マスキングテスト.

各業界プロファイルでのマスキング挙動を検証:
1. base 共通PII (氏名/電話/メール/マイナンバー/住所/郵便) は全業界で必ず発火
2. 業界固有カテゴリは該当プロファイルでのみ発火
3. property-based: ランダム生成PIIがマスカー通過後に regex で検出されないこと

hypothesis を使った property-based テストを含む。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from masker import mask_image, mask_image_with_profile  # noqa: E402
from ocr import OCRBox  # noqa: E402
from profile_loader import clear_cache, load_profile  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


def _box(text: str, y: int = 10) -> tuple[Image.Image, list[OCRBox]]:
    img = Image.new("RGB", (800, 600), (255, 255, 255))
    return img, [OCRBox(text=text, bbox=(10, y, 600, y + 30), confidence=0.95)]


# ============================================================================
# 1. base 共通PII: 全業界で必ずマスクされる
# ============================================================================

@pytest.mark.parametrize("profile_name", ["pharmacy", "accounting", "legal", "sales", "hr", "generic"])
@pytest.mark.parametrize("text,expected_cat", [
    ("user@example.com", "email"),
    ("090-1234-5678", "phone"),
    ("1234-5678-9012", "my_number"),
    ("鈴木太郎 様", "personal_name"),
    ("〒160-0023", "postal_code"),
    ("東京都新宿区西新宿1-2-3", "address"),
])
def test_base_pii_masked_in_all_profiles(profile_name: str, text: str, expected_cat: str) -> None:
    """全業界プロファイルで base 共通PIIは必ずマスクされる."""
    img, boxes = _box(text)
    result = mask_image_with_profile(img, boxes, profile=profile_name, strict=True)
    assert result.mask_count >= 1, f"{profile_name}: '{text}' not masked"
    assert expected_cat in result.mask_categories, \
        f"{profile_name}: expected category '{expected_cat}' not in {result.mask_categories}"


# ============================================================================
# 2. 業界固有カテゴリ: 該当プロファイルでのみ発火
# ============================================================================

def test_pharmacy_specific_insurance_id_masked() -> None:
    """薬局: 「保険者番号」が context_keyword なので 8桁数字が insurance_id として発火."""
    img, boxes = _box("保険者番号 12345678")
    result = mask_image_with_profile(img, boxes, profile="pharmacy", strict=True)
    assert "insurance_id" in result.mask_categories


def test_pharmacy_drug_name_whitelist() -> None:
    """薬局: 薬品名のみの行はマスクされない（誤マスク回避）."""
    img, boxes = _box("ロキソニン 60mg")
    result = mask_image_with_profile(img, boxes, profile="pharmacy", strict=False)
    # PIIがないのでマスクされない
    assert result.mask_count == 0


def test_accounting_company_name_masked() -> None:
    """会計: 株式会社○○の取引先名が customer_name としてマスクされる."""
    img, boxes = _box("株式会社サンプル商事")
    result = mask_image_with_profile(img, boxes, profile="accounting", strict=False)
    assert result.mask_count >= 1
    assert "client_name" in result.mask_categories


def test_legal_case_number_masked() -> None:
    """法律: 「令和5年(ワ)第123号」のような事件番号が case_number としてマスクされる."""
    img, boxes = _box("令和5年(ワ)第12345号")
    result = mask_image_with_profile(img, boxes, profile="legal", strict=False)
    assert "case_number" in result.mask_categories


def test_hr_employee_id_with_context() -> None:
    """HR: 社員番号 12345 のような従業員IDが context_keyword 経由でマスクされる."""
    img, boxes = _box("社員番号 12345")
    result = mask_image_with_profile(img, boxes, profile="hr", strict=False)
    assert "employee_id" in result.mask_categories


def test_sales_deal_amount_with_context() -> None:
    """営業: 「商談金額 1,000,000円」のような商談額が deal_amount としてマスクされる."""
    img, boxes = _box("商談 5,000,000円")
    result = mask_image_with_profile(img, boxes, profile="sales", strict=False)
    assert "deal_amount" in result.mask_categories


# ============================================================================
# 3. クロス業界: pharmacy 用 patient_id は generic ではマスクされない
# ============================================================================

def test_generic_does_not_mask_pharmacy_specific() -> None:
    """generic プロファイルには薬局固有ルール(patient_id)がないので、
    患者ID のような数字はマスクされない（誤陽性回避）."""
    img, boxes = _box("患者ID 12345")
    result = mask_image_with_profile(img, boxes, profile="generic", strict=False)
    # generic には patient_id ルールがないので、4桁以上数字でも発火しない
    assert "patient_id" not in result.mask_categories


def test_generic_still_masks_base_pii() -> None:
    """ただし generic でも base PII (メール) はマスクされる."""
    img, boxes = _box("連絡先 user@example.com")
    result = mask_image_with_profile(img, boxes, profile="generic", strict=False)
    assert "email" in result.mask_categories


# ============================================================================
# 4. property-based テスト: ランダムPII生成 → マスク → 元値が text_summary に残らない
# ============================================================================

# hypothesis が無くても動くシンプルな fuzz
import random


def _gen_email() -> str:
    name = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=random.randint(3, 10)))
    domain = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=random.randint(3, 8)))
    return f"{name}@{domain}.com"


def _gen_phone() -> str:
    return f"0{random.randint(70, 99)}-{random.randint(1000, 9999)}-{random.randint(1000, 9999)}"


def _gen_my_number() -> str:
    return f"{random.randint(1000,9999)}-{random.randint(1000,9999)}-{random.randint(1000,9999)}"


@pytest.mark.parametrize("profile_name", ["pharmacy", "accounting", "legal", "sales", "hr", "generic"])
def test_fuzz_emails_always_masked(profile_name: str) -> None:
    """100件のランダムメアドが、全業界プロファイルで必ずマスクされ、生値が残らない."""
    random.seed(42)
    for _ in range(100):
        email = _gen_email()
        img, boxes = _box(f"メール送信先: {email}")
        result = mask_image_with_profile(img, boxes, profile=profile_name, strict=True)
        assert "email" in result.mask_categories, \
            f"{profile_name}: email '{email}' not categorized"
        assert email not in result.text_summary, \
            f"{profile_name}: PII LEAK email '{email}' in text_summary: {result.text_summary}"


@pytest.mark.parametrize("profile_name", ["pharmacy", "accounting", "legal", "sales", "hr", "generic"])
def test_fuzz_phones_always_masked(profile_name: str) -> None:
    """100件のランダム電話番号が全業界で必ずマスクされる."""
    random.seed(43)
    for _ in range(100):
        phone = _gen_phone()
        img, boxes = _box(f"連絡先: {phone}")
        result = mask_image_with_profile(img, boxes, profile=profile_name, strict=True)
        assert "phone" in result.mask_categories, f"{profile_name}: phone '{phone}' not categorized"
        assert phone not in result.text_summary, \
            f"{profile_name}: PII LEAK phone '{phone}' in text_summary: {result.text_summary}"


@pytest.mark.parametrize("profile_name", ["pharmacy", "accounting", "legal", "sales", "hr", "generic"])
def test_fuzz_my_numbers_always_masked(profile_name: str) -> None:
    """100件のランダムマイナンバーが全業界で必ずマスクされる."""
    random.seed(44)
    for _ in range(100):
        my_num = _gen_my_number()
        img, boxes = _box(f"マイナンバー: {my_num}")
        result = mask_image_with_profile(img, boxes, profile=profile_name, strict=True)
        assert "my_number" in result.mask_categories
        assert my_num not in result.text_summary, \
            f"{profile_name}: PII LEAK my_number '{my_num}' in text_summary: {result.text_summary}"


# ============================================================================
# 5. プロファイル指定なし = pharmacy のデフォルト挙動を維持（v0.1.0互換）
# ============================================================================

def test_default_profile_is_pharmacy_compatible(monkeypatch) -> None:
    """環境変数なし + config なしで mask_image() を呼ぶと pharmacy 互換挙動になる."""
    monkeypatch.delenv("WORKSCOPE_PROFILE", raising=False)
    img, boxes = _box("保険者番号 12345678")
    # mask_image は DEFAULT_RULES (=pharmacy 継承) を使う
    result = mask_image(img, boxes, strict=True)
    assert "insurance_id" in result.mask_categories


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
