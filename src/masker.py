"""業界プロファイル駆動の個人情報マスキング.

v1.0で profile_loader 経由のプロファイル駆動に移行。
v0.1.0で薬局向けにハードコードしていた DEFAULT_RULES / COMMON_DRUG_NAMES /
CAT_* 定数は後方互換のため維持。

設計方針:
- 漏洩 = 事業停止リスクなので、`strict=True` 時は迷ったらマスクする
- ルールベース（regex + 文脈キーワード）で説明可能性を担保
- 周辺ボックス文脈: 「保険者番号」boxの右隣/下隣の数字列もマスク対象に格上げ
- プロファイル切替: load_profile("pharmacy"|"accounting"|...) で業界別ルール集合を取得
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
from PIL import Image, ImageDraw

try:
    from .ocr import OCRBox
except ImportError:  # pragma: no cover - allow `from masker import ...`
    from ocr import OCRBox  # type: ignore[no-redef]

try:
    from .profile_loader import MaskRule, Profile, load_profile, get_default_profile_name
except ImportError:  # pragma: no cover
    from profile_loader import MaskRule, Profile, load_profile, get_default_profile_name  # type: ignore[no-redef]


logger = logging.getLogger(__name__)


# --- カテゴリ定数（v0.1.0 互換: 既存テスト 31本 / window_titles.py がimport） ---
CAT_PATIENT_NAME = "personal_name"            # v0.1.0時点では "patient_name" だったが
CAT_PATIENT_NAME_KANA = "personal_name_kana"  # v1.0で base に格上げ → 全業界共通カテゴリに
CAT_INSURANCE_ID = "insurance_id"
CAT_INSURANCE_CARD = "insurance_card_no"
CAT_PATIENT_ID = "patient_id"
CAT_BIRTHDATE = "birthdate"
CAT_PHONE = "phone"
CAT_ADDRESS = "address"
CAT_POSTAL = "postal_code"
CAT_MY_NUMBER = "my_number"
CAT_PRESCRIPTION_WITH_ID = "prescription_with_id"
CAT_NAME_LIKE_KANJI = "name_like_kanji"  # strict modeのヒューリスティック
CAT_EMAIL = "email"


# --- 既存テスト互換のため、v0.1.0と同じ正規表現も module level で維持 ---
# (window_titles.py がフォールバックで参照する)
RE_NAME_KANJI_HONORIFIC = re.compile(r"[一-鿿々]{2,5}\s?(?:様|さん|殿|氏)")
RE_NAME_KANA_HONORIFIC = re.compile(r"[゠-ヿぁ-ゟー]{2,10}\s?(?:様|さん|殿|氏)")
RE_KANA_LONG = re.compile(r"[゠-ヿ]{3,}[　\s]?[゠-ヿ]{2,}")
RE_DATE_SLASH = re.compile(
    r"(?:19|20)\d{2}[\-/.]\s?(?:0?[1-9]|1[0-2])[\-/.]\s?(?:0?[1-9]|[12]\d|3[01])"
)
RE_DATE_KANJI = re.compile(
    r"(?:19|20)\d{2}\s?年\s?(?:0?[1-9]|1[0-2])\s?月\s?(?:0?[1-9]|[12]\d|3[01])\s?日"
)
RE_DATE_WAREKI = re.compile(
    r"(?:明治|大正|昭和|平成|令和)\s?\d{1,2}\s?年\s?\d{1,2}\s?月\s?\d{1,2}\s?日"
)
RE_PHONE = re.compile(r"0\d{1,4}[-(]?\d{1,4}[-)]?\d{3,4}")
RE_POSTAL = re.compile(r"(?<!\d)〒?\s?\d{3}-?\d{4}(?!\d)")
RE_ADDRESS = re.compile(
    r"(?:北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|"
    r"埼玉県|千葉県|東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|"
    r"岐阜県|静岡県|愛知県|三重県|滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|"
    r"鳥取県|島根県|岡山県|広島県|山口県|徳島県|香川県|愛媛県|高知県|福岡県|"
    r"佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県)"
    r"[一-鿿ぁ-ゟ゠-ヿ\w]{1,30}?\d+(?:-\d+){0,3}"
)
RE_DIGITS_8 = re.compile(r"(?<!\d)\d{8}(?!\d)")
RE_INSURANCE_CARD = re.compile(r"\d{6,10}[-－]\d{1,4}")
RE_MY_NUMBER = re.compile(r"(?<!\d)\d{4}[-\s]?\d{4}[-\s]?\d{4}(?!\d)")
RE_DIGITS_RUN = re.compile(r"\d{4,}")
RE_KANJI_RUN = re.compile(r"[一-鿿々]{2,5}")
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


# --- v0.1.0互換キーワード群（pharmacyプロファイルから抽出） ---
KEYWORDS_INSURANCE = ("保険者番号", "保険番号", "保険者", "記号番号", "記号", "番号")
KEYWORDS_PATIENT_ID = ("患者ID", "患者id", "カルテNo", "カルテno", "カルテ番号", "ID", "No.", "受付番号")
KEYWORDS_MY_NUMBER = ("マイナンバー", "個人番号")
KEYWORDS_BIRTHDATE = ("生年月日", "生年", "誕生日")
KEYWORDS_PHONE = ("電話", "TEL", "Tel", "携帯")
KEYWORDS_ADDRESS = ("住所", "現住所", "所在地")


# v0.1.0互換: pharmacy プロファイルの whitelist["drug_names"] を引き継ぐ
COMMON_DRUG_NAMES: tuple[str, ...] = ()


def _bootstrap_default_rules() -> tuple[list[MaskRule], tuple[str, ...]]:
    """デフォルトプロファイル(=pharmacy v0.1.0互換)からルールと薬品名を取得.

    profile_loader が読み込めない/プロファイルがない場合は空リストにフォールバック
    （window_titles.py は単独で動作するため、最終的なPII漏洩は防げる）
    """
    try:
        profile_name = get_default_profile_name() or "pharmacy"
        profile = load_profile(profile_name)
        drugs = tuple(profile.whitelist.get("drug_names") or ())
        return list(profile.rules), drugs
    except Exception as exc:  # noqa: BLE001
        logger.warning("profile bootstrap failed (%s); DEFAULT_RULES will be empty", exc)
        return [], ()


DEFAULT_RULES, COMMON_DRUG_NAMES = _bootstrap_default_rules()


# --- 結果型 ----------------------------------------------------------------

@dataclass
class MaskResult:
    """マスキング結果."""

    masked_image: Image.Image
    text_summary: str  # マスク済みテキストの連結
    mask_count: int
    mask_categories: list[str] = field(default_factory=list)
    unmaskable: bool = False


# --- 内部ユーティリティ ----------------------------------------------------

def _has_any_keyword(text: str, keywords: Iterable[str]) -> bool:
    return any(kw in text for kw in keywords)


def _is_drug_name_only(text: str, drug_names: Iterable[str] = ()) -> bool:
    """テキストが薬品名のみ（PIIを含まない）かを判定.

    薬品名ホワイトリストを「マスクしない」根拠にしているので、
    PII らしき表現（数字列・メール・電話・敬称・郵便・マイナンバー）が
    1 つでも混在していたら False を返して通常のルール評価に回す。
    """
    drug_pool = drug_names or COMMON_DRUG_NAMES
    if not drug_pool:
        return False
    has_drug = any(d in text for d in drug_pool)
    if not has_drug:
        return False
    if RE_DIGITS_RUN.search(text):
        return False
    if RE_EMAIL.search(text):
        return False
    if RE_PHONE.search(text):
        return False
    if RE_POSTAL.search(text):
        return False
    if RE_MY_NUMBER.search(text):
        return False
    if RE_NAME_KANJI_HONORIFIC.search(text):
        return False
    if RE_NAME_KANA_HONORIFIC.search(text):
        return False
    return True


def _box_distance(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax = (a[0] + a[2]) / 2.0
    ay = (a[1] + a[3]) / 2.0
    bx = (b[0] + b[2]) / 2.0
    by = (b[1] + b[3]) / 2.0
    return float(((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)


def _is_neighbor(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """右隣 or 下隣（同一行 or 直下行）に近接しているか."""
    ah = a[3] - a[1]
    aw = a[2] - a[0]
    horiz_overlap = not (a[3] < b[1] or b[3] < a[1])
    if horiz_overlap and b[0] >= a[0] and b[0] - a[2] <= max(aw, 200):
        return True
    vert_overlap = not (a[2] < b[0] or b[2] < a[0])
    if vert_overlap and b[1] >= a[1] and b[1] - a[3] <= max(ah * 2, 60):
        return True
    return False


def _expand_categories_via_context(boxes: list[OCRBox]) -> dict[int, set[str]]:
    """boxごとに「近傍にあるキーワードboxから派生する追加カテゴリ」を返す."""
    out: dict[int, set[str]] = {i: set() for i in range(len(boxes))}
    keyword_map = [
        (KEYWORDS_INSURANCE, CAT_INSURANCE_ID),
        (KEYWORDS_PATIENT_ID, CAT_PATIENT_ID),
        (KEYWORDS_MY_NUMBER, CAT_MY_NUMBER),
        (KEYWORDS_BIRTHDATE, CAT_BIRTHDATE),
        (KEYWORDS_PHONE, CAT_PHONE),
        (KEYWORDS_ADDRESS, CAT_ADDRESS),
    ]
    for i, src in enumerate(boxes):
        for kws, cat in keyword_map:
            if _has_any_keyword(src.text, kws):
                for j, dst in enumerate(boxes):
                    if i == j:
                        continue
                    if _is_neighbor(src.bbox, dst.bbox):
                        out[j].add(cat)
    return out


def _classify_box(
    box: OCRBox,
    extra_cats: set[str],
    rules: list[MaskRule],
    strict: bool,
    drug_names: Iterable[str] = (),
) -> tuple[list[str], bool]:
    """boxを分類し (該当カテゴリ列, unmaskable疑い) を返す."""
    text = box.text or ""
    if not text.strip():
        return [], False

    if _is_drug_name_only(text, drug_names):
        return [], False

    matched: list[str] = []
    for rule in rules:
        if rule.pattern.search(text):
            if rule.context_keywords:
                if (
                    _has_any_keyword(text, rule.context_keywords)
                    or rule.category in extra_cats
                ):
                    matched.append(rule.category)
            else:
                matched.append(rule.category)

    if not matched and extra_cats:
        if RE_DIGITS_RUN.search(text):
            matched.extend(extra_cats & {CAT_PATIENT_ID, CAT_INSURANCE_ID, CAT_MY_NUMBER})
        if extra_cats & {CAT_BIRTHDATE} and (
            RE_DATE_SLASH.search(text) or RE_DATE_KANJI.search(text) or RE_DATE_WAREKI.search(text)
            or re.search(r"\d", text)
        ):
            matched.append(CAT_BIRTHDATE)
        if extra_cats & {CAT_ADDRESS} and re.search(r"[一-鿿]", text):
            matched.append(CAT_ADDRESS)
        if extra_cats & {CAT_PHONE} and re.search(r"\d", text):
            matched.append(CAT_PHONE)

    if strict and not matched:
        stripped = text.strip()
        if RE_KANJI_RUN.fullmatch(stripped) and 2 <= len(stripped) <= 5:
            matched.append(CAT_NAME_LIKE_KANJI)

    return list(dict.fromkeys(matched)), False


def _draw_black_rect(img: Image.Image, bbox: tuple[int, int, int, int]) -> None:
    draw = ImageDraw.Draw(img)
    x1, y1, x2, y2 = bbox
    draw.rectangle(
        [max(x1 - 2, 0), max(y1 - 2, 0), x2 + 2, y2 + 2],
        fill=(0, 0, 0),
    )


def _summary_label(categories: list[str]) -> str:
    if not categories:
        return "[MASKED]"
    return f"[MASKED:{categories[0]}]"


def _sort_boxes_reading_order(boxes: list[OCRBox]) -> list[int]:
    """上から下→左から右の順でindexを返す."""
    indexed = list(enumerate(boxes))
    if not indexed:
        return []
    avg_h = sum(b.bbox[3] - b.bbox[1] for _, b in indexed) / len(indexed) or 1
    indexed.sort(key=lambda kv: (round(kv[1].bbox[1] / max(avg_h * 0.6, 1)), kv[1].bbox[0]))
    return [i for i, _ in indexed]


# --- 公開API --------------------------------------------------------------

def mask_image(
    image: Image.Image,
    ocr_boxes: list[OCRBox],
    strict: bool = True,
    rules: list[MaskRule] | None = None,
    drug_names: Iterable[str] | None = None,
) -> MaskResult:
    """OCR結果を元に画像へ黒塗りを適用し、マスク済みテキスト要約も返す.

    rules を省略するとモジュール初期化時に解決した DEFAULT_RULES を使用
    （v0.1.0互換: pharmacyプロファイル）。
    drug_names を省略すると pharmacyプロファイルの whitelist["drug_names"] を使用。
    """
    if rules is None:
        rules = DEFAULT_RULES
    drugs = tuple(drug_names) if drug_names is not None else COMMON_DRUG_NAMES

    if isinstance(image, np.ndarray):
        out_img = Image.fromarray(image).convert("RGB")
    else:
        out_img = image.convert("RGB").copy()

    extra = _expand_categories_via_context(ocr_boxes)

    classifications: list[tuple[OCRBox, list[str], bool]] = []
    mask_count = 0
    all_categories: list[str] = []
    unmaskable_overall = False

    for i, box in enumerate(ocr_boxes):
        cats, unmaskable = _classify_box(box, extra.get(i, set()), rules, strict, drugs)
        classifications.append((box, cats, unmaskable))
        if cats:
            _draw_black_rect(out_img, box.bbox)
            mask_count += 1
            all_categories.extend(cats)
            if unmaskable:
                unmaskable_overall = True

    order = _sort_boxes_reading_order([c[0] for c in classifications])
    parts: list[str] = []
    for idx in order:
        box, cats, _ = classifications[idx]
        if cats:
            parts.append(_summary_label(cats))
        else:
            parts.append(box.text)
    text_summary = " ".join(p for p in parts if p)
    if len(text_summary) > 2000:
        text_summary = text_summary[:2000]

    if strict and not unmaskable_overall:
        for box, cats, _ in classifications:
            if cats:
                continue
            t = box.text
            if (
                re.search(r"[一-鿿]", t)
                and re.search(r"\d{4,}", t)
                and not _is_drug_name_only(t, drugs)
            ):
                unmaskable_overall = True
                break

    deduped_cats = list(dict.fromkeys(all_categories))

    return MaskResult(
        masked_image=out_img,
        text_summary=text_summary,
        mask_count=mask_count,
        mask_categories=deduped_cats,
        unmaskable=unmaskable_overall,
    )


def mask_image_with_profile(
    image: Image.Image,
    ocr_boxes: list[OCRBox],
    profile: Profile | str,
    strict: bool = True,
) -> MaskResult:
    """業界プロファイルを指定してマスキング.

    profile は Profile オブジェクトまたはプロファイル名（"pharmacy" 等）。
    """
    if isinstance(profile, str):
        profile = load_profile(profile)
    drugs = tuple(profile.whitelist.get("drug_names") or ())
    return mask_image(image, ocr_boxes, strict=strict, rules=profile.rules, drug_names=drugs)


def reload_default_rules() -> None:
    """テスト/設定変更時にデフォルトルールを再解決."""
    global DEFAULT_RULES, COMMON_DRUG_NAMES
    DEFAULT_RULES, COMMON_DRUG_NAMES = _bootstrap_default_rules()


__all__ = [
    "MaskRule",
    "MaskResult",
    "DEFAULT_RULES",
    "COMMON_DRUG_NAMES",
    "mask_image",
    "mask_image_with_profile",
    "reload_default_rules",
    # カテゴリ定数
    "CAT_PATIENT_NAME",
    "CAT_PATIENT_NAME_KANA",
    "CAT_INSURANCE_ID",
    "CAT_INSURANCE_CARD",
    "CAT_PATIENT_ID",
    "CAT_BIRTHDATE",
    "CAT_PHONE",
    "CAT_ADDRESS",
    "CAT_POSTAL",
    "CAT_MY_NUMBER",
    "CAT_NAME_LIKE_KANJI",
    "CAT_EMAIL",
]
