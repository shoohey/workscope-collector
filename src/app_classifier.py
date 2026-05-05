"""アプリ自動分類モジュール.

process_name / process_path / window_title から AppCategory を判定する。
判定結果は (a) 業務フロー解析時のグルーピング基準、(b) RPA出口の自動振り分け
（pywinauto/PAD/Selenium/Computer Use）に使用される。

ルールDBは src/app_rules.json で外部化。コミュニティ拡張可能。

設計方針:
- 判定優先度: 業界アプリ(医療/会計) > ERP > SaaS-Desktop > SaaS-Web(URL判定) > Office > Browser(URL不明) > Dev > Other
- SaaS-Web は process が browser でも window_title にドメインが含まれていれば優先判定
- 判定不可は "other" で返し、Computer Use エージェントの対象とする
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---- カテゴリ定数 -----------------------------------------------------------
CATEGORY_SAAS_WEB = "saas_web"
CATEGORY_SAAS_DESKTOP = "saas_desktop"
CATEGORY_ERP = "erp"
CATEGORY_INDUSTRY_MEDICAL = "industry_medical"
CATEGORY_INDUSTRY_ACCOUNTING = "industry_accounting"
CATEGORY_OFFICE = "office"
CATEGORY_BROWSER = "browser"
CATEGORY_DEV = "dev"
CATEGORY_OTHER = "other"


# ---- RPA出口種別 -----------------------------------------------------------
RPA_PYWINAUTO = "pywinauto"
RPA_PAD = "pad"
RPA_SELENIUM = "selenium"
RPA_COMPUTER_USE = "computer_use"
RPA_NONE = "none"


# 判定優先度（小さいほど優先）
_PRIORITY = {
    CATEGORY_INDUSTRY_MEDICAL: 10,
    CATEGORY_INDUSTRY_ACCOUNTING: 11,
    CATEGORY_ERP: 20,
    CATEGORY_SAAS_DESKTOP: 30,
    CATEGORY_SAAS_WEB: 40,  # browser+URLマッチで優先（browser単独より上）
    CATEGORY_OFFICE: 50,
    CATEGORY_BROWSER: 60,
    CATEGORY_DEV: 70,
    CATEGORY_OTHER: 99,
}


@dataclass(frozen=True)
class AppClassification:
    """アプリ分類結果."""

    category: str
    label: str
    rpa_target: str
    matched_rule: str = ""  # デバッグ用


# ---- ルールDB読込 -----------------------------------------------------------

def _candidate_rule_paths() -> list[Path]:
    """app_rules.json の探索先."""
    out: list[Path] = []
    env = os.environ.get("WORKSCOPE_APP_RULES_PATH")
    if env:
        out.append(Path(env))
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            out.append(Path(meipass) / "app_rules.json")
    here = Path(__file__).resolve().parent
    out.append(here / "app_rules.json")
    out.append(here.parent / "src" / "app_rules.json")
    return out


_rules_cache: dict[str, Any] | None = None


def _load_rules() -> dict[str, Any]:
    """app_rules.json を1回だけ読み込んでキャッシュ."""
    global _rules_cache
    if _rules_cache is not None:
        return _rules_cache
    for p in _candidate_rule_paths():
        if p.exists():
            try:
                _rules_cache = json.loads(p.read_text(encoding="utf-8"))
                logger.info("loaded app_rules from %s (%d categories)",
                            p, len(_rules_cache.get("categories", {})))
                return _rules_cache
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("failed to load %s: %s", p, e)
                continue
    logger.warning("app_rules.json not found in %s; using empty rules",
                   [str(p) for p in _candidate_rule_paths()])
    _rules_cache = {"version": "0", "categories": {}, "default_category": "other",
                    "default_rpa_target": "computer_use"}
    return _rules_cache


def clear_cache() -> None:
    """テスト用: ルールキャッシュをリセット."""
    global _rules_cache
    _rules_cache = None


# ---- 個別マッチャー --------------------------------------------------------

def _match_rule(rule: dict[str, Any], process_name: str, process_path: str,
                window_title: str) -> bool:
    """1ルール定義 vs 入力 をマッチ."""
    rtype = rule.get("type", "")
    val = rule.get("value", "")
    if not val:
        return False

    if rtype == "process_name":
        return (process_name or "").lower() == val.lower()
    if rtype == "process_name_substring":
        return val.lower() in (process_name or "").lower() or val.lower() in (process_path or "").lower()
    if rtype == "process_path_substring":
        return val.lower() in (process_path or "").lower()
    if rtype == "title_substring":
        return val.lower() in (window_title or "").lower()
    if rtype == "title_exact":
        return (window_title or "") == val
    return False


# ---- 公開API ---------------------------------------------------------------

def classify(process_name: str | None, process_path: str | None = None,
             window_title: str | None = None) -> AppClassification:
    """アプリを分類して AppClassification を返す.

    優先度順にカテゴリを評価し、最初にマッチしたものを採用。
    マッチなしは default ("other" + computer_use)。
    """
    process_name = process_name or ""
    process_path = process_path or ""
    window_title = window_title or ""

    rules_db = _load_rules()
    categories = rules_db.get("categories", {})

    # カテゴリを優先度順に評価
    sorted_cats = sorted(categories.items(),
                         key=lambda kv: _PRIORITY.get(kv[0], 99))

    for cat_key, cat_def in sorted_cats:
        for rule in cat_def.get("rules", []):
            if _match_rule(rule, process_name, process_path, window_title):
                return AppClassification(
                    category=cat_key,
                    label=cat_def.get("label", cat_key),
                    rpa_target=cat_def.get("rpa_target", RPA_COMPUTER_USE),
                    matched_rule=f"{rule.get('type')}={rule.get('value')}",
                )

    return AppClassification(
        category=rules_db.get("default_category", CATEGORY_OTHER),
        label="その他",
        rpa_target=rules_db.get("default_rpa_target", RPA_COMPUTER_USE),
        matched_rule="default",
    )


def list_categories() -> list[str]:
    """登録済みカテゴリ一覧."""
    return list(_load_rules().get("categories", {}).keys())


def get_rpa_target_for_category(category: str) -> str:
    """カテゴリ名からRPA出口を引く（未登録は computer_use）."""
    rules_db = _load_rules()
    cat_def = rules_db.get("categories", {}).get(category, {})
    return cat_def.get("rpa_target", rules_db.get("default_rpa_target", RPA_COMPUTER_USE))


__all__ = [
    "AppClassification",
    "classify",
    "clear_cache",
    "list_categories",
    "get_rpa_target_for_category",
    # カテゴリ定数
    "CATEGORY_SAAS_WEB",
    "CATEGORY_SAAS_DESKTOP",
    "CATEGORY_ERP",
    "CATEGORY_INDUSTRY_MEDICAL",
    "CATEGORY_INDUSTRY_ACCOUNTING",
    "CATEGORY_OFFICE",
    "CATEGORY_BROWSER",
    "CATEGORY_DEV",
    "CATEGORY_OTHER",
    # RPA出口定数
    "RPA_PYWINAUTO",
    "RPA_PAD",
    "RPA_SELENIUM",
    "RPA_COMPUTER_USE",
    "RPA_NONE",
]
