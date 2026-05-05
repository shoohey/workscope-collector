"""consent.py のテスト. 同意記録の永続化と判定ロジックを検証.

GUI ダイアログ自体はヘッドレス環境では実行できないので関数の動作のみ確認."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from consent import (  # noqa: E402
    ConsentRecord,
    consent_file_path,
    ensure_consent_or_exit,
    get_bundled_consent_html,
    is_consented,
    record_consent,
    revoke_consent,
    show_consent_dialog,
)


@pytest.fixture()
def isolated_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    yield tmp_path


# ---- 1. 初期状態は未同意 ---------------------------------------------------

def test_initial_state_not_consented(isolated_appdata):
    assert is_consented() is False
    assert not consent_file_path().exists()


# ---- 2. record_consent → ファイル生成 ------------------------------------

def test_record_consent_creates_file(isolated_appdata):
    rec = record_consent(
        customer_name="村上薬局",
        industry_profile="pharmacy",
        upload_endpoint="https://example.com/upload",
    )
    assert is_consented() is True
    p = consent_file_path()
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["customer_name"] == "村上薬局"
    assert data["industry_profile"] == "pharmacy"
    assert data["upload_endpoint"] == "https://example.com/upload"
    assert data["consented_at"]  # 非空
    assert rec.customer_name == "村上薬局"


# ---- 3. revoke_consent → ファイル削除 ----------------------------------

def test_revoke_consent_removes_file(isolated_appdata):
    record_consent(customer_name="X", industry_profile="generic")
    assert is_consented() is True

    revoke_consent()
    assert is_consented() is False
    assert not consent_file_path().exists()


def test_revoke_consent_idempotent(isolated_appdata):
    revoke_consent()  # 未同意でも例外なし
    revoke_consent()


# ---- 4. is_consented: 壊れたファイルは未同意扱い ---------------------------

def test_is_consented_handles_corrupted_file(isolated_appdata):
    p = consent_file_path()
    p.write_text("not valid json{{{", encoding="utf-8")
    assert is_consented() is False


def test_is_consented_handles_missing_consented_at(isolated_appdata):
    """consented_at が空のJSONは未同意扱い."""
    p = consent_file_path()
    p.write_text(json.dumps({"customer_name": "X", "consented_at": ""}),
                 encoding="utf-8")
    assert is_consented() is False


# ---- 5. ensure_consent_or_exit: 既に同意済みなら True ---------------------

def test_ensure_consent_returns_true_if_already_consented(isolated_appdata):
    record_consent(customer_name="X", industry_profile="pharmacy")
    # is_consented=True なのでダイアログを開かず True
    result = ensure_consent_or_exit(customer_name="X", industry_profile="pharmacy")
    assert result is True


# ---- 6. show_consent_dialog: ヘッドレス環境では False -------------------

def test_show_consent_dialog_returns_false_when_no_display(isolated_appdata, monkeypatch):
    """ヘッドレス環境(DISPLAY未設定+tkinter失敗)では False を返す."""
    # tkinter import を失敗させる
    monkeypatch.setitem(sys.modules, "tkinter", None)

    # show_consent_dialog 内の `import tkinter` が失敗 → False
    result = show_consent_dialog(customer_name="X", industry_profile="generic")
    assert result is False


# ---- 7. ConsentRecord シリアライズ ---------------------------------------

def test_consent_record_to_json_round_trip():
    rec = ConsentRecord(
        customer_name="株式会社テスト",
        industry_profile="accounting",
        upload_endpoint="",
        consented_at="2026-05-05T12:00:00+09:00",
        schema_version=1,
        user_signature="代表者サイン",
    )
    js = rec.to_json()
    parsed = json.loads(js)
    assert parsed["customer_name"] == "株式会社テスト"
    assert parsed["industry_profile"] == "accounting"
    assert parsed["user_signature"] == "代表者サイン"


# ---- 8. get_bundled_consent_html: 同意書HTMLが取得できる ----------------

def test_bundled_consent_html_path_exists():
    """リポジトリには docs/consent_form.html が存在する."""
    p = get_bundled_consent_html()
    assert p is not None
    assert p.exists()
    assert p.name == "consent_form.html"


# ---- 9. 同意ファイルのフィールド完全性 ----------------------------------

def test_consent_file_has_all_required_fields(isolated_appdata):
    record_consent(
        customer_name="A", industry_profile="hr",
        upload_endpoint="https://x", user_signature="director",
    )
    data = json.loads(consent_file_path().read_text(encoding="utf-8"))
    required_keys = {"customer_name", "industry_profile", "upload_endpoint",
                     "consented_at", "schema_version", "user_signature"}
    assert required_keys <= set(data.keys())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
