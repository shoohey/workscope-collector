"""ウィンドウタイトル専用の軽量マスキング.

OCR を使わず正規表現のみで個人情報候補を黒塗りする。
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, Pattern

# --- マスキング対象パターン ---
# 漢字 3-4 文字 + 敬称（様/さん/殿/氏）
_PERSON_NAME = re.compile(
    r"[一-鿿]{2,4}\s?(?:様|さん|殿|氏)"
)

# カナ 3-6 文字 + 敬称
_PERSON_NAME_KANA = re.compile(
    r"[゠-ヿ぀-ゟ]{3,8}\s?(?:様|さん|殿|氏)"
)

# 数字 8 桁以上の連番（保険証番号・患者ID候補）
_LONG_DIGITS = re.compile(r"\d{8,}")

# 生年月日: 1900-2099 / YYYY-MM-DD, YYYY/MM/DD, YYYY年MM月DD日
_DATE_BIRTH = re.compile(
    r"(?:19|20)\d{2}[\-/年]\s?(?:0?[1-9]|1[0-2])[\-/月]\s?(?:0?[1-9]|[12]\d|3[01])日?"
)

# 和暦: 昭和/平成/令和 + 数字 + 年 + 数字 + 月 + 数字 + 日
_DATE_WAREKI = re.compile(
    r"(?:昭和|平成|令和)\s?\d{1,2}\s?年\s?\d{1,2}\s?月\s?\d{1,2}\s?日"
)

# 電話番号 (0X-XXXX-XXXX, 0XX-XXX-XXXX 等)
_PHONE = re.compile(r"0\d{1,4}-\d{1,4}-\d{3,4}")

# メールアドレス
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# マイナンバー（12桁、ハイフンあり/なし）
_MY_NUMBER = re.compile(r"\d{4}-?\d{4}-?\d{4}")

_PATTERNS: tuple[Pattern[str], ...] = (
    _PERSON_NAME,
    _PERSON_NAME_KANA,
    _DATE_BIRTH,
    _DATE_WAREKI,
    _MY_NUMBER,
    _LONG_DIGITS,
    _PHONE,
    _EMAIL,
)


def _hash_short(text: str) -> str:
    """SHA256 の先頭 16 文字を返す."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def mask_window_title(title: str) -> tuple[str, str]:
    """ウィンドウタイトルをマスキングして (masked, hash) を返す.

    マッチした箇所は ``[MASKED]`` に置換される。
    元タイトルは保存しないため、ハッシュ短縮値で同一性のみ追跡する。
    """
    if not title:
        return "", ""
    masked = title
    for pat in _PATTERNS:
        masked = pat.sub("[MASKED]", masked)
    return masked, _hash_short(title)


def is_blocklisted(
    title: str,
    process_name: str,
    blocklist_processes: Iterable[str],
    blocklist_title_substrings: Iterable[str],
) -> bool:
    """ブロックリストに引っかかれば True を返す（部分一致・大小無視）."""
    title_lc = (title or "").lower()
    proc_lc = (process_name or "").lower()
    for proc in blocklist_processes:
        if proc and proc.lower() in proc_lc:
            return True
    for sub in blocklist_title_substrings:
        if sub and sub.lower() in title_lc:
            return True
    return False
