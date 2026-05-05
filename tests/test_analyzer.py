"""analyzer (detector/scorer/report_generator/rpa_generator) のテスト."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyzer.detector import (  # noqa: E402
    RepeatedPattern,
    WorkUnit,
    app_time_distribution,
    detect_repeated_patterns,
    detect_work_units,
    load_events,
)
from analyzer.report_generator import render_html  # noqa: E402
from analyzer.rpa_generator import (  # noqa: E402
    generate_all,
    generate_for_candidate,
    generate_pad,
    generate_playwright,
    generate_pywinauto,
    generate_computer_use,
)
from analyzer.scorer import (  # noqa: E402
    AutomationCandidate,
    score_patterns,
    top_candidates,
)


# ---- ヘルパ -----------------------------------------------------------

def _ev(seq: int, title: str, proc: str = "ReceptyNEXT.exe",
        category: str = "industry_medical", dwell_ms: int = 1000,
        ts: str = "2026-05-05T10:00:00+09:00") -> dict:
    return {
        "schema_version": 2,
        "session_id": "s1", "event_seq": seq,
        "ts": ts, "event_type": "window_focus",
        "app": {"process_name": proc, "process_path": f"C:\\{proc}",
                "pid": 1, "category": category, "rpa_target": "pywinauto"},
        "window": {"title": title, "title_raw_hash": "abc123" + str(seq),
                   "title_mask_categories": [],
                   "hwnd": 1, "rect": [0, 0, 800, 600], "monitor": 1},
        "focused_control": None,
        "dwell_ms_prev": dwell_ms,
        "screenshot": None,
        "transition_from_app": "",
    }


# ============================================================================
# detector.detect_work_units
# ============================================================================

def test_detect_work_units_groups_by_app_category() -> None:
    events = [
        _ev(1, "患者検索", category="industry_medical"),
        _ev(2, "患者詳細", category="industry_medical"),
        _ev(3, "売上.xlsx", proc="EXCEL.EXE", category="office"),
    ]
    units = detect_work_units(events)
    assert len(units) == 2
    assert units[0].app_category == "industry_medical"
    assert units[0].event_count == 2
    assert units[1].app_category == "office"


def test_detect_work_units_splits_on_long_dwell() -> None:
    events = [
        _ev(1, "画面A", dwell_ms=1000),
        _ev(2, "画面B", dwell_ms=400000),  # 6分超 → 別業務
    ]
    units = detect_work_units(events, split_dwell_seconds=300.0)
    assert len(units) == 2


def test_detect_work_units_empty() -> None:
    assert detect_work_units([]) == []


# ============================================================================
# detector.detect_repeated_patterns
# ============================================================================

def test_detect_repeated_patterns_finds_3gram() -> None:
    events = []
    # 同じ3画面遷移を3回繰り返す
    for round_idx in range(3):
        for i, t in enumerate(["患者検索", "患者詳細", "処方入力", "確認"]):
            events.append(_ev(round_idx * 10 + i, t,
                              ts=f"2026-05-05T10:0{round_idx}:00+09:00"))
    units = detect_work_units(events)
    patterns = detect_repeated_patterns(units, n=3, min_occurrences=2)
    assert len(patterns) >= 1
    # 「患者検索 → 患者詳細 → 処方入力」が頻出
    found = any(p.pattern == ("患者検索", "患者詳細", "処方入力") for p in patterns)
    assert found


def test_detect_repeated_patterns_below_min_returns_empty() -> None:
    events = [_ev(i, t) for i, t in enumerate(["A", "B", "C"])]
    units = detect_work_units(events)
    # min_occurrences=2 だが1回しか観測されない
    patterns = detect_repeated_patterns(units, n=3, min_occurrences=2)
    assert patterns == []


# ============================================================================
# detector.app_time_distribution
# ============================================================================

def test_app_time_distribution() -> None:
    units = [
        WorkUnit(app_category="industry_medical", process_name="x", title_first="",
                 title_last="", duration_ms=10000),
        WorkUnit(app_category="office", process_name="y", title_first="",
                 title_last="", duration_ms=5000),
        WorkUnit(app_category="industry_medical", process_name="x", title_first="",
                 title_last="", duration_ms=3000),
    ]
    dist = app_time_distribution(units)
    assert dist["industry_medical"] == 13000
    assert dist["office"] == 5000


# ============================================================================
# scorer
# ============================================================================

def test_score_patterns_ranks_by_score() -> None:
    p1 = RepeatedPattern(app_category="industry_medical",
                         pattern=("A", "B", "C"),
                         occurrences=10, avg_duration_ms=5000,
                         total_duration_ms=50000)
    p2 = RepeatedPattern(app_category="industry_medical",
                         pattern=("X", "Y", "Z"),
                         occurrences=2, avg_duration_ms=1000,
                         total_duration_ms=2000)
    candidates = score_patterns([p1, p2], observation_days=14)
    assert candidates[0].pattern == p1  # スコア高い順
    assert candidates[0].score > candidates[1].score


def test_score_classifies_rpa_for_high_freq_short_pattern() -> None:
    p = RepeatedPattern(app_category="industry_medical",
                        pattern=("A", "B", "C"),  # 短い
                        occurrences=10,
                        avg_duration_ms=1000, total_duration_ms=10000)
    candidates = score_patterns([p])
    assert candidates[0].automation_kind == "rpa"
    assert candidates[0].rpa_target == "pywinauto"


def test_score_classifies_agent_for_complex_pattern() -> None:
    p = RepeatedPattern(app_category="other",
                        pattern=("A", "B", "C", "D", "E"),  # 長い
                        occurrences=4, avg_duration_ms=3000, total_duration_ms=12000)
    candidates = score_patterns([p])
    assert candidates[0].automation_kind == "agent"


def test_score_classifies_human_for_low_freq() -> None:
    p = RepeatedPattern(app_category="industry_medical",
                        pattern=("A", "B"),
                        occurrences=2, avg_duration_ms=1000, total_duration_ms=2000)
    candidates = score_patterns([p])
    assert candidates[0].automation_kind == "human"


def test_score_monthly_savings_calculation() -> None:
    """月間時間→削減コスト: 60分=¥3000 (時給3000円)."""
    p = RepeatedPattern(app_category="office",
                        pattern=("A", "B", "C"),
                        occurrences=10,
                        avg_duration_ms=60_000,    # 1分/回
                        total_duration_ms=600_000)  # 計10分
    candidates = score_patterns([p], observation_days=20)
    # 月20日観測 → そのまま月10分 → ¥500
    assert candidates[0].monthly_savings_yen == 500


# ============================================================================
# report_generator
# ============================================================================

def test_render_html_minimal() -> None:
    html_str = render_html(
        customer_name="村上薬局",
        industry_profile="pharmacy",
        observation_days=14,
        units=[], patterns=[], candidates=[],
    )
    assert "<html" in html_str
    assert "村上薬局" in html_str
    assert "pharmacy" in html_str


def test_render_html_with_data() -> None:
    units = [WorkUnit(app_category="industry_medical", process_name="x",
                      title_first="A", title_last="C",
                      titles_seen=["A", "B", "C"], event_count=3,
                      duration_ms=60000)]
    patterns = [RepeatedPattern(
        app_category="industry_medical",
        pattern=("A", "B", "C"),
        occurrences=5, avg_duration_ms=12000, total_duration_ms=60000,
    )]
    candidates = score_patterns(patterns)
    html_str = render_html("テスト", "pharmacy", 14, units, patterns, candidates)
    # 上位10に含まれる
    assert "A → B → C" in html_str
    # 自動化候補テーブルが描画される
    assert "RPA" in html_str or "rpa" in html_str.lower()


# ============================================================================
# rpa_generator
# ============================================================================

def test_generate_pywinauto() -> None:
    c = AutomationCandidate(
        pattern=RepeatedPattern("industry_medical", ("画面A", "画面B"),
                                  10, 1000, 10000),
        score=100.0, monthly_minutes=10, monthly_savings_yen=500,
        rpa_target="pywinauto", automation_kind="rpa",
    )
    code = generate_pywinauto(c, "test_task")
    assert "from pywinauto import Application" in code
    assert "test_task" in code
    assert "画面A" in code


def test_generate_pad_returns_valid_json() -> None:
    c = AutomationCandidate(
        pattern=RepeatedPattern("office", ("仕訳入力", "保存"),
                                  5, 2000, 10000),
        score=50.0, monthly_minutes=10, monthly_savings_yen=500,
        rpa_target="pad", automation_kind="rpa",
    )
    js = generate_pad(c, "task_journal")
    parsed = json.loads(js)
    assert parsed["FlowName"] == "task_journal"
    assert parsed["Stats"]["Occurrences"] == 5


def test_generate_playwright() -> None:
    c = AutomationCandidate(
        pattern=RepeatedPattern("saas_web", ("ログイン", "商談入力"),
                                  8, 3000, 24000),
        score=80.0, monthly_minutes=20, monthly_savings_yen=1000,
        rpa_target="selenium", automation_kind="rpa",
    )
    code = generate_playwright(c, "salesforce_lead")
    assert "@playwright/test" in code
    assert "salesforce_lead" in code
    assert "ログイン" in code


def test_generate_computer_use_returns_valid_json() -> None:
    c = AutomationCandidate(
        pattern=RepeatedPattern("other", ("複雑画面1", "判断", "複雑画面2", "保存"),
                                  3, 5000, 15000),
        score=40.0, monthly_minutes=15, monthly_savings_yen=750,
        rpa_target="computer_use", automation_kind="agent",
    )
    js = generate_computer_use(c, "agent_task")
    parsed = json.loads(js)
    assert parsed["name"] == "agent_task"
    assert "computer" in parsed["tools"]
    assert "複雑画面1" in parsed["system_prompt"]


def test_generate_all_writes_files(tmp_path: Path) -> None:
    candidates = [
        AutomationCandidate(
            pattern=RepeatedPattern("industry_medical", ("A", "B", "C"),
                                      10, 1000, 10000),
            score=100, monthly_minutes=10, monthly_savings_yen=500,
            rpa_target="pywinauto", automation_kind="rpa",
        ),
        AutomationCandidate(
            pattern=RepeatedPattern("saas_web", ("ログイン", "検索"),
                                      5, 2000, 10000),
            score=50, monthly_minutes=10, monthly_savings_yen=500,
            rpa_target="selenium", automation_kind="rpa",
        ),
    ]
    out_dir = tmp_path / "rpa_out"
    paths = generate_all(candidates, out_dir)
    assert len(paths) == 2
    assert any(p.suffix == ".py" for p in paths)
    assert any(str(p).endswith(".spec.ts") for p in paths)


def test_generate_for_candidate_dispatches_by_rpa_target() -> None:
    c1 = AutomationCandidate(
        pattern=RepeatedPattern("industry_medical", ("A",), 10, 1000, 10000),
        score=10, monthly_minutes=10, monthly_savings_yen=500,
        rpa_target="pad", automation_kind="rpa",
    )
    content, ext = generate_for_candidate(c1, "test")
    assert ext == ".padfile.json"

    c2 = AutomationCandidate(
        pattern=RepeatedPattern("other", ("A",), 1, 1, 1),
        score=1, monthly_minutes=1, monthly_savings_yen=1,
        rpa_target="unknown_target", automation_kind="agent",
    )
    content, ext = generate_for_candidate(c2, "test")
    # 不明な rpa_target は computer_use にフォールバック
    assert ext == ".agent.json"


# ============================================================================
# 統合: load_events + 全パイプライン
# ============================================================================

def test_full_pipeline_with_jsonl(tmp_path: Path) -> None:
    """JSONL書き込み→読込→検出→スコアリング→HTML生成の統合フロー."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()

    # 同じ3画面シーケンスを3回繰り返すJSONL
    events = []
    for r in range(3):
        for i, t in enumerate(["A", "B", "C"]):
            events.append(_ev(r * 10 + i, t,
                              ts=f"2026-05-05T10:0{r}:0{i}+09:00"))

    p = events_dir / "2026-05-05.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    loaded = list(load_events(events_dir))
    assert len(loaded) == 9

    units = detect_work_units(loaded)
    patterns = detect_repeated_patterns(units, n=3, min_occurrences=2)
    candidates = score_patterns(patterns)
    assert len(candidates) >= 1

    html_str = render_html("テスト顧客", "pharmacy", 14, units, patterns, candidates)
    assert "テスト顧客" in html_str


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
