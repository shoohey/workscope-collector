"""ウィンドウタイトルの軽量マスキング（OCR不要・正規表現のみ）.

設計原則:

1. **masker.py への依存を最小化**: numpy/PIL に依存する masker 本体が import
   できない環境（部分インストール / 依存破損）でも window_titles は単独で
   動作するよう、必要な正規表現とカテゴリ定数を本ファイル内で定義する。
   masker が使える場合は DEFAULT_RULES からの追加ルール反復を行う（人名 等）。
2. **コンテキストキーワード判定は元タイトルに対して行う**。先行ルールが
   タイトル中の文脈ワード（例: 「マイナンバー」）をマスクしても、後続の
   number 系ルールが context を見失わないようにする。
3. **メールアドレスは context 不要で常時マスク**。
4. **保険のため、context が無くても 12 桁 / 8 桁 / 4桁以上 の数字列・電話・
   日付・住所・郵便はとにかくマスクする**。タイトルは短く判断材料が少ない
   ため、誤マスクが増えても情報漏えいを優先回避する。
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable


# ---- カテゴリ定数（masker と重複定義。masker が import できない環境でも動作させるため） ----
CAT_NAME_LIKE_KANJI = "name_like_kanji"
CAT_EMAIL = "email"
CAT_MY_NUMBER = "my_number"
CAT_INSURANCE_CARD = "insurance_card_no"
CAT_INSURANCE_ID = "insurance_id"
CAT_PATIENT_ID = "patient_id"
CAT_PHONE = "phone"
CAT_BIRTHDATE = "birthdate"
CAT_POSTAL = "postal_code"
CAT_ADDRESS = "address"


# ---- 正規表現（独立定義） -----------------------------------------------------
# 漢字 - 数字の境界は Python の `\b` で認識されないので、数字非隣接判定には
# lookbehind/lookahead を使う。
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
RE_MY_NUMBER = re.compile(r"(?<!\d)\d{4}[-\s]?\d{4}[-\s]?\d{4}(?!\d)")
RE_INSURANCE_CARD = re.compile(r"\d{6,10}[-－]\d{1,4}")
RE_PHONE = re.compile(r"0\d{1,4}[-(]?\d{1,4}[-)]?\d{3,4}")
RE_DATE_SLASH = re.compile(
    r"(?:19|20)\d{2}[\-/.]\s?(?:0?[1-9]|1[0-2])[\-/.]\s?(?:0?[1-9]|[12]\d|3[01])"
)
RE_DATE_KANJI = re.compile(
    r"(?:19|20)\d{2}\s?年\s?(?:0?[1-9]|1[0-2])\s?月\s?(?:0?[1-9]|[12]\d|3[01])\s?日"
)
RE_DATE_WAREKI = re.compile(
    r"(?:明治|大正|昭和|平成|令和)\s?\d{1,2}\s?年\s?\d{1,2}\s?月\s?\d{1,2}\s?日"
)
RE_POSTAL = re.compile(r"(?<!\d)〒?\s?\d{3}-?\d{4}(?!\d)")
RE_DIGITS_8 = re.compile(r"(?<!\d)\d{8}(?!\d)")
RE_DIGITS_RUN = re.compile(r"\d{4,}")
RE_ADDRESS = re.compile(
    r"(?:北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|"
    r"埼玉県|千葉県|東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|"
    r"岐阜県|静岡県|愛知県|三重県|滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|"
    r"鳥取県|島根県|岡山県|広島県|山口県|徳島県|香川県|愛媛県|高知県|福岡県|"
    r"佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県)"
    r"[一-鿿ぁ-ゟ゠-ヿ\w]{1,30}?\d+(?:-\d+){0,3}"
)
RE_KANJI_RUN = re.compile(r"[一-鿿々]{2,5}")


# ---- masker 連携（オプショナル） ---------------------------------------------
# masker (PIL/numpy 必要) が読めれば DEFAULT_RULES から人名系も追加で反復する。
# 読めなくても本ファイルだけで主要 PII はカバーする。
try:
    from masker import DEFAULT_RULES as _MASKER_DEFAULT_RULES  # type: ignore[import]
    _HAS_MASKER = True
except Exception:  # pragma: no cover - 部分インストール時の保険
    _MASKER_DEFAULT_RULES = []  # type: ignore[assignment]
    _HAS_MASKER = False


# ---- フォールバックルール群（context 不要で必ず発火） ------------------------
# 順序が重要: 区切り記号 / 境界が明確な高信頼度パターンを先に消費させ、
# 最後に貪欲な digits_run でフォールバックする。
_FALLBACK_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # 1. メール（最も特定的）
    (RE_EMAIL, CAT_EMAIL),
    # 2. 12桁マイナンバー（数字非隣接境界つき）
    (RE_MY_NUMBER, CAT_MY_NUMBER),
    # 3. 保険証記号番号（ハイフン必須）
    (RE_INSURANCE_CARD, CAT_INSURANCE_CARD),
    # 4. 電話番号（区切り記号必須）
    (RE_PHONE, CAT_PHONE),
    # 5. 生年月日各形式
    (RE_DATE_WAREKI, CAT_BIRTHDATE),
    (RE_DATE_KANJI, CAT_BIRTHDATE),
    (RE_DATE_SLASH, CAT_BIRTHDATE),
    # 6. 〒+7桁（数字非隣接境界つき。9桁IDの先頭7桁を食わない）
    (RE_POSTAL, CAT_POSTAL),
    # 7. 8桁ぴったり（数字非隣接境界つき。9桁IDで部分マッチしない、
    #    insurance_id 分類のため digits_run より先）
    (RE_DIGITS_8, CAT_INSURANCE_ID),
    # 8. 4桁以上の連続数字（最後のフォールバック）
    (RE_DIGITS_RUN, CAT_PATIENT_ID),
    # 9. 住所
    (RE_ADDRESS, CAT_ADDRESS),
)


def _hash_short(text: str) -> str:
    """SHA256 の先頭 16 文字を返す（タイトル同一性の追跡用）."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _apply_pattern(text: str, pattern: re.Pattern[str], category: str) -> tuple[str, int]:
    """正規表現を 1 回適用し ``(置換後文字列, ヒット数)`` を返す."""
    label = f"[MASKED:{category}]"
    new_text, n = pattern.subn(label, text)
    return new_text, n


