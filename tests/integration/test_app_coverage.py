"""主要SaaS/業界アプリ20種のカバレッジテスト.

Mac開発環境ではUI Automation実機検証は不可なので、ここではルールDB側の
カバレッジを保証する。実機検証は Windows clean VM CI で別workflow化する。

カバレッジ:
- アプリ分類が想定通り判定される (process_name/window_titleの代表例)
- RPA出口が想定通り振り分けられる
- 全主要アプリで業務分析対象になる (other 落ちしない)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from app_classifier import classify, clear_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    clear_cache()
    yield
    clear_cache()


# ============================================================================
# 主要アプリ20種カバレッジ
# ============================================================================

# (アプリ識別ヒント, process_name, window_title, 期待カテゴリ, 期待RPA出口)
APP_COVERAGE = [
    # === 業界アプリ-医療 (8メーカー) ===
    ("ウィーメックス",       "WeMex.exe",         "メイン画面",                "industry_medical", "pywinauto"),
    ("シグマソリューションズ", "Sigma.exe",         "エリシアS - 受付",          "industry_medical", "pywinauto"),
    ("EMシステムズ",         "EM_System.exe",      "ReceptyNEXT v3",          "industry_medical", "pywinauto"),
    ("ハイブリッジ",         "Hibridge.exe",       "ヒストリープラス",          "industry_medical", "pywinauto"),
    ("モイネット",           "Moinet.exe",         "薬局管理",                  "industry_medical", "pywinauto"),
    ("ユニケ",               "Unike.exe",         "受付業務",                  "industry_medical", "pywinauto"),
    ("電子カルテ汎用",       "Karte.exe",         "電子カルテ - 患者一覧",     "industry_medical", "pywinauto"),
    ("レセプト汎用",         "Receipt.exe",       "レセプト管理",              "industry_medical", "pywinauto"),

    # === 業界アプリ-会計 ===
    ("freee (Web)",          "chrome.exe",         "freee会計 - 仕訳",          "industry_accounting", "pad"),
    ("マネーフォワード",     "chrome.exe",         "マネーフォワード - 経費",   "industry_accounting", "pad"),
    ("弥生会計",             "yayoi_kaikei.exe",   "弥生会計 - 仕訳",           "industry_accounting", "pad"),

    # === ERP ===
    ("SAP GUI",              "saplogon.exe",       "SAP GUI for Windows",       "erp", "pywinauto"),
    ("勘定奉行",             "kanjo.exe",         "勘定奉行 - 仕訳入力",       "industry_accounting", "pad"),

    # === SaaS-Desktop ===
    ("Slack",                "Slack.exe",          "チャンネル",                "saas_desktop", "pad"),
    ("Microsoft Teams",      "Teams.exe",          "チーム会議",                "saas_desktop", "pad"),
    ("Zoom",                 "Zoom.exe",           "ミーティング",              "saas_desktop", "pad"),

    # === SaaS-Web (browser経由) ===
    ("kintone",              "chrome.exe",         "案件 - kintone.cybozu.com", "saas_web", "selenium"),
    ("Salesforce",           "chrome.exe",         "Lead | lightning.force.com", "saas_web", "selenium"),
    ("Notion",               "chrome.exe",         "Workspace - notion.so",     "saas_web", "selenium"),
    ("Asana",                "chrome.exe",         "Project - .asana.com",      "saas_web", "selenium"),

    # === Office ===
    ("Excel",                "EXCEL.EXE",          "売上.xlsx",                  "office", "pad"),
    ("Word",                 "WINWORD.EXE",        "提案書.docx",                "office", "pad"),
    ("Outlook",              "OUTLOOK.EXE",        "受信トレイ",                 "office", "pad"),
    ("PowerPoint",           "POWERPNT.EXE",       "プレゼン.pptx",              "office", "pad"),
    ("PDF (Acrobat)",        "Acrobat.exe",        "契約書.pdf",                 "office", "pad"),

    # === Browser (URL不明) ===
    ("Chrome (汎用)",        "chrome.exe",         "Google検索",                 "browser", "selenium"),

    # === 開発ツール ===
    ("VSCode",               "Code.exe",           "main.py - VSCode",          "dev", "none"),
    ("PyCharm",              "pycharm64.exe",      "Python Project",            "dev", "none"),
    ("Windows Terminal",     "WindowsTerminal.exe", "PowerShell",                "dev", "none"),
]


@pytest.mark.parametrize("hint,proc,title,expected_cat,expected_rpa", APP_COVERAGE,
                          ids=[c[0] for c in APP_COVERAGE])
def test_app_coverage(hint: str, proc: str, title: str,
                      expected_cat: str, expected_rpa: str) -> None:
    """各主要アプリで category と rpa_target が想定通り."""
    result = classify(process_name=proc, window_title=title)
    assert result.category == expected_cat, \
        f"{hint}: expected category={expected_cat}, got {result.category} (matched: {result.matched_rule})"
    assert result.rpa_target == expected_rpa, \
        f"{hint}: expected rpa_target={expected_rpa}, got {result.rpa_target}"


# ============================================================================
# 全カテゴリの存在確認
# ============================================================================

def test_all_expected_categories_covered() -> None:
    """期待される全カテゴリで少なくとも1アプリがマッチする."""
    expected = {"industry_medical", "industry_accounting", "erp",
                "saas_desktop", "saas_web", "office", "browser", "dev"}
    matched_categories: set[str] = set()
    for _, proc, title, _, _ in APP_COVERAGE:
        result = classify(process_name=proc, window_title=title)
        matched_categories.add(result.category)
    missing = expected - matched_categories
    assert not missing, f"カバレッジ不足カテゴリ: {missing}"


# ============================================================================
# RPA出口の網羅
# ============================================================================

def test_all_rpa_targets_covered() -> None:
    """全RPA出口種別がいずれかのアプリで利用される."""
    expected = {"pywinauto", "pad", "selenium", "none"}
    matched: set[str] = set()
    for _, proc, title, _, _ in APP_COVERAGE:
        result = classify(process_name=proc, window_title=title)
        matched.add(result.rpa_target)
    missing = expected - matched
    assert not missing, f"使われない RPA 出口: {missing}"


# ============================================================================
# unknown app は other + computer_use にフォールバック
# ============================================================================

@pytest.mark.parametrize("proc,title", [
    ("UnknownApp_zzz.exe", "Unknown Window"),
    ("custom_internal_tool.exe", "Internal Dashboard"),
    ("legacy_system.exe", "Legacy v1.0"),
])
def test_unknown_apps_fall_to_computer_use(proc: str, title: str) -> None:
    result = classify(process_name=proc, window_title=title)
    assert result.category == "other"
    assert result.rpa_target == "computer_use"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
