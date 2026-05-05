"""業界別E2Eテスト.

各業界の典型業務シナリオを「OCR結果 + 入力イベント列」でシミュレートし、
collector パイプラインを通して JSONL 出力を検証する。

シナリオ:
- pharmacy: 処方せん入力 (患者検索→処方→確認→送信)
- accounting: 仕訳入力 (取引先選択→金額入力→保存)
- legal: 案件登録 (依頼者選択→案件番号→保存)
- generic: メール作成 (宛先→本文→送信)

各シナリオで:
1. window_focus イベントが正しい app.category で記録される
2. 入力イベント列が schema v2 で記録される
3. PII（氏名・口座番号等）がマスクされている（業界プロファイル経由）
4. text_summary に生PIIが残っていない
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _isolate(tmp: Path, profile: str) -> None:
    os.environ["APPDATA"] = str(tmp)
    os.environ["WORKSCOPE_PROFILE"] = profile
    for m in ("storage", "collector", "config", "window_titles", "masker", "ocr",
              "profile_loader", "app_classifier", "uia_capture", "input_events"):
        sys.modules.pop(m, None)


class _StubOCR:
    def __init__(self, scenario_boxes: list[list]) -> None:
        # シナリオの各画面で返す OCR boxes を順にキューイング
        self._queue = list(scenario_boxes)
    def extract(self, _img):
        if not self._queue:
            return []
        return self._queue.pop(0)


def _white(w=800, h=600):
    return Image.new("RGB", (w, h), (255, 255, 255))


def _make_collector(scenario_boxes, **cfg):
    import collector as cm  # type: ignore
    import config as cfg_mod  # type: ignore
    base = {"min_dwell_seconds_for_capture": 0.0, "max_capture_per_minute": 200}
    base.update(cfg)
    return cm, cm.Collector(cfg=cfg_mod.CollectorConfig(**base),
                             ocr_engine=_StubOCR(scenario_boxes))


def _info(cm, hwnd, title, proc):
    return cm.WindowInfo(hwnd=hwnd, title=title, process_name=proc,
                         process_path=f"C:\\{proc}", pid=999,
                         rect=(0, 0, 1920, 1080), monitor=1)


def _read_events(appdata: Path) -> list[dict]:
    import json
    events_dir = appdata / "WorkScope" / "data" / "events"
    if not events_dir.exists():
        return []
    out = []
    for p in sorted(events_dir.glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _patch_capture(cm, monkeypatch):
    monkeypatch.setattr(cm, "capture_active", lambda _i=None: _white())


# ============================================================================
# 1. pharmacy: 処方せん入力業務
# ============================================================================

def test_pharmacy_prescription_entry_workflow(tmp_path, monkeypatch):
    """薬局: 処方せん入力業務を再現し、患者氏名・保険者番号がマスクされる事を検証."""
    _isolate(tmp_path, profile="pharmacy")
    from ocr import OCRBox  # type: ignore

    # シナリオ: 4画面遷移 (患者検索→患者詳細→処方入力→確認)
    scenario = [
        # 画面1: 患者検索
        [OCRBox(text="患者検索", bbox=(50, 30, 300, 70), confidence=0.95),
         OCRBox(text="鈴木太郎 様", bbox=(50, 100, 300, 140), confidence=0.95)],
        # 画面2: 患者詳細
        [OCRBox(text="保険者番号 12345678", bbox=(50, 100, 400, 140), confidence=0.95),
         OCRBox(text="生年月日 1985/03/15", bbox=(50, 150, 400, 190), confidence=0.95)],
        # 画面3: 処方入力
        [OCRBox(text="処方入力画面", bbox=(50, 30, 300, 70), confidence=0.95),
         OCRBox(text="アムロジピン 5mg", bbox=(50, 100, 400, 140), confidence=0.95)],
        # 画面4: 確認
        [OCRBox(text="確認", bbox=(50, 30, 200, 70), confidence=0.95)],
    ]
    cm, collector = _make_collector(scenario, drop_image_if_unmaskable=False)
    _patch_capture(cm, monkeypatch)

    # 4画面遷移を再現
    titles = ["患者検索", "患者詳細", "処方入力", "確認"]
    for i, title in enumerate(titles, start=1):
        ev = collector.process(_info(cm, hwnd=i, title=title, proc="ReceptyNEXT.exe"))
        assert ev is not None
        # 全イベント schema_version=2
        assert ev["schema_version"] == 2
        # アプリ分類: industry_medical
        assert ev["app"]["category"] == "industry_medical"
        assert ev["app"]["rpa_target"] == "pywinauto"

    events = _read_events(tmp_path)
    assert len(events) == 4

    # PII漏洩チェック: 全イベントの ocr_text_summary に生PIIが残らない
    for ev in events:
        ss = ev.get("screenshot")
        if ss is None:
            continue
        summary = ss.get("ocr_text_summary", "")
        assert "鈴木太郎" not in summary, f"PII LEAK in event {ev['event_seq']}: {summary}"
        assert "12345678" not in summary, f"insurance_id LEAK: {summary}"
        assert "1985/03/15" not in summary, f"birthdate LEAK: {summary}"

    # 業務シーケンス（4画面遷移）が正しく記録されている
    seq_titles = [ev["window"]["title"] for ev in events]
    assert seq_titles == titles or all(t in str(seq_titles) for t in titles)


# ============================================================================
# 2. accounting: 仕訳入力業務（freee想定）
# ============================================================================

def test_accounting_journal_entry_workflow(tmp_path, monkeypatch):
    """会計: 仕訳入力業務、取引先名・金額がマスクされる事を検証."""
    _isolate(tmp_path, profile="accounting")
    from ocr import OCRBox  # type: ignore

    scenario = [
        [OCRBox(text="取引先選択", bbox=(50, 30, 300, 70), confidence=0.95),
         OCRBox(text="株式会社サンプル商事", bbox=(50, 100, 400, 140), confidence=0.95)],
        [OCRBox(text="金額入力", bbox=(50, 30, 300, 70), confidence=0.95),
         OCRBox(text="売上 1,500,000円", bbox=(50, 100, 400, 140), confidence=0.95)],
        [OCRBox(text="振込先 普通 1234567", bbox=(50, 100, 400, 140), confidence=0.95)],
    ]
    cm, collector = _make_collector(scenario, drop_image_if_unmaskable=False)
    _patch_capture(cm, monkeypatch)

    # freee は industry_accounting に分類される（業界アプリ優先）
    titles = ["freee 取引先選択", "freee 仕訳入力", "freee 振込確認"]
    for i, title in enumerate(titles, start=1):
        ev = collector.process(_info(cm, hwnd=i, title=title, proc="chrome.exe"))
        assert ev is not None
        assert ev["app"]["category"] == "industry_accounting"

    events = _read_events(tmp_path)
    for ev in events:
        ss = ev.get("screenshot")
        if ss is None:
            continue
        summary = ss.get("ocr_text_summary", "")
        # 会計プロファイルで取引先名がマスクされる
        assert "株式会社サンプル商事" not in summary, f"client_name LEAK: {summary}"
        # 口座番号 (7桁) がマスクされる
        assert "1234567" not in summary, f"bank_account LEAK: {summary}"


# ============================================================================
# 3. legal: 案件登録業務
# ============================================================================

def test_legal_case_registration_workflow(tmp_path, monkeypatch):
    """法律: 案件登録、依頼者・事件番号がマスクされる事を検証."""
    _isolate(tmp_path, profile="legal")
    from ocr import OCRBox  # type: ignore

    scenario = [
        [OCRBox(text="案件登録", bbox=(50, 30, 300, 70), confidence=0.95),
         OCRBox(text="株式会社クライアント太郎", bbox=(50, 100, 400, 140), confidence=0.95)],
        [OCRBox(text="令和5年(ワ)第12345号", bbox=(50, 100, 400, 140), confidence=0.95),
         OCRBox(text="東京地方裁判所", bbox=(50, 150, 400, 190), confidence=0.95)],
    ]
    cm, collector = _make_collector(scenario, drop_image_if_unmaskable=False)
    _patch_capture(cm, monkeypatch)

    for i, title in enumerate(["案件登録 - LegalSuite", "案件詳細"], start=1):
        ev = collector.process(_info(cm, hwnd=i, title=title, proc="LegalSuite.exe"))
        assert ev is not None

    events = _read_events(tmp_path)
    for ev in events:
        ss = ev.get("screenshot")
        if ss is None:
            continue
        summary = ss.get("ocr_text_summary", "")
        # 依頼者会社名と事件番号がマスクされる
        assert "クライアント太郎" not in summary, f"client_name LEAK: {summary}"
        assert "令和5年(ワ)第12345号" not in summary, f"case_number LEAK: {summary}"


# ============================================================================
# 4. generic: 普通の会社のメール作成業務
# ============================================================================

def test_generic_email_workflow(tmp_path, monkeypatch):
    """汎用: メール作成、宛先メアド・電話・氏名がマスクされる事を検証."""
    _isolate(tmp_path, profile="generic")
    from ocr import OCRBox  # type: ignore

    scenario = [
        [OCRBox(text="メール作成", bbox=(50, 30, 300, 70), confidence=0.95),
         OCRBox(text="宛先 customer@example.com", bbox=(50, 100, 500, 140), confidence=0.95),
         OCRBox(text="電話 090-1234-5678", bbox=(50, 150, 500, 190), confidence=0.95),
         OCRBox(text="田中花子 様", bbox=(50, 200, 500, 240), confidence=0.95)],
    ]
    cm, collector = _make_collector(scenario, drop_image_if_unmaskable=False)
    _patch_capture(cm, monkeypatch)

    ev = collector.process(_info(cm, hwnd=1, title="新規メール - Outlook",
                                  proc="OUTLOOK.EXE"))
    assert ev is not None
    assert ev["app"]["category"] == "office"
    assert ev["app"]["rpa_target"] == "pad"

    events = _read_events(tmp_path)
    for ev in events:
        ss = ev.get("screenshot")
        if ss is None:
            continue
        summary = ss.get("ocr_text_summary", "")
        # base 共通PIIが全部マスクされる
        assert "customer@example.com" not in summary, f"email LEAK: {summary}"
        assert "090-1234-5678" not in summary, f"phone LEAK: {summary}"
        assert "田中花子" not in summary, f"name LEAK: {summary}"


# ============================================================================
# 5. アプリ別カバレッジ: 各業界アプリで category が正しく振られる
# ============================================================================

@pytest.mark.parametrize("title,proc,expected_category", [
    ("レセプト管理", "WeMex.exe", "industry_medical"),
    ("freee会計 - 仕訳入力", "chrome.exe", "industry_accounting"),
    ("SAP GUI - 受注", "saplogon.exe", "erp"),
    ("Slack - チームチャット", "Slack.exe", "saas_desktop"),
    ("商談一覧 - kintone.cybozu.com", "chrome.exe", "saas_web"),
    ("売上.xlsx - Excel", "EXCEL.EXE", "office"),
    ("Code.exe - main.py", "Code.exe", "dev"),
])
def test_app_category_routing(tmp_path, monkeypatch, title, proc, expected_category):
    _isolate(tmp_path, profile="generic")
    cm, collector = _make_collector(scenario_boxes=[])
    _patch_capture(cm, monkeypatch)

    ev = collector.process(_info(cm, hwnd=1, title=title, proc=proc))
    assert ev["app"]["category"] == expected_category


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