def mask_window_title(title: str) -> tuple[str, str, list[str]]:
    """タイトルをマスキングして ``(masked, hash_short, categories)`` を返す.

    実装方針:
      Phase 1: 形式マッチ ( context 不要 ) でメアド・電話・郵便・住所・
               日付・各種数字列を確実にマスク。
      Phase 2: masker.DEFAULT_RULES から context-free ルール（人名 等）を反復。
               masker 不在時はスキップ。
      Phase 3: 漢字 2-5 文字連続を name_like_kanji としてマスク（保険）。
    """
    if not title:
        return "", "", []

    masked = title
    hit_categories: list[str] = []

    # ---- Phase 1: 高信頼度の形式マッチ -------
    for pat, cat in _FALLBACK_RULES:
        new_text, n = _apply_pattern(masked, pat, cat)
        if n > 0:
            hit_categories.extend([cat] * n)
            masked = new_text

    # ---- Phase 2: masker DEFAULT_RULES から人名等の context-free ルール ----
    if _HAS_MASKER:
        for rule in _MASKER_DEFAULT_RULES:
            if getattr(rule, "context_keywords", ()):
                # context 必須ルールは Phase 1 のフォールバックでカバー済み
                continue
            new_text, n = _apply_pattern(masked, rule.pattern, rule.category)
            if n > 0:
                hit_categories.extend([rule.category] * n)
                masked = new_text

    # ---- Phase 3: 業務語ホワイトリスト + 氏名候補マスク (Codex Critical#1 対応, 2026-05-06) ----
    # v0.1.0で全漢字連続マスクしていた→v1.0で全許容に倒した→Codex指摘で
    # 「敬称なし患者名 (山田太郎 など) が漏れる」と判明したので、業務語ホワイトリストで
    # 残すべき業務名を保護しつつ、それ以外の漢字2-5文字連続をマスクする折衷案に。
    masked = _apply_phase3_kanji_mask(masked, hit_categories)

    deduped = list(dict.fromkeys(hit_categories))
    return masked, _hash_short(title), deduped


# ---- Phase 3 ヘルパ -----------------------------------------------------

