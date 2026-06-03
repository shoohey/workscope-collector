"""業務単位グルーピング + 反復パターン検出.

入力: WorkScope Collector が生成する schema_version=2/3 の JSONL
       （v3 では各イベントに device_id / hostname が付与され、PC単位で分離解析できる）
出力: 業務一覧 + 反復パターン (N-gram) + アプリ別時間配分

設計方針:
- 業務単位 = 「同一アプリ内で連続するイベント列、ただし dwell 5分超で分離」
- 反復パターン = 同一アプリ内のフィールド入力順を 3-gram 化してクラスタリング
- アプリ分類カテゴリ (industry_medical/erp/...) を業務名の prefix に使う
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)


@dataclass
class WorkUnit:
    """業務単位（連続したイベント列）."""
    app_category: str           # "industry_medical" 等
    process_name: str           # "ReceptyNEXT.exe"
    title_first: str            # 開始時のwindow.title
    title_last: str             # 終了時のwindow.title
    titles_seen: list[str] = field(default_factory=list)  # 全タイトル順
    event_count: int = 0
    duration_ms: int = 0        # 開始 - 終了
    started_at: str = ""        # ISO timestamp
    ended_at: str = ""
    rpa_target: str = ""        # pywinauto/pad/selenium/computer_use
    field_focus_path: list[str] = field(default_factory=list)  # 操作したフィールド名列


@dataclass
class RepeatedPattern:
    """検出された反復パターン."""
    app_category: str
    pattern: tuple[str, ...]    # 例: ("患者検索", "患者詳細", "処方入力")
    occurrences: int            # 観測回数
    avg_duration_ms: float      # 1回あたり平均所要時間
    total_duration_ms: int      # 累積時間
    sample_started_at: str = ""


# ---- イベント読込 -------------------------------------------------------

def load_events(events_dir: Path) -> Iterator[dict]:
    """events_dir 配下の *.jsonl をすべて読み込む（壊れた行はスキップ）."""
    for p in sorted(events_dir.glob("*.jsonl")):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("skip broken JSONL line in %s", p)
        except OSError:
            logger.exception("failed to read %s", p)


# ---- 業務単位グルーピング ----------------------------------------------

def detect_work_units(
    events: Iterable[dict],
    split_dwell_seconds: float = 300.0,
) -> list[WorkUnit]:
    """連続イベント列を WorkUnit に分割.

    分割条件:
    - app_category が変わったら別業務
    - dwell_ms_prev が split_dwell_seconds * 1000 を超えたら別業務（休憩・離席相当）
    """
    units: list[WorkUnit] = []
    current: WorkUnit | None = None

    for ev in events:
        if ev.get("event_type") != "window_focus":
            # 入力イベント (key/mouse) は現在の WorkUnit に紐づくフィールド入力として処理
            if current is not None and ev.get("event_type") in ("uia_focus", "key_typed"):
                fc = ev.get("focused_control") or (ev.get("input") or {}).get("focused_control")
                if fc and fc.get("name"):
                    current.field_focus_path.append(fc["name"])
            continue

        app = ev.get("app", {})
        cat = app.get("category", "other")
        proc = app.get("process_name", "")
        title = (ev.get("window") or {}).get("title", "")
        ts = ev.get("ts", "")
        dwell = ev.get("dwell_ms_prev", 0)
        rpa = app.get("rpa_target", "")

        should_split = (
            current is None
            or current.app_category != cat
            or dwell > split_dwell_seconds * 1000
        )
        if should_split:
            if current is not None:
                units.append(current)
            current = WorkUnit(
                app_category=cat, process_name=proc, title_first=title,
                title_last=title, titles_seen=[title], event_count=1,
                duration_ms=0, started_at=ts, ended_at=ts, rpa_target=rpa,
            )
        else:
            current.title_last = title
            current.titles_seen.append(title)
            current.event_count += 1
            current.duration_ms += int(dwell)
            current.ended_at = ts

    if current is not None:
        units.append(current)
    return units


# ---- PC単位のグルーピング（複数PC運用時の混線防止） --------------------

def group_events_by_device(events: Iterable[dict]) -> dict[str, list[dict]]:
    """イベントを device_id 単位にグルーピングし、各群を ts 昇順に並べる.

    同一顧客フォルダに複数 PC のJSONLが集約されている場合に、PC を跨いだ
    偽の業務遷移・滞在時間が生成されるのを防ぐための前処理。

    キーの解決順:
    - device_id（schema v3 以降。端末固定）を最優先
    - 無ければ session_id（schema v2 以前。プロセス単位だが、少なくとも
      別セッションの混線は防げる）
    - どちらも無ければ "unknown"
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        key = ev.get("device_id") or ev.get("session_id") or "unknown"
        groups[key].append(ev)
    for key in groups:
        groups[key].sort(key=lambda e: e.get("ts") or "")
    return dict(groups)


