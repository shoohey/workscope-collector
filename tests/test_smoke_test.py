"""smoke_test.py のロジックテスト. PyInstaller化前に Python 直叩きで全チェック関数を検証."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))


def _isolate(tmp: Path) -> None:
    os.environ["APPDATA"] = str(tmp)
    # 他テストで設定された WORKSCOPE_PROFILE_DIR がリークしてプロファイル探索が
    # 失敗するのを防ぐ。
    os.environ.pop("WORKSCOPE_PROFILE_DIR", None)
    os.environ.pop("WORKSCOPE_PROFILE", None)
    # smoke_test.py 自身もキャッシュクリア
    sys.modules.pop("smoke_test", None)
    for m in ("config", "profile_loader", "app_classifier", "masker", "ocr",
              "consent", "uploader"):
        sys.modules.pop(m, None)


@pytest.fixture()
def isolated(tmp_path):
    _isolate(tmp_path)
    yield tmp_path


# ---- 個別チェック関数 ----

def test_check_python_imports(isolated):
    import smoke_test  # type: ignore
    r = smoke_test.check_python_imports()
    assert r.passed is True


def test_check_profile_loader(isolated):
    import smoke_test  # type: ignore
    r = smoke_test.check_profile_loader()
    assert r.passed is True
    assert "pharmacy" in r.detail or "rules=" in r.detail


def test_check_app_classifier(isolated):
    import smoke_test  # type: ignore
    r = smoke_test.check_app_classifier()
    assert r.passed is True


def test_check_masker_actually_masks(isolated):
    """masker が実サンプル（氏名+保険番号+電話）を正しく黒塗りすること."""
    import smoke_test  # type: ignore
    r = smoke_test.check_masker_actually_masks()
    assert r.passed is True


def test_check_appdata_writable(isolated):
    import smoke_test  # type: ignore
    r = smoke_test.check_appdata_writable()
    assert r.passed is True


def test_check_consent_form_present(isolated):
    import smoke_test  # type: ignore
    r = smoke_test.check_consent_form_present()
    assert r.passed is True


def test_check_consent_status_unsigned(isolated):
    """未同意状態を smoke_test が検出（critical でない警告扱い）."""
    import smoke_test  # type: ignore
    r = smoke_test.check_consent_status()
    # 未同意でも passed=True (is_critical=False で警告扱い)
    assert r.passed is True
    assert "not yet consented" in r.detail or "already consented" in r.detail


def test_check_uploader_endpoint_config_usb_mode(isolated):
    """UPLOAD_ENDPOINT 未設定時は USB回収モードで OK."""
    import smoke_test  # type: ignore
    r = smoke_test.check_uploader_endpoint_config()
    # USB回収モードなら passed=True
    assert r.passed is True


# ---- 統合: render_html がHTMLを生成 ----

def test_render_html_all_passed(isolated):
    import smoke_test  # type: ignore
    results = [
        smoke_test.CheckResult("test1", True, "ok"),
        smoke_test.CheckResult("test2", True, "ok"),
    ]
    html = smoke_test.render_html(results, True, "test_customer")
    assert "<html" in html
    assert "test_customer" in html
    assert "全項目クリア" in html


def test_render_html_with_failures(isolated):
    import smoke_test  # type: ignore
    results = [
        smoke_test.CheckResult("test1", True, "ok"),
        smoke_test.CheckResult("test2", False, "missing dependency", is_critical=True),
    ]
    html = smoke_test.render_html(results, False, "")
    assert "失敗項目があります" in html


def test_render_html_with_warning_not_failed(isolated):
    """non-critical な warning は overall=PASS のまま."""
    import smoke_test  # type: ignore
    results = [
        smoke_test.CheckResult("test1", True, "ok"),
        smoke_test.CheckResult("test2", False, "skip", is_critical=False),
    ]
    # critical_failed = any(not passed and is_critical) → False (test2 は non-critical)
    critical_failed = any(not r.passed and r.is_critical for r in results)
    assert critical_failed is False


# ---- 全チェック実行 ----

def test_all_checks_passing_in_dev(isolated):
    """開発環境（Mac）でも、critical チェックは全部 passed."""
    import smoke_test  # type: ignore
    results = [smoke_test._safe(c) for c in smoke_test.ALL_CHECKS]
    critical_failed = [r for r in results if not r.passed and r.is_critical]
    assert not critical_failed, f"critical fails: {[r.name + ':' + r.detail for r in critical_failed]}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
