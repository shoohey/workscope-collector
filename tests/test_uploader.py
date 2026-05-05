"""uploader のテスト. アーカイブ生成・マーカー管理・スケジューラ振る舞い."""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _isolate(tmp: Path) -> None:
    os.environ["APPDATA"] = str(tmp)
    for m in ("uploader", "config"):
        sys.modules.pop(m, None)


@pytest.fixture()
def isolated(tmp_path):
    _isolate(tmp_path)
    yield tmp_path


# ---- 1. list_pending_events: マーカーのないJSONLを返す ------------------

def test_list_pending_events_returns_only_unuploaded(isolated):
    from uploader import list_pending_events  # type: ignore

    events_dir = isolated / "WorkScope" / "data" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "2026-04-30.jsonl").write_text("line\n", encoding="utf-8")
    (events_dir / "2026-05-01.jsonl").write_text("line\n", encoding="utf-8")

    # 1つにマーカーを置く
    marker_dir = isolated / "WorkScope" / "uploaded_markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "2026-04-30.jsonl.uploaded").write_text("{}", encoding="utf-8")

    pending = list_pending_events()
    assert len(pending) == 1
    assert pending[0].name == "2026-05-01.jsonl"


def test_list_pending_excludes_today(isolated):
    """当日のJSONLは送信対象外 (まだ書き込み中の可能性)."""
    from uploader import list_pending_events  # type: ignore
    from datetime import datetime

    events_dir = isolated / "WorkScope" / "data" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    (events_dir / f"{today}.jsonl").write_text("line\n", encoding="utf-8")

    pending = list_pending_events()
    assert len(pending) == 0


# ---- 2. count_events_in_jsonl ----------------------------------------

def test_count_events_in_jsonl(isolated):
    from uploader import count_events_in_jsonl  # type: ignore
    p = isolated / "x.jsonl"
    p.write_text("a\nb\n\nc\n", encoding="utf-8")
    assert count_events_in_jsonl(p) == 3


# ---- 3. build_archive: zipに events/ が入る --------------------------

def test_build_archive_includes_events(isolated):
    from uploader import build_archive  # type: ignore

    events_dir = isolated / "WorkScope" / "data" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    p = events_dir / "2026-04-30.jsonl"
    p.write_text(json.dumps({"event_seq": 1}) + "\n", encoding="utf-8")

    archive_bytes, fname, event_count = build_archive([p], include_screenshots=False)
    assert event_count == 1
    assert fname.startswith("workscope_") and fname.endswith(".zip")

    # zip 検査
    zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
    names = zf.namelist()
    assert "events/2026-04-30.jsonl" in names


def test_build_archive_respects_max_bytes(isolated):
    """max_bytes 超過時に追加ファイルがスキップされる. ランダムバイトで圧縮率を抑制."""
    import secrets

    from uploader import build_archive  # type: ignore

    events_dir = isolated / "WorkScope" / "data" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    # 各ファイル 200KB の非圧縮性高いランダム16進文字列
    paths = []
    for i in range(5):
        p = events_dir / f"2026-04-{i:02d}.jsonl"
        p.write_text(secrets.token_hex(100_000), encoding="utf-8")
        paths.append(p)

    archive_bytes, fname, _ = build_archive(paths, max_bytes=300_000)
    # 300KB制限なので5ファイル(計1MB)全部は入らない
    zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
    assert len(zf.namelist()) < 5


# ---- 4. upload_once: 未設定なら False ------------------------------

def test_upload_once_returns_false_when_not_configured(isolated):
    from uploader import upload_once  # type: ignore
    assert upload_once("", "") is False


def test_upload_once_no_pending_returns_true(isolated):
    """未送信ファイルが無ければ何もしないが成功扱い (allowlist済みホスト使用)."""
    from uploader import upload_once  # type: ignore
    events_dir = isolated / "WorkScope" / "data" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    # allowlist 通過するホストを使用
    result = upload_once(
        "https://upload.tribe-saas.com/customers/test/",
        "fake-api-key", max_retry=1,
    )
    assert result is True


# ---- 5. upload scheduler: 未設定状態 -----------------------------

def test_scheduler_not_configured_does_not_start(isolated):
    from uploader import UploadScheduler  # type: ignore

    sched = UploadScheduler(endpoint="", api_key="")
    assert sched.configured is False
    sched.start()  # no-op
    assert sched._thread is None


def test_scheduler_configured_state(isolated):
    from uploader import UploadScheduler  # type: ignore

    sched = UploadScheduler(endpoint="https://x", api_key="k")
    assert sched.configured is True


# ---- 6. PII 漏洩テスト: アーカイブには事前にマスク済みデータしか入らない ----

def test_archive_only_contains_listed_files(isolated):
    """build_archive で指定したファイルのみが zip に入り、それ以外は混入しない."""
    from uploader import build_archive  # type: ignore

    events_dir = isolated / "WorkScope" / "data" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    listed = events_dir / "2026-04-30.jsonl"
    listed.write_text("event\n", encoding="utf-8")
    not_listed = events_dir / "2026-05-99.jsonl"
    not_listed.write_text("DO_NOT_INCLUDE\n", encoding="utf-8")

    archive_bytes, _, _ = build_archive([listed])
    zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
    contents = zf.read("events/2026-04-30.jsonl").decode("utf-8")
    assert "DO_NOT_INCLUDE" not in contents
    # not_listed のファイル名が zip 内に存在しない
    assert not any("2026-05-99" in n for n in zf.namelist())