# 業務分析を成立させるため残しておくべき汎用業務語（漢字2-5文字）
# 業界別の業務語は profile_loader 経由で base.json / pharmacy.json の
# whitelist["business_terms"] / whitelist["dental_terms"] 等から読み込まれる
_BUILTIN_BUSINESS_TERMS: frozenset[str] = frozenset({
    # === 汎用UI/操作語 ===
    "受付", "登録", "編集", "削除", "保存", "確認", "送信", "印刷",
    "検索", "詳細", "一覧", "新規", "更新", "変更", "修正", "閉じる",
    "戻る", "次へ", "戻す", "追加", "選択", "決定", "取消", "中止",
    "ログイン", "ログアウト", "設定", "管理", "操作", "表示", "入力",
    "出力", "ホーム", "ファイル", "ヘルプ", "ツール", "メニュー",
    "画面", "設定画面", "管理画面", "ホーム画面",
    # === 業務シーン共通 ===
    "業務", "処理", "申請", "承認", "発行", "発注", "受注", "請求",
    "支払", "入金", "出金", "案件", "計画", "予定", "実績", "報告",
    # === 薬局/医療系 ===
    "患者", "処方", "調剤", "薬歴", "服薬", "投薬", "交付", "監査",
    "計算", "明細", "領収", "保険", "受診", "診察", "予約", "来局",
    "メイン", "履歴", "記録", "レセプト", "電子", "カルテ",
    # === 会計/法律/HR系 ===
    "仕訳", "取引", "勘定", "決算", "決算書", "事件", "依頼",
    "従業員", "人事", "給与", "勤怠", "休暇", "評価",
    # === 不動産/建設/製造 ===
    "物件", "賃貸", "売買", "現場", "工事", "施工", "見積", "発注",
    "工程", "在庫", "出荷", "検査", "図面", "品質", "仕入",
})


def _phase3_load_extra_terms() -> frozenset[str]:
    """profile_loader 経由でデフォルトプロファイルの whitelist から業務語を取得.

    取得失敗時 (Mac開発で profile_loader が無いケース等) は空集合.
    """
    try:
        from profile_loader import load_profile, get_default_profile_name  # type: ignore
    except Exception:
        return frozenset()
    try:
        profile = load_profile(get_default_profile_name() or "pharmacy")
    except Exception:
        return frozenset()
    extras: set[str] = set()
    for key, val in (profile.whitelist or {}).items():
        if not isinstance(val, list):
            continue
        for item in val:
            if isinstance(item, str) and 1 < len(item) <= 6:
                extras.add(item)
    return frozenset(extras)


def _apply_phase3_kanji_mask(masked: str, hit_categories: list[str]) -> str:
    """業務語ホワイトリストを除外しつつ、敬称なし漢字氏名候補をマスク.

    - [MASKED:xxx] プレースホルダは保護
    - 業務語ホワイトリスト (組み込み + プロファイル whitelist) はマスクしない
    - 残った 漢字2-5文字 が氏名候補としてマスクされる
    """
    # placeholder を一時退避
    placeholder_pattern = re.compile(r"\[MASKED:[a-z_]+\]")
    placeholders: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        placeholders.append(m.group(0))
        return f"\x00PH{len(placeholders) - 1}\x00"

    stashed = placeholder_pattern.sub(_stash, masked)

    extra_terms = _phase3_load_extra_terms()
    business_terms = _BUILTIN_BUSINESS_TERMS | extra_terms

    def _is_business_term(text: str) -> bool:
        """業務語判定: 完全一致 or 業務語を部分含み AND 漢字3字以上.

        - "患者" → 完全一致 → 業務語
        - "患者検索" → "患者" / "検索" を含む + 4字 → 業務語
        - "山田太郎" → どの業務語も含まない → 氏名候補
        """
        if text in business_terms:
            return True
        if len(text) >= 3:
            for term in business_terms:
                if len(term) >= 2 and term in text:
                    return True
        return False

    def _replace_kanji(m: re.Match[str]) -> str:
        text = m.group(0)
        if _is_business_term(text):
            return text
        hit_categories.append(CAT_NAME_LIKE_KANJI)
        return f"[MASKED:{CAT_NAME_LIKE_KANJI}]"

    stashed = RE_KANJI_RUN.sub(_replace_kanji, stashed)

    def _restore(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return placeholders[idx]

    return re.sub(r"\x00PH(\d+)\x00", _restore, stashed)


def is_blocklisted(
    title: str,
    process_name: str,
    blocklist_processes: Iterable[str],
    blocklist_title_substrings: Iterable[str],
) -> bool:
    """ブロックリストに引っかかれば True（部分一致・大小無視）."""
    title_lc = (title or "").lower()
    proc_lc = (process_name or "").lower()
    for proc in blocklist_processes:
        if proc and proc.lower() in proc_lc:
            return True
    for sub in blocklist_title_substrings:
        if sub and sub.lower() in title_lc:
            return True
    return False


__all__ = ["mask_window_title", "is_blocklisted"]
