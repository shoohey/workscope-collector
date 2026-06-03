"""analyzer CLI: events_dir → 業務マップHTML + RPAスクリプト生成.

使い方:
  python -m analyzer.cli \
    --events ~/Library/Application\\ Support/WorkScope/data/events \
    --output ./report.html \
    --rpa-output ./rpa-scripts \
    --customer "村上薬局" \
    --industry pharmacy \
    --days 14
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .detector import (
    detect_repeated_patterns,
    detect_work_units_per_device,
    load_events,
)
from .report_generator import write_report
from .rpa_generator import generate_all
from .scorer import score_patterns


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkScope analyzer")
    parser.add_argument("--events", required=True, type=Path,
                        help="JSONLイベントディレクトリ")
    parser.add_argument("--output", default=Path("report.html"), type=Path,
                        help="業務マップHTML出力先")
    parser.add_argument("--rpa-output", default=None, type=Path,
                        help="RPAスクリプト出力ディレクトリ (省略=生成しない)")
    parser.add_argument("--customer", default="顧客", type=str)
    parser.add_argument("--industry", default="generic", type=str)
    parser.add_argument("--days", default=14, type=int,
                        help="観測期間（日）。月換算に使う")
    parser.add_argument("--ngram", default=3, type=int,
                        help="反復パターンのN-gramサイズ")
    parser.add_argument("--min-occurrences", default=2, type=int,
                        help="反復パターンとみなす最小観測回数")
    parser.add_argument("--top-n", default=10, type=int,
                        help="RPA生成する上位候補数")
    parser.add_argument("--device", default=None, type=str,
                        help="特定の device_id のみを解析（省略=全PCをPC別に分離して解析）")
    args = parser.parse_args()

    if not args.events.exists():
        print(f"ERROR: events dir not found: {args.events}", file=sys.stderr)
        return 1

    print(f"[1/4] loading events from {args.events}...")
    events = list(load_events(args.events))
    print(f"      loaded {len(events)} events")

    print("[2/4] detecting work units & patterns (PC単位で分離)...")
    # device_id ごとに業務単位を検出する。これにより、同一顧客フォルダに
    # 複数 PC のデータが集約されていても、PC を跨いだ偽の業務遷移・滞在時間が
    # 生成されない。--device 指定時はその PC のみを対象にする。
    units_by_device = detect_work_units_per_device(events)
    if args.device is not None:
        units_by_device = {
            k: v for k, v in units_by_device.items() if k == args.device
        }
        if not units_by_device:
            print(f"ERROR: device_id not found: {args.device}", file=sys.stderr)
            return 1

    # 各 PC の hostname ラベルを集計（人間可読の PC 名）
    host_by_device: dict[str, str] = {}
    for ev in events:
        dev = ev.get("device_id") or ev.get("session_id") or "unknown"
        host = ev.get("hostname") or ""
        if host and dev not in host_by_device:
            host_by_device[dev] = host

    device_summary = []
    units = []  # 全 PC の業務単位を結合（各 unit は PC 内で閉じている）
    for dev, dev_units in sorted(units_by_device.items()):
        units.extend(dev_units)
        dev_ms = sum(u.duration_ms for u in dev_units)
        device_summary.append({
            "device_id": dev,
            "hostname": host_by_device.get(dev, ""),
            "unit_count": len(dev_units),
            "duration_ms": dev_ms,
        })

    patterns = detect_repeated_patterns(units, n=args.ngram,
                                         min_occurrences=args.min_occurrences)
    print(f"      {len(units_by_device)} PC / {len(units)} work units, "
          f"{len(patterns)} patterns")
    for d in device_summary:
        label = d["hostname"] or "(PC名不明)"
        mins = d["duration_ms"] // (1000 * 60)
        print(f"        - {label} [{d['device_id']}]: "
              f"{d['unit_count']}業務単位 / 約{mins}分")

    print("[3/4] scoring automation candidates...")
    candidates = score_patterns(patterns, observation_days=args.days)
    print(f"      {len(candidates)} candidates")

    print(f"[4/4] generating report → {args.output}")
    write_report(
        output_path=args.output,
        customer_name=args.customer,
        industry_profile=args.industry,
        observation_days=args.days,
        units=units, patterns=patterns, candidates=candidates,
        device_summary=device_summary,
    )

    if args.rpa_output is not None:
        top = candidates[: args.top_n]
        written = generate_all(top, args.rpa_output)
        print(f"      {len(written)} RPA scripts → {args.rpa_output}")

    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
