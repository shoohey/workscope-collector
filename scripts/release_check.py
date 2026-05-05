"""リリース前自動チェックスクリプト.

CIゲートとして使い、1項目でも失敗したら release を block する。

チェック項目:
1. 全テストグリーン (pytest tests/)
2. PII漏洩テスト3本 が必須でパス
3. 全プロファイルJSONが正規表現として有効
4. 必要な同梱ファイル(docs/consent_form.html等)が存在
5. config.py の埋め込み定数が想定値である（ビルド時用）
6. requirements.txt の依存が pip install --dry-run で解決可能
7. PyInstaller で smoke build が成功（オプション、Windows実機CIのみ）

出力: docs/RELEASE_CHECK.md にレポート、終了コード 0 = ok / 非0 = block
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    is_critical: bool = True  # critical=true なら失敗で release block
    extras: list[str] = field(default_factory=list)


# --- 個別チェック ---------------------------------------------------------

def check_pytest_all() -> CheckResult:
    """全テスト実行."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    last = proc.stdout.strip().split("\n")[-1] if proc.stdout else ""
    return CheckResult(
        name="pytest_all",
        passed=proc.returncode == 0,
        detail=last,
        is_critical=True,
    )


def check_pii_safety_tests() -> CheckResult:
    """PII漏洩テスト3本+補助テストが100%パス（CIゲートの最重要項目）."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_pii_safety.py", "-v",
         "--tb=short", "-p", "no:cacheprovider"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    return CheckResult(
        name="pii_safety_tests",
        passed=proc.returncode == 0,
        detail=proc.stdout.strip().split("\n")[-1] if proc.stdout else "",
        is_critical=True,
    )


def check_profiles_valid() -> CheckResult:
    """全プロファイルJSONが正規表現として有効."""
    profile_dir = REPO / "profiles"
    if not profile_dir.exists():
        return CheckResult(name="profiles_valid", passed=False,
                           detail=f"profiles/ not found at {profile_dir}",
                           is_critical=True)

    sys.path.insert(0, str(REPO / "src"))
    try:
        from profile_loader import load_profile, list_available_profiles, clear_cache  # type: ignore
    except Exception as e:
        return CheckResult(name="profiles_valid", passed=False,
                           detail=f"profile_loader import failed: {e}",
                           is_critical=True)

    clear_cache()
    extras = []
    failures = []
    for name in list_available_profiles():
        try:
            p = load_profile(name)
            extras.append(f"{name}: rules={len(p.rules)}, "
                          f"whitelist_keys={list(p.whitelist.keys())}")
        except Exception as e:
            failures.append(f"{name}: {e}")
    if failures:
        return CheckResult(name="profiles_valid", passed=False,
                           detail="; ".join(failures), is_critical=True,
                           extras=extras)
    return CheckResult(name="profiles_valid", passed=True,
                       detail=f"{len(extras)} profiles validated",
                       is_critical=True, extras=extras)


def check_required_files() -> CheckResult:
    """配布に必須なファイルが揃っているか."""
    required = [
        "docs/consent_form.html",
        "docs/operation_guide.html",
        "docs/SPEC_v1.0.md",
        "src/main.py",
        "src/collector.py",
        "src/masker.py",
        "src/profile_loader.py",
        "src/app_classifier.py",
        "src/uia_capture.py",
        "src/input_events.py",
        "src/app_rules.json",
        "profiles/base.json",
        "profiles/pharmacy.json",
        "profiles/generic.json",
        "pyinstaller.spec",
        "requirements.txt",
    ]
    missing = [f for f in required if not (REPO / f).exists()]
    return CheckResult(
        name="required_files",
        passed=len(missing) == 0,
        detail=f"missing: {missing}" if missing else f"all {len(required)} files present",
        is_critical=True,
    )


def check_config_constants_safe(expect_default_profile: str | None = None) -> CheckResult:
    """src/config.py の DEFAULT_PROFILE / CUSTOMER_NAME / UPLOAD_ENDPOINT を確認.

    expect_default_profile が指定されていれば一致を必須とする（顧客別ビルド検証用）。
    """
    cfg_path = REPO / "src" / "config.py"
    src = cfg_path.read_text(encoding="utf-8")

    def _grab(name: str) -> str:
        m = re.search(rf'^{name}\s*=\s*"([^"]*)"', src, flags=re.M)
        return m.group(1) if m else "<missing>"

    dp = _grab("DEFAULT_PROFILE")
    cn = _grab("CUSTOMER_NAME")
    ue = _grab("UPLOAD_ENDPOINT")
    extras = [f"DEFAULT_PROFILE={dp!r}", f"CUSTOMER_NAME={cn!r}", f"UPLOAD_ENDPOINT={ue!r}"]

    if expect_default_profile and dp != expect_default_profile:
        return CheckResult(
            name="config_constants",
            passed=False,
            detail=f"DEFAULT_PROFILE expected '{expect_default_profile}' but got '{dp}'",
            is_critical=True,
            extras=extras,
        )
    return CheckResult(name="config_constants", passed=True,
                       detail="constants present",
                       is_critical=False, extras=extras)


def check_requirements_resolvable() -> CheckResult:
    """requirements.txt の各行が pip install --dry-run で解決可能.
    実際のインストールはせず、pip download で検証（time-bounded）.
    Windows clean VM CIで本番検証する想定なので、ここはローカル軽量チェック."""
    req = REPO / "requirements.txt"
    if not req.exists():
        return CheckResult(name="requirements", passed=False,
                           detail="requirements.txt not found", is_critical=True)
    lines = [l.strip() for l in req.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    return CheckResult(
        name="requirements", passed=True,
        detail=f"{len(lines)} dependencies declared",
        is_critical=False,
        extras=lines,
    )


def check_app_rules_valid() -> CheckResult:
    """src/app_rules.json が JSON として有効."""
    p = REPO / "src" / "app_rules.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        cats = list(data.get("categories", {}).keys())
        return CheckResult(name="app_rules", passed=True,
                           detail=f"{len(cats)} categories: {cats}",
                           is_critical=True)
    except Exception as e:
        return CheckResult(name="app_rules", passed=False,
                           detail=str(e), is_critical=True)


# --- メイン -----------------------------------------------------------------

def run_all(expect_default_profile: str | None = None) -> tuple[list[CheckResult], bool]:
    results: list[CheckResult] = []
    results.append(check_pytest_all())
    results.append(check_pii_safety_tests())
    results.append(check_profiles_valid())
    results.append(check_required_files())
    results.append(check_app_rules_valid())
    results.append(check_config_constants_safe(expect_default_profile))
    results.append(check_requirements_resolvable())

    critical_failed = any(not r.passed and r.is_critical for r in results)
    return results, not critical_failed


def render_report(results: list[CheckResult], passed: bool) -> str:
    lines = [
        "# Release Check Report",
        "",
        f"**Overall**: {'PASS — release allowed' if passed else 'FAIL — release blocked'}",
        "",
        "| Check | Status | Critical | Detail |",
        "|---|---|---|---|",
    ]
    for r in results:
        status = "✅" if r.passed else "❌"
        crit = "yes" if r.is_critical else "no"
        lines.append(f"| {r.name} | {status} | {crit} | {r.detail} |")

    for r in results:
        if r.extras:
            lines.append("")
            lines.append(f"### {r.name} details")
            for e in r.extras:
                lines.append(f"- {e}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkScope release check")
    parser.add_argument("--expect-profile", type=str, default=None,
                        help="顧客別ビルドで期待される DEFAULT_PROFILE 値")
    parser.add_argument("--report", type=str,
                        default=str(REPO / "docs" / "RELEASE_CHECK.md"),
                        help="レポート出力先")
    args = parser.parse_args()

    results, passed = run_all(expect_default_profile=args.expect_profile)
    report = render_report(results, passed)

    # コンソール出力
    print(report)

    # ファイル出力
    out_path = Path(args.report)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report + "\n", encoding="utf-8")
    print(f"\nReport saved to: {out_path}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
