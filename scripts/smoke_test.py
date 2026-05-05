"""WorkScope smoke test — 配布物の動作確認用ツール.

顧客先で本体EXEを起動する前に、ダブルクリックして1分で「ちゃんとインストールできたか」
を確認するためのバイナリ。チェック項目:

1. Python依存（PIL/numpy/win32等）が全部 import できる
2. profile_loader でデフォルトプロファイルが読み込める
3. app_classifier がアプリ分類できる
4. masker が実サンプル（氏名+保険番号）を正しく黒塗りする
5. AppData ディレクトリに書き込み権限がある
6. consent_form.html が同梱されている
7. 同意状態（未同意ならその旨を表示）

結果は HTML として表示（PyInstaller --noconsole でもブラウザで確認できる）。
全項目 ✅ ならメッセージで「本体起動可能」、1項目でも ❌ なら原因明記。
"""

from __future__ import annotations

import os
import platform
import sys
import tempfile
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple

# PyInstaller frozen でも src/ の同梱モジュールを import できるよう sys.path に追加
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
# PyInstaller frozen 時は _MEIPASS から
if getattr(sys, "frozen", False):
    _MEIPASS = getattr(sys, "_MEIPASS", "")
    if _MEIPASS:
        for sub in ("", "src"):
            p = os.path.join(_MEIPASS, sub) if sub else _MEIPASS
            if p not in sys.path:
                sys.path.insert(0, p)


class CheckResult(NamedTuple):
    name: str
    passed: bool
    detail: str
    is_critical: bool = True


def _safe(fn: Callable[[], CheckResult]) -> CheckResult:
    try:
        return fn()
    except Exception:
        tb = traceback.format_exc(limit=3)
        return CheckResult(
            name=fn.__name__,
            passed=False,
            detail=f"unexpected exception: {tb}",
        )


# ---- 個別チェック -------------------------------------------------------

def check_python_imports() -> CheckResult:
    missing = []
    for mod in ("PIL", "numpy", "json", "re", "pathlib"):
        try:
            __import__(mod)
        except Exception as e:
            missing.append(f"{mod}: {e}")
    if missing:
        return CheckResult("python_imports", False, "; ".join(missing))
    return CheckResult("python_imports", True, "core dependencies importable")


def check_windows_bindings() -> CheckResult:
    """Windows 固有モジュール（フローズン or Windows 上のみ必須）."""
    if sys.platform != "win32" and not getattr(sys, "frozen", False):
        return CheckResult(
            "windows_bindings", True,
            "skipped (development env, not Windows)",
            is_critical=False,
        )
    missing = []
    for mod in ("win32gui", "win32process", "psutil", "mss"):
        try:
            __import__(mod)
        except Exception as e:
            missing.append(f"{mod}: {e}")
    if missing:
        return CheckResult("windows_bindings", False, "; ".join(missing))
    return CheckResult("windows_bindings", True, "Windows API bindings ok")


def check_profile_loader() -> CheckResult:
    from profile_loader import load_profile, get_default_profile_name  # type: ignore
    name = get_default_profile_name() or "pharmacy"
    profile = load_profile(name)
    return CheckResult(
        "profile_loader",
        passed=len(profile.rules) > 0,
        detail=f"profile='{name}' rules={len(profile.rules)} whitelist={list(profile.whitelist.keys())}",
    )


def check_app_classifier() -> CheckResult:
    from app_classifier import classify, list_categories  # type: ignore
    cats = list_categories()
    sample = classify(process_name="ReceptyNEXT.exe", window_title="処方せん入力")
    if sample.category != "industry_medical":
        return CheckResult("app_classifier", False,
                           f"unexpected category: {sample.category}")
    return CheckResult(
        "app_classifier", True,
        f"{len(cats)} categories, sample → {sample.category}/{sample.rpa_target}",
    )


def check_masker_actually_masks() -> CheckResult:
    from PIL import Image  # type: ignore
    from masker import mask_image  # type: ignore
    from ocr import OCRBox  # type: ignore

    img = Image.new("RGB", (800, 200), (255, 255, 255))
    boxes = [
        OCRBox(text="鈴木太郎 様", bbox=(20, 20, 400, 60), confidence=0.95),
        OCRBox(text="保険者番号 12345678", bbox=(20, 80, 600, 120), confidence=0.95),
        OCRBox(text="090-1234-5678", bbox=(20, 140, 400, 180), confidence=0.95),
    ]
    result = mask_image(img, boxes, strict=True)
    failed: list[str] = []
    if "鈴木太郎" in result.text_summary:
        failed.append("name leak")
    if "12345678" in result.text_summary:
        failed.append("insurance leak")
    if "090-1234-5678" in result.text_summary:
        failed.append("phone leak")
    if failed:
        return CheckResult("masker_works", False, ", ".join(failed))
    return CheckResult(
        "masker_works", True,
        f"masked {result.mask_count} regions, categories={result.mask_categories}",
    )