# ============================================================================
# Codex High#4: endpoint allowlist
# ============================================================================

def test_endpoint_allowlist_accepts_tribe_domain(isolated):
    from uploader import is_endpoint_allowed  # type: ignore
    ok, _ = is_endpoint_allowed("https://upload.tribe-saas.com/customers/x/")
    assert ok is True


def test_endpoint_allowlist_accepts_dashboard(isolated):
    from uploader import is_endpoint_allowed  # type: ignore
    ok, _ = is_endpoint_allowed("https://workscope-dashboard.vercel.app/api/workscope/uploads")
    assert ok is True


def test_endpoint_allowlist_rejects_unknown_host(isolated):
    from uploader import is_endpoint_allowed  # type: ignore
    ok, reason = is_endpoint_allowed("https://evil.example.com/upload")
    assert ok is False
    assert "allowlist" in reason


def test_endpoint_allowlist_rejects_http(isolated):
    from uploader import is_endpoint_allowed  # type: ignore
    ok, reason = is_endpoint_allowed("http://upload.tribe-saas.com/upload")
    assert ok is False
    assert "https" in reason.lower()


def test_endpoint_allowlist_rejects_localhost(isolated):
    """ローカル/メタデータ系URLへの誤送信を防ぐ."""
    from uploader import is_endpoint_allowed  # type: ignore
    for url in ("https://localhost/x", "https://127.0.0.1/x",
                "https://169.254.169.254/latest/meta-data/", "https://metadata.google.internal/x"):
        ok, _ = is_endpoint_allowed(url)
        assert ok is False, f"should reject {url}"


def test_endpoint_allowlist_dev_bypass(isolated, monkeypatch):
    from uploader import is_endpoint_allowed  # type: ignore
    monkeypatch.setenv("WORKSCOPE_ALLOW_ANY_ENDPOINT", "1")
    ok, reason = is_endpoint_allowed("https://example.com/x")
    assert ok is True
    assert "dev-bypass" in reason


def test_upload_once_blocks_disallowed_endpoint(isolated, tmp_path):
    """allowlist外のエンドポイントには絶対送信しない (PII漏洩防止の最終ゲート)."""
    from uploader import upload_once  # type: ignore

    events_dir = isolated / "WorkScope" / "data" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "2026-04-30.jsonl").write_text('{"event_seq":1}\n', encoding="utf-8")

    result = upload_once("https://attacker.example/x", "key", max_retry=1)
    assert result is False


# ============================================================================
# Codex High#5: 送信前 PII 再スキャン
# ============================================================================

def test_scan_jsonl_detects_email(isolated):
    from uploader import scan_jsonl_for_pii_leakage  # type: ignore
    p = isolated / "events.jsonl"
    p.write_text('{"text": "send to user@example.com"}\n', encoding="utf-8")
    leaks = scan_jsonl_for_pii_leakage(p)
    assert len(leaks) >= 1


def test_scan_jsonl_detects_phone(isolated):
    from uploader import scan_jsonl_for_pii_leakage  # type: ignore
    p = isolated / "events.jsonl"
    p.write_text('{"text": "tel: 090-1234-5678"}\n', encoding="utf-8")
    leaks = scan_jsonl_for_pii_leakage(p)
    assert len(leaks) >= 1


def test_scan_jsonl_detects_my_number(isolated):
    from uploader import scan_jsonl_for_pii_leakage  # type: ignore
    p = isolated / "events.jsonl"
    p.write_text('{"x": "1234-5678-9012"}\n', encoding="utf-8")
    leaks = scan_jsonl_for_pii_leakage(p)
    assert len(leaks) >= 1


def test_scan_jsonl_ignores_already_masked(isolated):
    """[MASKED:email] のような正規マスク後の表現は誤検出しない."""
    from uploader import scan_jsonl_for_pii_leakage  # type: ignore
    p = isolated / "events.jsonl"
    p.write_text('{"text": "[MASKED:email]"}\n', encoding="utf-8")
    leaks = scan_jsonl_for_pii_leakage(p)
    assert leaks == []


def test_scan_jsonl_clean_returns_empty(isolated):
    from uploader import scan_jsonl_for_pii_leakage  # type: ignore
    p = isolated / "events.jsonl"
    p.write_text('{"window": "処方入力", "ts": "2026-05-05T10:00:00Z"}\n', encoding="utf-8")
    leaks = scan_jsonl_for_pii_leakage(p)
    assert leaks == []


def test_upload_once_blocks_when_pii_detected(isolated):
    """送信予定ファイルにPII残存が検出されたら送信中止."""
    from uploader import upload_once  # type: ignore

    events_dir = isolated / "WorkScope" / "data" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "2026-04-30.jsonl").write_text(
        '{"text":"漏れたメール user@example.com"}\n',
        encoding="utf-8",
    )
    result = upload_once(
        "https://upload.tribe-saas.com/customers/x/",
        "key", max_retry=1,
    )
    # PIIが検出されたので送信されない
    assert result is False


def test_upload_once_proceeds_when_pii_scan_disabled(isolated, monkeypatch):
    """pii_scan=False で disabling した場合は通る (テスト用、本番ではFalseにしない)."""
    from uploader import upload_once  # type: ignore

    events_dir = isolated / "WorkScope" / "data" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    # 空ファイル（実通信は発生しないがpii_scan=Falseが効くか確認）
    result = upload_once(
        "https://upload.tribe-saas.com/customers/x/",
        "key", max_retry=1, pii_scan=False,
    )
    # pending無しなのでTrue
    assert result is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
