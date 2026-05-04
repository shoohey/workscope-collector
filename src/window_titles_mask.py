"""ウィンドウタイトル軽量マスキング（OCR不要・正規表現のみ）.

masker.py の DEFAULT_RULES を流用し、タイトル文字列にマッチした個人情報を
[MASKED:<category>] に置換する。元タイトルは保存しないため SHA256 短縮ハッシュ
を返して同一性のみ追跡可能にする。
"""

from __future__ import annotations

import hashlib
import re

try:
    from .masker import (
        DEFAULT_RULES,
        CAT_NAME_LIKE_KANJI,
        MaskRule,
        RE_KANJI_RUN,
    )
except ImportError:  # pragma: no cover
    from masker import (  # type: ignore[no-redef]
        DEFAULT_RULES,
        CAT_NAME_LIKE_KANJI,
        MaskRule,
        RE_KANJI_RUN,
    )


def _hash_short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _apply_rule(text: str, rule: MaskRule) -> tuple[str, int]:
    """ルールを適用し (置換後文字列, ヒット数) を返す."""
    label = f"[MASKED:{rule.category}]"
    new_text, n = rule.pattern.subn(label, text)
    return new_text, n


def mask_window_title(title: str) -> tuple[str, str, list[str]]:
    """タイトルをマスクし (masked_title, hash_short, categories) を返す.

    - masked_title: マッチ箇所を [MASKED:<category>] に置換した文字列
    - hash_short: 元タイトルの SHA256 先頭 8 文字（小文字hex）
    - categories: マスクされたカテゴリ一覧（順序保持・重複なし）
    """
    if not title:
        return "", "", []

    masked = title
    hit_categories: list[str] = []

    # context_keywords を持つルールは、タイトル文字列内にキーワードが含まれる時のみ発火
    for rule in DEFAULT_RULES:
        if rule.context_keywords:
            if not any(kw in masked for kw in rule.context_keywords):
                # キーワードがない場合は安全側でスキップ（軽量版なので近傍判定不可）
                continue
        new_text, n = _apply_rule(masked, rule)
        if n > 0:
            hit_categories.extend([rule.category] * n)
            masked = new_text

    # 漢字2-5文字の連続（敬称なし氏名候補）も保険でマスク
    # 既に [MASKED:...] 化された箇所は触らないように先にプレースホルダ退避
    placeholder_pattern = re.compile(r"\[MASKED:[a-z_]+\]")
    placeholders: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        placeholders.append(m.group(0))
        return f"\x00PH{len(placeholders) - 1}\x00"

    stashed = placeholder_pattern.sub(_stash, masked)

    def _replace_kanji(m: re.Match[str]) -> str:
        hit_categories.append(CAT_NAME_LIKE_KANJI)
        return f"[MASKED:{CAT_NAME_LIKE_KANJI}]"

    stashed = RE_KANJI_RUN.sub(_replace_kanji, stashed)

    def _restore(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return placeholders[idx]

    masked = re.sub(r"\x00PH(\d+)\x00", _restore, stashed)

    deduped = list(dict.fromkeys(hit_categories))
    return masked, _hash_short(title), deduped