def detect_work_units_per_device(
    events: Iterable[dict],
    split_dwell_seconds: float = 300.0,
) -> dict[str, list[WorkUnit]]:
    """device_id ごとに WorkUnit を検出して返す（PC単位の分離解析）.

    各 PC（device_id）内でのみ連続イベント列を業務単位に分割するため、
    PC-A の最後のアプリから PC-B の最初のアプリへの偽遷移が生じない。
    """
    grouped = group_events_by_device(events)
    return {
        dev: detect_work_units(evs, split_dwell_seconds)
        for dev, evs in grouped.items()
    }


# ---- 反復パターン検出 (N-gram) ------------------------------------------

def detect_repeated_patterns(
    units: Iterable[WorkUnit],
    n: int = 3,
    min_occurrences: int = 2,
) -> list[RepeatedPattern]:
    """各業務単位のtitle列から N-gram を抽出し、頻出パターンをまとめる."""
    pattern_counter: dict[tuple[str, str, ...], list[WorkUnit]] = defaultdict(list)

    for unit in units:
        titles = unit.titles_seen
        if len(titles) < n:
            continue
        for i in range(len(titles) - n + 1):
            key = (unit.app_category,) + tuple(titles[i:i + n])
            pattern_counter[key].append(unit)

    results: list[RepeatedPattern] = []
    for key, hits in pattern_counter.items():
        if len(hits) < min_occurrences:
            continue
        cat = key[0]
        pat = tuple(key[1:])
        # 平均時間 = 各 unit の duration_ms の平均
        durations = [u.duration_ms for u in hits]
        avg = sum(durations) / max(1, len(durations))
        total = sum(durations)
        results.append(RepeatedPattern(
            app_category=cat,
            pattern=pat,
            occurrences=len(hits),
            avg_duration_ms=avg,
            total_duration_ms=total,
            sample_started_at=hits[0].started_at,
        ))
    # 頻度の高い順にソート
    results.sort(key=lambda r: (-r.occurrences, -r.total_duration_ms))
    return results


# ---- アプリ別時間配分 ---------------------------------------------------

def app_time_distribution(units: Iterable[WorkUnit]) -> dict[str, int]:
    """アプリカテゴリ別の累積時間 (ms)."""
    dist: dict[str, int] = defaultdict(int)
    for u in units:
        dist[u.app_category] += u.duration_ms
    return dict(dist)


def process_time_distribution(units: Iterable[WorkUnit]) -> dict[str, int]:
    """プロセス名別の累積時間 (ms)."""
    dist: dict[str, int] = defaultdict(int)
    for u in units:
        dist[u.process_name] += u.duration_ms
    return dict(dist)


__all__ = [
    "WorkUnit",
    "RepeatedPattern",
    "load_events",
    "detect_work_units",
    "group_events_by_device",
    "detect_work_units_per_device",
    "detect_repeated_patterns",
    "app_time_distribution",
    "process_time_distribution",
]
