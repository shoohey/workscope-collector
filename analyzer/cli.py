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
    detect_work_units,
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
    args = parser.parse_args()

    if not args.events.exists():
        print(f"ERROR: events dir not found: {args.events}", file=sys.stderr)
        return 1

    print(f"[1/4] loading events from {args.events}...")
    events = list(load_events(args.events))
    print(f"      loaded {len(events)} events")

    print("[2/4] detecting work units & patterns...")
    units = detect_work_units(events)
    patterns = detect_repeated_patterns(units, n=args.ngram,
                                         min_occurrences=args.min_occurrences)
    print(f"      {len(units)} work units, {len(patterns)} patterns")

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
    )

    if args.rpa_output is not None:
        top = candidates[: args.top_n]
        written = generate_all(top, args.rpa_output)
        print(f"      {len(written)} RPA scripts → {args.rpa_output}")

    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
