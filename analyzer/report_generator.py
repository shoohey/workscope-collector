"""業務マップHTMLレポート生成 (TRIBE¥30万案件の主要納品物).

トンマナ: グローバルCLAUDE.md準拠 (濃紺×白、A4横印刷対応)
"""

from __future__ import annotations

import html
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .detector import RepeatedPattern, WorkUnit, app_time_distribution
from .scorer import AutomationCandidate


CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  background: #ffffff;
  font-family: 'Noto Sans JP', sans-serif;
  color: #1a1a2e;
  line-height: 1.7;
  font-size: 14px;
}
.page {
  max-width: 1100px;
  margin: 24px auto;
  padding: 32px 40px;
}
h1 {
  font-family: 'Inter', sans-serif;
  font-weight: 800;
  font-size: 32px;
  color: #1e3a5f;
  margin-bottom: 8px;
}
h2 {
  font-family: 'Inter', sans-serif;
  font-weight: 800;
  font-size: 22px;
  color: #1e3a5f;
  margin-top: 36px;
  margin-bottom: 12px;
  border-bottom: 2px solid #e2e8f0;
  padding-bottom: 8px;
}
h3 { font-size: 16px; color: #1e3a5f; margin-top: 18px; margin-bottom: 8px; }
.lead { color: #4a5568; margin-bottom: 24px; }
.label { font-size: 11px; color: #4a5568; letter-spacing: 0.1em; text-transform: uppercase; }
.kpi-row { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
.kpi {
  flex: 1; min-width: 200px;
  background: #f5f7fa;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 16px 20px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.04);
}
.kpi .num {
  font-family: 'Inter', sans-serif;
  font-size: 36px;
  font-weight: 800;
  color: #1e3a5f;
}
.kpi .unit { font-size: 14px; color: #4a5568; margin-left: 6px; }
.kpi .lab { font-size: 12px; color: #4a5568; margin-bottom: 4px; }
table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 12px;
  font-size: 13px;
}
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #e2e8f0; }
th { background: #f5f7fa; color: #1e3a5f; font-weight: 700; }
.score-bar {
  background: #1e3a5f;
  height: 6px;
  border-radius: 3px;
  margin-top: 4px;
}
.tag {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: 11px; font-weight: 700;
}
.tag-rpa { background: #d4f1d4; color: #276749; }
.tag-agent { background: #fef0c7; color: #975a16; }
.tag-human { background: #fed7d7; color: #c53030; }
.bar-container { background: #f5f7fa; border-radius: 4px; overflow: hidden; }
.bar { background: #1e3a5f; height: 18px; padding: 0 8px; color: white; font-size: 11px; line-height: 18px; }
@media print {
  @page { size: A4 landscape; margin: 12mm; }
  body { font-size: 11px; }
  .page { margin: 0; padding: 0; }
}
"""


def _ms_to_min(ms: int) -> float:
    return ms / (1000 * 60)


def _format_duration(ms: int) -> str:
    minutes = ms // (1000 * 60)
    if minutes < 60:
        return f"{minutes}分"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours}時間{rem}分"


def _category_label(cat: str) -> str:
    labels = {
        "industry_medical": "業界アプリ-医療",
        "industry_accounting": "業界アプリ-会計",
        "erp": "ERP/基幹",
        "saas_desktop": "SaaS-Desktop",
        "saas_web": "SaaS-Web",
        "office": "Office",
        "browser": "Browser",
        "dev": "開発ツール",
        "other": "その他",
    }
    return labels.get(cat, cat)


def render_html(
    customer_name: str,
    industry_profile: str,
    observation_days: int,
    units: list[WorkUnit],
    patterns: list[RepeatedPattern],
    candidates: list[AutomationCandidate],
) -> str:
    """業務マップ HTML を生成して文字列で返す."""
    total_ms = sum(u.duration_ms for u in units)
    total_minutes = _ms_to_min(total_ms)
    monthly_total_minutes = total_minutes * (20 / max(1, observation_days))
    rpa_count = sum(1 for c in candidates if c.automation_kind == "rpa")
    agent_count = sum(1 for c in candidates if c.automation_kind == "agent")
    human_count = sum(1 for c in candidates if c.automation_kind == "human")

    monthly_savings_total = sum(c.monthly_savings_yen for c in candidates
                                 if c.automation_kind in ("rpa", "agent"))

    # アプリ別時間配分
    app_dist = app_time_distribution(units)
    app_dist_sorted = sorted(app_dist.items(), key=lambda kv: -kv[1])
    max_dist = max((v for _, v in app_dist_sorted), default=1)

    # KPI
    kpis = [
        ("業務単位数", f"{len(units)}", "件"),
        ("反復パターン", f"{len(patterns)}", "件"),
        ("月間総作業時間", f"{int(monthly_total_minutes / 60)}", "時間"),
        ("RPA化候補", f"{rpa_count}", "件"),
        ("月間削減見込", f"{monthly_savings_total // 10000}", "万円"),
    ]

    parts = ['<!DOCTYPE html><html lang="ja"><head>',
             '<meta charset="UTF-8">',
             f'<title>業務マップ - {html.escape(customer_name)}</title>',
             '<link rel="preconnect" href="https://fonts.googleapis.com">',
             '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=Noto+Sans+JP:wght@400;500;700&display=swap" rel="stylesheet">',
             f'<style>{CSS}</style></head><body>',
             '<div class="page">']

    # ヘッダー
    parts.append(f'<div class="label">業務まるごと可視化AI 納品レポート</div>')
    parts.append(f'<h1>業務マップ — {html.escape(customer_name)}</h1>')
    parts.append(
        f'<div class="lead">'
        f'業界プロファイル: <strong>{html.escape(industry_profile)}</strong> ／ '
        f'観測期間: <strong>{observation_days}日</strong> ／ '
        f'生成日時: {datetime.now().strftime("%Y-%m-%d %H:%M")}'
        f'</div>'
    )

    # KPI
    parts.append('<div class="kpi-row">')
    for lab, num, unit in kpis:
        parts.append(
            f'<div class="kpi">'
            f'<div class="lab">{html.escape(lab)}</div>'
            f'<div class="num">{html.escape(num)}<span class="unit">{html.escape(unit)}</span></div>'
            f'</div>'
        )
    parts.append('</div>')

    # アプリ別時間配分
    parts.append('<h2>アプリ別 時間配分</h2>')
    parts.append('<table>')
    parts.append('<tr><th>アプリ種別</th><th>累積時間</th><th>占有率</th></tr>')
    for cat, dur in app_dist_sorted:
        pct = (dur / max(1, total_ms)) * 100
        bar_w = int((dur / max(1, max_dist)) * 100)
        parts.append(
            f'<tr><td>{html.escape(_category_label(cat))}</td>'
            f'<td>{_format_duration(dur)}</td>'
            f'<td><div class="bar-container"><div class="bar" style="width:{bar_w}%">{pct:.1f}%</div></div></td></tr>'
        )
    parts.append('</table>')

    # 自動化候補上位10
    parts.append('<h2>自動化候補 上位10業務</h2>')
    parts.append(
        f'<div class="lead">'
        f'RPA化候補: <strong>{rpa_count}件</strong> / '
        f'AIエージェント候補: <strong>{agent_count}件</strong> / '
        f'人力継続推奨: <strong>{human_count}件</strong>'
        f'</div>'
    )
    parts.append('<table>')
    parts.append('<tr><th>順位</th><th>業務名（操作シーケンス）</th><th>頻度</th>'
                  '<th>月間時間</th><th>月間削減見込</th><th>RPA出口</th><th>分類</th></tr>')
    max_score = max((c.score for c in candidates[:10]), default=1)
    for i, c in enumerate(candidates[:10], start=1):
        pat_str = " → ".join(html.escape(t) for t in c.pattern.pattern)
        kind_label = {"rpa": "RPA向き", "agent": "AIエージェント", "human": "人力"}[c.automation_kind]
        kind_class = {"rpa": "tag-rpa", "agent": "tag-agent", "human": "tag-human"}[c.automation_kind]
        bar_w = int((c.score / max(1, max_score)) * 100)
        parts.append(
            f'<tr><td>{i}</td>'
            f'<td>{pat_str}<div class="score-bar" style="width:{bar_w}%"></div></td>'
            f'<td>{c.pattern.occurrences}回</td>'
            f'<td>{c.monthly_minutes:.0f}分</td>'
            f'<td>¥{c.monthly_savings_yen:,}</td>'
            f'<td><code>{html.escape(c.rpa_target)}</code></td>'
            f'<td><span class="tag {kind_class}">{kind_label}</span></td></tr>'
        )
    parts.append('</table>')

    # 全反復パターン
    parts.append('<h2>検出された全反復パターン</h2>')
    parts.append('<table>')
    parts.append('<tr><th>アプリ種別</th><th>パターン</th><th>頻度</th><th>累積時間</th></tr>')
    for p in patterns[:30]:
        pat_str = " → ".join(html.escape(t) for t in p.pattern)
        parts.append(
            f'<tr><td>{html.escape(_category_label(p.app_category))}</td>'
            f'<td>{pat_str}</td>'
            f'<td>{p.occurrences}回</td>'
            f'<td>{_format_duration(p.total_duration_ms)}</td></tr>'
        )
    parts.append('</table>')

    parts.append('</div></body></html>')
    return "".join(parts)


def write_report(
    output_path: Path,
    customer_name: str,
    industry_profile: str,
    observation_days: int,
    units: list[WorkUnit],
    patterns: list[RepeatedPattern],
    candidates: list[AutomationCandidate],
) -> Path:
    """HTMLレポートをファイル出力."""
    html_content = render_html(
        customer_name=customer_name,
        industry_profile=industry_profile,
        observation_days=observation_days,
        units=units, patterns=patterns, candidates=candidates,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_content, encoding="utf-8")
    return output_path


__all__ = ["render_html", "write_report"]