def check_appdata_writable() -> CheckResult:
    """AppData/WorkScope に書き込み権限があるか."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    target_dir = Path(appdata) / "WorkScope" / "data"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        test_file = target_dir / ".smoke_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        return CheckResult("appdata_writable", True, f"path={target_dir}")
    except OSError as e:
        return CheckResult("appdata_writable", False, str(e))


def check_consent_form_present() -> CheckResult:
    """consent_form.html が同梱されているか."""
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "docs" / "consent_form.html")
    candidates.append(_REPO_ROOT / "docs" / "consent_form.html")
    for c in candidates:
        if c.exists():
            return CheckResult("consent_form_present", True, str(c))
    return CheckResult(
        "consent_form_present", False,
        f"not found in: {[str(c) for c in candidates]}",
    )


def check_consent_status() -> CheckResult:
    """同意済みかどうか. 未同意でもエラーではなく警告扱い."""
    try:
        from consent import is_consented, consent_file_path  # type: ignore
    except Exception as e:
        return CheckResult("consent_status", False, f"consent module import failed: {e}")
    if is_consented():
        return CheckResult("consent_status", True, f"already consented at {consent_file_path()}")
    return CheckResult(
        "consent_status", True,
        "not yet consented (will show dialog on first launch)",
        is_critical=False,
    )


def check_uploader_endpoint_config() -> CheckResult:
    """送信先設定が allowlist 通過するか. アップロード未設定 (USB回収) でもOK."""
    try:
        from config import UPLOAD_ENDPOINT, UPLOAD_API_KEY  # type: ignore
        from uploader import is_endpoint_allowed  # type: ignore
    except Exception as e:
        return CheckResult("uploader_config", False, f"import failed: {e}")
    if not UPLOAD_ENDPOINT:
        return CheckResult(
            "uploader_config", True,
            "USB回収モード (cloud upload disabled)",
            is_critical=False,
        )
    ok, reason = is_endpoint_allowed(UPLOAD_ENDPOINT)
    if not ok:
        return CheckResult("uploader_config", False, f"endpoint blocked: {reason}")
    has_key = bool(UPLOAD_API_KEY)
    return CheckResult(
        "uploader_config", has_key,
        f"endpoint allowed; api_key {'set' if has_key else 'MISSING'}",
    )


# ---- メイン ---------------------------------------------------------------

ALL_CHECKS: list[Callable[[], CheckResult]] = [
    check_python_imports,
    check_windows_bindings,
    check_profile_loader,
    check_app_classifier,
    check_masker_actually_masks,
    check_appdata_writable,
    check_consent_form_present,
    check_consent_status,
    check_uploader_endpoint_config,
]


def render_html(results: list[CheckResult], all_passed: bool, customer: str) -> str:
    rows = []
    for r in results:
        if r.passed:
            mark = "✅"
            color = "#276749"
        elif r.is_critical:
            mark = "❌"
            color = "#c53030"
        else:
            mark = "⚠️"
            color = "#975a16"
        rows.append(
            f'<tr><td style="text-align:center;font-size:18px;color:{color};">{mark}</td>'
            f'<td><b>{r.name}</b></td>'
            f'<td style="color:#4a5568;font-size:13px;">{r.detail}</td></tr>'
        )

    overall_msg = (
        ('<div style="background:#d4f1d4;color:#276749;padding:16px;border-radius:8px;'
         'font-size:18px;font-weight:700;margin:20px 0;">✅ 全項目クリア — 本体EXEを起動できます</div>')
        if all_passed else
        ('<div style="background:#fed7d7;color:#c53030;padding:16px;border-radius:8px;'
         'font-size:18px;font-weight:700;margin:20px 0;">❌ 失敗項目があります — 詳細を確認してください</div>')
    )

    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="UTF-8">
<title>WorkScope Smoke Test</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;800&family=Noto+Sans+JP:wght@400;700&display=swap" rel="stylesheet">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Noto Sans JP', sans-serif; background: #f5f7fa; color: #1a1a2e; padding: 24px; line-height: 1.7; }}
.container {{ max-width: 800px; margin: 0 auto; background: #fff; padding: 32px; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }}
h1 {{ font-family: 'Inter', sans-serif; color: #1e3a5f; font-size: 24px; margin-bottom: 4px; }}
.subtitle {{ color: #4a5568; font-size: 13px; margin-bottom: 20px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #e2e8f0; }}
th {{ background: #f5f7fa; color: #1e3a5f; font-weight: 700; }}
.footer {{ margin-top: 24px; padding-top: 16px; border-top: 1px solid #e2e8f0; color: #4a5568; font-size: 12px; }}
</style></head>
<body>
<div class="container">
  <h1>WorkScope Smoke Test</h1>
  <p class="subtitle">配布物の動作確認 / Customer: <b>{customer or "(unset)"}</b> / {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
  {overall_msg}
  <table>
    <tr><th style="width:40px;">結果</th><th style="width:200px;">チェック項目</th><th>詳細</th></tr>
    {''.join(rows)}
  </table>
  <div class="footer">
    Platform: {platform.platform()}<br>
    Python: {sys.version.split()[0]}<br>
    Frozen: {getattr(sys, 'frozen', False)}
  </div>
</div>
</body></html>"""


def main() -> int:
    customer = ""
    try:
        from config import CUSTOMER_NAME  # type: ignore
        customer = CUSTOMER_NAME or ""
    except Exception:
        pass

    results = [_safe(c) for c in ALL_CHECKS]
    critical_failed = any(not r.passed and r.is_critical for r in results)
    all_passed = not critical_failed

    html = render_html(results, all_passed, customer)
    out = Path(tempfile.gettempdir()) / f"workscope_smoke_test_{os.getpid()}.html"
    out.write_text(html, encoding="utf-8")

    # 結果ファイルをデフォルトブラウザで開く
    try:
        webbrowser.open(out.as_uri())
    except Exception:
        pass

    # コンソール（PyInstaller --console 時のみ表示される）
    print("=" * 60)
    print("WorkScope Smoke Test")
    print("=" * 60)
    for r in results:
        mark = "OK " if r.passed else ("FAIL" if r.is_critical else "warn")
        print(f"  [{mark}] {r.name}: {r.detail}")
    print("=" * 60)
    print(f"Overall: {'PASS' if all_passed else 'FAIL'}")
    print(f"Report: {out}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
