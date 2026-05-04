"""薬局レセコン特化の個人情報マスキング.

OCR で抽出した OCRBox 群を走査し、患者氏名・保険者番号・生年月日・
電話番号・住所・マイナンバー等を黒塗り矩形で塗りつぶす。

設計方針:
- 漏洩 = 事業停止リスクなので、`strict=True` 時は迷ったらマスクする
- ルールベース（regex + 文脈キーワード）で説明可能性を担保
- 周辺ボックス文脈: 「保険者番号」boxの右隣/下隣の数字列もマスク対象に格上げ
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
from PIL import Image, ImageDraw

try:
    from .ocr import OCRBox
except ImportError:  # pragma: no cover - allow `from masker import ...`
    from ocr import OCRBox  # type: ignore[no-redef]


# --- カテゴリ定数 ---
CAT_PATIENT_NAME = "patient_name"
CAT_PATIENT_NAME_KANA = "patient_name_kana"
CAT_INSURANCE_ID = "insurance_id"
CAT_INSURANCE_CARD = "insurance_card_no"
CAT_PATIENT_ID = "patient_id"
CAT_BIRTHDATE = "birthdate"
CAT_PHONE = "phone"
CAT_ADDRESS = "address"
CAT_POSTAL = "postal_code"
CAT_MY_NUMBER = "my_number"
CAT_PRESCRIPTION_WITH_ID = "prescription_with_id"
CAT_NAME_LIKE_KANJI = "name_like_kanji"  # strict mode のヒューリスティック


# --- 個別正規表現 ---
# 漢字氏名 + 敬称
RE_NAME_KANJI_HONORIFIC = re.compile(r"[一-鿿々]{2,5}\s?(?:様|さん|殿|氏)")
# カナ氏名 + 敬称
RE_NAME_KANA_HONORIFIC = re.compile(r"[゠-ヿぁ-ゟー]{2,10}\s?(?:様|さん|殿|氏)")
# カナ氏名（ふりがな単独・3文字以上）
RE_KANA_LONG = re.compile(r"[゠-ヿ]{3,}[　\s]?[゠-ヿ]{2,}")  # フル幅スペース許容

# 生年月日
RE_DATE_SLASH = re.compile(
    r"(?:19|20)\d{2}[\-/.]\s?(?:0?[1-9]|1[0-2])[\-/.]\s?(?:0?[1-9]|[12]\d|3[01])"
)
RE_DATE_KANJI = re.compile(
    r"(?:19|20)\d{2}\s?年\s?(?:0?[1-9]|1[0-2])\s?月\s?(?:0?[1-9]|[12]\d|3[01])\s?日"
)
RE_DATE_WAREKI = re.compile(
    r"(?:明治|大正|昭和|平成|令和)\s?\d{1,2}\s?年\s?\d{1,2}\s?月\s?\d{1,2}\s?日"
)

# 電話番号
RE_PHONE = re.compile(r"0\d{1,4}[-(]?\d{1,4}[-)]?\d{3,4}")

# 郵便番号
RE_POSTAL = re.compile(r"〒?\s?\d{3}-?\d{4}")

# 住所（都道府県+市区町村+番地）
RE_ADDRESS = re.compile(
    r"(?:北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|"
    r"埼玉県|千葉県|東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|"
    r"岐阜県|静岡県|愛知県|三重県|滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|"
    r"鳥取県|島根県|岡山県|広島県|山口県|徳島県|香川県|愛媛県|高知県|福岡県|"
    r"佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県)"
    r"[一-鿿ぁ-ゟ゠-ヿ\w]{1,30}?\d+(?:-\d+){0,3}"
)

# 保険者番号 / 記号番号 / カルテ No 近傍の数字列
RE_DIGITS_8 = re.compile(r"\d{8}")
RE_INSURANCE_CARD = re.compile(r"\d{6,10}[-－]\d{1,4}")
RE_MY_NUMBER = re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}\b")
RE_DIGITS_RUN = re.compile(r"\d{4,}")  # 4桁以上の数字（ID候補）

# 漢字連続（人名らしき）: strictモード用ヒューリスティック
RE_KANJI_RUN = re.compile(r"[一-鿿々]{2,5}")

# 文脈キーワード（box内に登場すれば数字列をその種別に格上げ）
KEYWORDS_INSURANCE = ("保険者番号", "保険番号", "保険者", "記号番号", "記号", "番号")
KEYWORDS_PATIENT_ID = ("患者ID", "患者id", "カルテNo", "カルテno", "カルテ番号", "ID", "No.", "受付番号")
KEYWORDS_MY_NUMBER = ("マイナンバー", "個人番号")
KEYWORDS_BIRTHDATE = ("生年月日", "生年", "誕生日")
KEYWORDS_PHONE = ("電話", "TEL", "Tel", "携帯")
KEYWORDS_ADDRESS = ("住所", "現住所", "所在地")

# 薬品名（誤マスク防止用ホワイトリスト・代表例）
COMMON_DRUG_NAMES = (
    "アムロジピン", "ロキソニン", "カロナール", "メトホルミン", "アスピリン",
    "ロサルタン", "アトルバスタチン", "オメプラゾール", "ファモチジン",
    "クラリスロマイシン", "セフカペン", "ムコダイン", "ムコソルバン",
    "リピトール", "ノルバスク", "メバロチン", "ガスター",
)


@dataclass
class MaskRule:
    """1個のマスキングルール."""

    name: str
    pattern: re.Pattern[str]
    category: str
    context_keywords: tuple[str, ...] = ()  # 近傍boxにあれば優先発火


DEFAULT_RULES: list[MaskRule] = [
    # 1. 患者氏名
    MaskRule("name_kanji_honorific", RE_NAME_KANJI_HONORIFIC, CAT_PATIENT_NAME),
    MaskRule("name_kana_honorific", RE_NAME_KANA_HONORIFIC, CAT_PATIENT_NAME_KANA),
    MaskRule("name_kana_long", RE_KANA_LONG, CAT_PATIENT_NAME_KANA),
    # 2-4. 番号系（文脈キーワード強化）
    MaskRule("my_number", RE_MY_NUMBER, CAT_MY_NUMBER, KEYWORDS_MY_NUMBER),
    MaskRule("insurance_card_no", RE_INSURANCE_CARD, CAT_INSURANCE_CARD, KEYWORDS_INSURANCE),
    MaskRule("insurance_8digits", RE_DIGITS_8, CAT_INSURANCE_ID, KEYWORDS_INSURANCE),
    MaskRule("patient_id_digits", RE_DIGITS_RUN, CAT_PATIENT_ID, KEYWORDS_PATIENT_ID),
    # 5. 生年月日
    MaskRule("date_wareki", RE_DATE_WAREKI, CAT_BIRTHDATE),
    MaskRule("date_kanji", RE_DATE_KANJI, CAT_BIRTHDATE),
    MaskRule("date_slash", RE_DATE_SLASH, CAT_BIRTHDATE),
    # 6. 電話番号
    MaskRule("phone", RE_PHONE, CAT_PHONE),
    # 7. 住所
    MaskRule("postal", RE_POSTAL, CAT_POSTAL),
    MaskRule("address", RE_ADDRESS, CAT_ADDRESS),
]


@dataclass
class MaskResult:
    """マスキング結果."""

    masked_image: Image.Image
    text_summary: str  # マスク済みテキストの連結
    mask_count: int
    mask_categories: list[str] = field(default_factory=list)
    unmaskable: bool = False


def _has_any_keyword(text: str, keywords: Iterable[str]) -> bool:
    return any(kw in text for kw in keywords)


def _is_drug_name_only(text: str) -> bool:
    """テキストが薬品名のみ（患者IDなどを含まない）かを判定."""
    has_drug = any(d in text for d in COMMON_DRUG_NAMES)
    if not has_drug:
        return False
    # 患者ID候補（4桁以上の数字）が混在している場合は薬品名だけとは見なさない
    if RE_DIGITS_RUN.search(text):
        return False
    return True


def _box_distance(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """2つの bbox 中心間ユークリッド距離."""
    ax = (a[0] + a[2]) / 2.0
    ay = (a[1] + a[3]) / 2.0
    bx = (b[0] + b[2]) / 2.0
    by = (b[1] + b[3]) / 2.0
    return float(((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)


def _is_neighbor(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """右隣 or 下隣（同一行 or 直下行）に近接しているか."""
    ah = a[3] - a[1]
    aw = a[2] - a[0]
    # 右隣: 縦位置がほぼ同じ で 横方向の隙間が幅の3倍以内
    horiz_overlap = not (a[3] < b[1] or b[3] < a[1])
    if horiz_overlap and b[0] >= a[0] and b[0] - a[2] <= max(aw, 200):
        return True
    # 下隣: 横位置がほぼ重なり で 縦方向の隙間が高さの2倍以内
    vert_overlap = not (a[2] < b[0] or b[2] < a[0])
    if vert_overlap and b[1] >= a[1] and b[1] - a[3] <= max(ah * 2, 60):
        return True
    return False


def _expand_categories_via_context(
    boxes: list[OCRBox],
) -> dict[int, set[str]]:
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
) -> tuple[list[str], bool]:
    """boxを分類し (該当カテゴリ列, unmaskable疑い) を返す.

    マッチがあれば該当カテゴリ全件を返す。strict mode では「人名らしき漢字連続」も
    フォールバックでマスク対象に含める。
    """
    text = box.text or ""
    if not text.strip():
        return [], False

    # 薬品名単独（患者IDなし）はマスクしない
    if _is_drug_name_only(text):
        return [], False

    matched: list[str] = []
    for rule in rules:
        if rule.pattern.search(text):
            # context_keywords があるルールは、boxまたは近傍にキーワードが必要
            if rule.context_keywords:
                if (
                    _has_any_keyword(text, rule.context_keywords)
                    or rule.category in extra_cats
                ):
                    matched.append(rule.category)
            else:
                matched.append(rule.category)

    # 近傍からカテゴリが伝播してきていれば、数字列があるboxは該当カテゴリ扱い
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

    unmaskable = False
    # strict mode: 漢字連続だけのbox（敬称なし）も人名候補としてマスク
    if strict and not matched:
        stripped = text.strip()
        if RE_KANJI_RUN.fullmatch(stripped) and 2 <= len(stripped) <= 5:
            matched.append(CAT_NAME_LIKE_KANJI)
            unmaskable = True  # 推測でのマスクなので unmaskable 疑いを上げる

    return list(dict.fromkeys(matched)), unmaskable


def _draw_black_rect(
    img: Image.Image, bbox: tuple[int, int, int, int]
) -> None:
    draw = ImageDraw.Draw(img)
    x1, y1, x2, y2 = bbox
    # 1px 余白を持たせる（OCR矩形がタイトすぎる場合の保険）
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
    # まず y1 でラフに分行（行高あたり半分まで同一行扱い）
    if not indexed:
        return []
    avg_h = sum(b.bbox[3] - b.bbox[1] for _, b in indexed) / len(indexed) or 1
    indexed.sort(key=lambda kv: (round(kv[1].bbox[1] / max(avg_h * 0.6, 1)), kv[1].bbox[0]))
    return [i for i, _ in indexed]


def mask_image(
    image: Image.Image,
    ocr_boxes: list[OCRBox],
    strict: bool = True,
    rules: list[MaskRule] | None = None,
) -> MaskResult:
    """OCR結果を元に画像へ黒塗りを適用し、マスク済みテキスト要約も返す."""
    if rules is None:
        rules = DEFAULT_RULES

    # PIL Image を編集可能な形にコピー
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
        cats, unmaskable = _classify_box(box, extra.get(i, set()), rules, strict)
        classifications.append((box, cats, unmaskable))
        if cats:
            _draw_black_rect(out_img, box.bbox)
            mask_count += 1
            all_categories.extend(cats)
            if unmaskable:
                unmaskable_overall = True

    # text_summary: 読み順で連結
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

    # マスクできなかった疑いのある未知パターン検出
    # strict modeで「漢字+数字混在」なのにどのルールにもマッチしなかった場合等
    if strict and not unmaskable_overall:
        for box, cats, _ in classifications:
            if cats:
                continue
            t = box.text
            if (
                re.search(r"[一-鿿]", t)
                and re.search(r"\d{4,}", t)
                and not _is_drug_name_only(t)
            ):
                unmaskable_overall = True
                break

    # 重複除去（順序保持）
    deduped_cats = list(dict.fromkeys(all_categories))

    return MaskResult(
        masked_image=out_img,
        text_summary=text_summary,
        mask_count=mask_count,
        mask_categories=deduped_cats,
        unmaskable=unmaskable_overall,
    )
