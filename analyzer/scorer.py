"""自動化候補スコアリング.

スコア = 頻度 × 月間時間 × 複雑度
- 頻度: 観測回数 (occurrences)
- 月間時間: 1日あたり時間 × 営業日数(20) で月換算
- 複雑度: 1.0 (シンプル) 〜 2.0 (複雑) で重み付け

RPA向き / エージェント向き の判定ロジック付き。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .detector import RepeatedPattern, WorkUnit


@dataclass
class AutomationCandidate:
    """自動化候補業務."""
    pattern: RepeatedPattern
    score: float
    monthly_minutes: float    # 月間想定削減時間（分）
    monthly_savings_yen: int  # 月間想定削減コスト（円、人件費 3000円/h で換算）
    rpa_target: str           # 業務単位の app_category から決まる RPA出口
    automation_kind: str      # "rpa" (定型) / "agent" (判断あり) / "human" (自動化困難)
    rationale: str = ""


# 月20営業日前提
BIZ_DAYS_PER_MONTH = 20
HOURLY_RATE_YEN = 3000  # 業務時給


def _classify_automation_kind(pattern: RepeatedPattern) -> tuple[str, str]:
    """RPA向き / エージェント向き / 人力 の判定.

    判定ロジック (シンプル版):
    - 観測回数 >= 5 かつ pattern が決まりきっている (variance低い) → "rpa"
    - 観測回数 >= 3 かつ画面遷移が多い (4以上) → "agent" (Computer Use)
    - それ以外 → "human"
    """
    if pattern.occurrences >= 5 and len(pattern.pattern) <= 3:
        return "rpa", "高頻度・定型操作（pywinauto/PAD/Seleniumで自動化容易）"
    if pattern.occurrences >= 3 and len(pattern.pattern) >= 4:
        return "agent", "中頻度・複雑操作（Claude Computer Use エージェント向き）"
    return "human", "低頻度または非定型（自動化ROIが低い、人力継続を推奨）"


def _rpa_target_from_category(app_category: str) -> str:
    """app_category から RPA出口を決める."""
    mapping = {
        "industry_medical": "pywinauto",
        "industry_accounting": "pad",
        "erp": "pywinauto",
        "saas_desktop": "pad",
        "saas_web": "selenium",
        "office": "pad",
        "browser": "selenium",
        "dev": "none",
    }
    return mapping.get(app_category, "computer_use")


def score_patterns(
    patterns: Iterable[RepeatedPattern],
    observation_days: int = 14,
) -> list[AutomationCandidate]:
    """検出された反復パターンを自動化スコアでランク付け.

    observation_days: データ収集期間（デフォルト2週間 = 14日）.
    月換算は 20日/14日 = 1.43倍する。
    """
    out: list[AutomationCandidate] = []
    monthly_factor = BIZ_DAYS_PER_MONTH / max(1, observation_days)

    for p in patterns:
        # 月間時間（分）
        monthly_ms = p.total_duration_ms * monthly_factor
        monthly_minutes = monthly_ms / (1000 * 60)
        # 月間削減コスト（円）
        monthly_savings = int(monthly_minutes / 60 * HOURLY_RATE_YEN)

        # 複雑度: パターン長が短いほどシンプル（=自動化しやすい）→ 高スコア
        complexity = 1.0 + (max(0, 6 - len(p.pattern)) / 5.0)

        # 総合スコア = 頻度 × 月間時間(分) × 複雑度
        score = p.occurrences * monthly_minutes * complexity

        kind, rationale = _classify_automation_kind(p)
        rpa = _rpa_target_from_category(p.app_category)

        out.append(AutomationCandidate(
            pattern=p,
            score=score,
            monthly_minutes=monthly_minutes,
            monthly_savings_yen=monthly_savings,
            rpa_target=rpa,
            automation_kind=kind,
            rationale=rationale,
        ))
    out.sort(key=lambda c: -c.score)
    return out


def top_candidates(
    candidates: Iterable[AutomationCandidate],
    top_n: int = 10,
) -> list[AutomationCandidate]:
    return list(candidates)[:top_n]


__all__ = [
    "AutomationCandidate",
    "score_patterns",
    "top_candidates",
    "BIZ_DAYS_PER_MONTH",
    "HOURLY_RATE_YEN",
]
