"""app_classifier のテスト. process/window から正しいカテゴリ・RPA出口が返る事."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app_classifier import (  # noqa: E402
    CATEGORY_BROWSER,
    CATEGORY_DEV,
    CATEGORY_ERP,
    CATEGORY_INDUSTRY_MEDICAL,
    CATEGORY_OFFICE,
    CATEGORY_OTHER,
    CATEGORY_SAAS_DESKTOP,
    CATEGORY_SAAS_WEB,
    RPA_COMPUTER_USE,
    RPA_NONE,
    RPA_PAD,
    RPA_PYWINAUTO,
    RPA_SELENIUM,
    classify,
    clear_cache,
    get_rpa_target_for_category,
    list_categories,
)


@pytest.fixture(autouse=True)
def _reset():
    clear_cache()
    yield
    clear_cache()


# --- 1. 業界アプリ-医療（最優先） -------------------------------------------

@pytest.mark.parametrize("title", ["レセプト入力 - 患者一覧", "電子カルテ ABC薬局", "処方せん入力 v3.2", "調剤録"])
def test_classify_medical_industry_apps(title: str) -> None:
    result = classify(process_name="ReceptyNEXT.exe", window_title=title)
    assert result.category == CATEGORY_INDUSTRY_MEDICAL
    assert result.rpa_target == RPA_PYWINAUTO


def test_classify_medical_by_process() -> None:
    result = classify(process_name="WeMex.exe", window_title="メイン画面")
    assert result.category == CATEGORY_INDUSTRY_MEDICAL


# --- 2. ERP -----------------------------------------------------------------

def test_classify_sap() -> None:
    result = classify(process_name="saplogon.exe", window_title="SAP GUI for Windows")
    assert result.category == CATEGORY_ERP
    assert result.rpa_target == RPA_PYWINAUTO


def test_classify_yayoi() -> None:
    result = classify(process_name="yayoi.exe", window_title="弥生販売 - 入金処理")
    assert result.category == CATEGORY_ERP


# --- 3. SaaS-Desktop --------------------------------------------------------

@pytest.mark.parametrize("proc", ["Teams.exe", "ms-teams.exe", "Slack.exe", "Zoom.exe", "Discord.exe"])
def test_classify_saas_desktop(proc: str) -> None:
    result = classify(process_name=proc, window_title="チャット")
    assert result.category == CATEGORY_SAAS_DESKTOP
    assert result.rpa_target == RPA_PAD


# --- 4. SaaS-Web (browser + URL) -------------------------------------------

@pytest.mark.parametrize("title,expected_url", [
    ("商談一覧 - kintone.cybozu.com", "kintone.cybozu.com"),
    ("Lead detail | lightning.force.com", "lightning.force.com"),
    ("My Workspace - notion.so", "notion.so"),
    ("Project Board | atlassian.net Jira", "atlassian.net"),
])
def test_classify_saas_web_via_browser(title: str, expected_url: str) -> None:
    """Chrome/Edge プロセスでも window_title に SaaS の URL があれば SaaS-Web."""
    result = classify(process_name="chrome.exe", window_title=title)
    assert result.category == CATEGORY_SAAS_WEB
    assert result.rpa_target == RPA_SELENIUM
    assert expected_url in result.matched_rule


def test_classify_freee_in_browser_routed_to_industry_accounting() -> None:
    """freee は業界アプリ-会計に優先される（業務分析でアカウンティング業務として扱う）.
    SaaS-Web より優先度が高いのは仕様。"""
    result = classify(process_name="chrome.exe", window_title="freee会計 - 仕訳入力")
    assert result.category == "industry_accounting"
    assert result.rpa_target == RPA_PAD


# --- 5. Office --------------------------------------------------------------

@pytest.mark.parametrize("proc", ["EXCEL.EXE", "WINWORD.EXE", "POWERPNT.EXE", "OUTLOOK.EXE", "Acrobat.exe"])
def test_classify_office(proc: str) -> None:
    result = classify(process_name=proc, window_title="Document.xlsx")
    assert result.category == CATEGORY_OFFICE
    assert result.rpa_target == RPA_PAD


# --- 6. Browser (URL マッチなしフォールバック) ------------------------------

def test_classify_browser_fallback() -> None:
    """Chrome で SaaS URL が無い → Browser カテゴリにフォールバック."""
    result = classify(process_name="chrome.exe", window_title="Google 検索 - Google Chrome")
    assert result.category == CATEGORY_BROWSER
    assert result.rpa_target == RPA_SELENIUM


# --- 7. 開発ツール ----------------------------------------------------------

@pytest.mark.parametrize("proc", ["Code.exe", "idea64.exe", "pycharm64.exe", "WindowsTerminal.exe", "Cursor.exe"])
def test_classify_dev_tools(proc: str) -> None:
    result = classify(process_name=proc, window_title="main.py - VSCode")
    assert result.category == CATEGORY_DEV
    assert result.rpa_target == RPA_NONE


# --- 8. その他フォールバック ------------------------------------------------

def test_classify_unknown_app_returns_other() -> None:
    result = classify(process_name="UnknownApp_xyz.exe", window_title="Unknown")
    assert result.category == CATEGORY_OTHER
    assert result.rpa_target == RPA_COMPUTER_USE


def test_classify_empty_input() -> None:
    """空入力でも例外を起こさず other を返す."""
    result = classify(process_name="", window_title="")
    assert result.category == CATEGORY_OTHER


def test_classify_none_input() -> None:
    """None でも例外なく other を返す."""
    result = classify(process_name=None, window_title=None)
    assert result.category == CATEGORY_OTHER


# --- 9. 優先度: 業界アプリ > ERP > Office --------------------------------

def test_priority_industry_medical_beats_browser() -> None:
    """同時にマッチする時、業界医療アプリが優先される (chrome.exe + レセプトタイトル)."""
    # chrome.exe + 「レセプト」を含むタイトル → industry_medical 優先
    result = classify(process_name="chrome.exe", window_title="レセプト管理画面")
    assert result.category == CATEGORY_INDUSTRY_MEDICAL


# --- 10. ヘルパAPI ---------------------------------------------------------

def test_list_categories_includes_all() -> None:
    cats = list_categories()
    expected = {"saas_web", "saas_desktop", "erp", "industry_medical",
                "industry_accounting", "office", "browser", "dev"}
    missing = expected - set(cats)
    assert not missing, f"missing categories in app_rules.json: {missing}"


def test_get_rpa_target_for_category() -> None:
    assert get_rpa_target_for_category(CATEGORY_INDUSTRY_MEDICAL) == RPA_PYWINAUTO
    assert get_rpa_target_for_category(CATEGORY_OFFICE) == RPA_PAD
    assert get_rpa_target_for_category(CATEGORY_SAAS_WEB) == RPA_SELENIUM
    assert get_rpa_target_for_category("nonexistent") == RPA_COMPUTER_USE


# --- 11. matched_rule デバッグ情報 -----------------------------------------

def test_matched_rule_includes_type_and_value() -> None:
    result = classify(process_name="EXCEL.EXE", window_title="売上.xlsx")
    assert "process_name" in result.matched_rule
    assert "EXCEL.EXE" in result.matched_rule


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
