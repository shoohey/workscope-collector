"""gdrive_uploader のテスト.

設計方針:
- google-api-python-client / google-auth は開発環境 (Mac) で必ずしも
  インストールされていないので、すべて unittest.mock で差し替える。
- 既存 tests/test_uploader.py の "isolated APPDATA" パターン (APPDATA を
  tmp に向けて sys.modules から uploader/config を取り除く) を踏襲。
- gdrive_uploader は uploader.list_pending_events / _mark_uploaded を
  再利用するので、isolated fixture は uploader モジュールにも作用させる。
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _isolate(tmp: Path) -> None:
    os.environ["APPDATA"] = str(tmp)
    # uploader / config / gdrive_uploader をフレッシュロードするためキャッシュ削除
    for m in ("gdrive_uploader", "uploader", "config"):
        sys.modules.pop(m, None)


@pytest.fixture()
def isolated(tmp_path):
    _isolate(tmp_path)
    yield tmp_path


def _make_jsonl(isolated_dir: Path, name: str, body: str = '{"event_seq":1}\n') -> Path:
    events_dir = isolated_dir / "WorkScope" / "data" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    p = events_dir / name
    p.write_text(body, encoding="utf-8")
    return p


def _valid_sa_key_b64() -> str:
    """テスト用のダミーサービスアカウント JSON (base64)."""
    payload = {
        "type": "service_account",
        "project_id": "dummy",
        "private_key_id": "x",
        "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
        "client_email": "dummy@dummy.iam.gserviceaccount.com",
        "client_id": "0",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


# =============================================================================
# 1. configured property: 設定の有無で True/False が切り替わる
# =============================================================================

def test_configured_false_when_all_empty(isolated):
    import gdrive_uploader  # type: ignore
    sched = gdrive_uploader.GDriveUploadScheduler(
        folder_id="", service_account_key_b64="", customer_id="",
    )
    assert sched.configured is False


def test_configured_false_when_folder_id_missing(isolated):
    import gdrive_uploader  # type: ignore
    sched = gdrive_uploader.GDriveUploadScheduler(
        folder_id="", service_account_key_b64=_valid_sa_key_b64(),
        customer_id="tribe-001",
    )
    assert sched.configured is False


def test_configured_false_when_sa_key_missing(isolated):
    import gdrive_uploader  # type: ignore
    sched = gdrive_uploader.GDriveUploadScheduler(
        folder_id="folder123", service_account_key_b64="",
        customer_id="tribe-001",
    )
    assert sched.configured is False


def test_configured_false_when_customer_id_missing(isolated):
    import gdrive_uploader  # type: ignore
    sched = gdrive_uploader.GDriveUploadScheduler(
        folder_id="folder123", service_account_key_b64=_valid_sa_key_b64(),
        customer_id="",
    )
    assert sched.configured is False


def test_configured_true_when_all_present_and_gdrive_available(isolated):
    import gdrive_uploader  # type: ignore
    sched = gdrive_uploader.GDriveUploadScheduler(
        folder_id="folder123",
        service_account_key_b64=_valid_sa_key_b64(),
        customer_id="tribe-001",
    )
    # _HAS_GDRIVE を強制的に True 化 (テスト環境では未インストール想定)
    with patch.object(gdrive_uploader, "_HAS_GDRIVE", True):
        assert sched.configured is True


# =============================================================================
# 2. upload_once_gdrive: Drive API が正しい引数で呼ばれる
# =============================================================================

def _install_fake_gdrive(monkeypatch, gdrive_uploader_mod) -> dict:
    """gdrive_uploader 内の google API 依存をモックに差し替える.

    返り値: {"service": MagicMock, "build": MagicMock, "creds": MagicMock,
            "create_calls": list[dict]} など、テストから呼び出し履歴を読み取れる構造。
    """
    fake_service = MagicMock(name="drive_service")

    # files().list().execute() → 空 (フォルダ未存在 → create が走る)
    list_chain = MagicMock()
    list_chain.execute.return_value = {"files": []}
    fake_service.files.return_value.list.return_value = list_chain

    # files().create().execute() → {"id": "<新フォルダID or ファイルID>"}
    create_results = []

    def _create_side_effect(*, body, fields, supportsAllDrives=False, media_body=None):
        # 呼び出しごとに ID をユニークに発行
        new_id = f"id-{len(create_results)+1}"
        create_results.append({
            "body": body,
            "fields": fields,
            "supportsAllDrives": supportsAllDrives,
            "media_body": media_body,
            "returned_id": new_id,
        })
        chain = MagicMock()
        chain.execute.return_value = {"id": new_id, "name": body.get("name")}
        return chain

    fake_service.files.return_value.create.side_effect = _create_side_effect

    fake_creds = MagicMock(name="credentials")
    fake_sa_mod = MagicMock()
    fake_sa_mod.Credentials.from_service_account_info.return_value = fake_creds

    fake_build = MagicMock(return_value=fake_service)

    monkeypatch.setattr(gdrive_uploader_mod, "_HAS_GDRIVE", True)
    monkeypatch.setattr(gdrive_uploader_mod, "service_account", fake_sa_mod)
    monkeypatch.setattr(gdrive_uploader_mod, "build", fake_build)
    # MediaIoBaseUpload は引数記録だけできれば十分
    monkeypatch.setattr(
        gdrive_uploader_mod,
        "MediaIoBaseUpload",
        MagicMock(side_effect=lambda buf, mimetype=None, resumable=False: ("media", mimetype)),
    )
    # HttpError は本物に近い例外型としてカスタムクラスに差し替え
    class _HE(Exception):
        pass
    monkeypatch.setattr(gdrive_uploader_mod, "HttpError", _HE)

    return {
        "service": fake_service,
        "build": fake_build,
        "creds": fake_creds,
        "sa_mod": fake_sa_mod,
        "create_calls": create_results,
        "HttpError": _HE,
    }


def test_upload_once_calls_drive_create_with_correct_args(isolated, monkeypatch):
    import gdrive_uploader  # type: ignore

    _make_jsonl(isolated, "2026-04-30.jsonl",
                body=json.dumps({"event_seq": 1}) + "\n")
    fakes = _install_fake_gdrive(monkeypatch, gdrive_uploader)

    ok = gdrive_uploader.upload_once_gdrive(
        folder_id="root-folder",
        sa_key_b64=_valid_sa_key_b64(),
        customer_id="tribe-001",
        max_retry=1,
    )
    assert ok is True

    # 1. SA キーが service_account.Credentials.from_service_account_info に
    #    JSON 化された辞書として渡されたこと
    fakes["sa_mod"].Credentials.from_service_account_info.assert_called_once()
    args, kwargs = fakes["sa_mod"].Credentials.from_service_account_info.call_args
    info_arg = args[0]
    assert isinstance(info_arg, dict)
    assert info_arg.get("type") == "service_account"
    assert "scopes" in kwargs

    # 2. build("drive", "v3", credentials=...) が呼ばれていること
    fakes["build"].assert_called_once()
    bargs, bkwargs = fakes["build"].call_args
    assert bargs[0] == "drive"
    assert bargs[1] == "v3"
    assert bkwargs.get("credentials") is fakes["creds"]

    # 3. create が 3 回呼ばれていること (customer フォルダ, 日付フォルダ, ファイル本体)
    assert len(fakes["create_calls"]) == 3
    # 顧客フォルダ
    assert fakes["create_calls"][0]["body"]["name"] == "tribe-001"
    assert fakes["create_calls"][0]["body"]["parents"] == ["root-folder"]
    # 日付フォルダ
    assert fakes["create_calls"][1]["body"]["name"] == "2026-04-30"
    assert fakes["create_calls"][1]["body"]["parents"] == ["id-1"]
    # ファイル本体
    file_call = fakes["create_calls"][2]
    assert file_call["body"]["name"].startswith("events_")
    assert file_call["body"]["name"].endswith(".jsonl.gz")
    assert file_call["body"]["parents"] == ["id-2"]
    assert file_call["media_body"] is not None
    # 共有ドライブ対応フラグ
    assert all(c["supportsAllDrives"] is True for c in fakes["create_calls"])


# =============================================================================
# 3. リトライが指数バックオフで最大 5 回まで実行される
# =============================================================================

def test_upload_retries_with_exponential_backoff(isolated, monkeypatch):
    import gdrive_uploader  # type: ignore

    _make_jsonl(isolated, "2026-04-30.jsonl")
    fakes = _install_fake_gdrive(monkeypatch, gdrive_uploader)

    # フォルダの create は成功させ、ファイル本体の create のみ全失敗にする。
    # _create_side_effect を上書き: body["mimeType"] が無いものをファイルとみなす。
    HttpError = fakes["HttpError"]
    file_attempts = {"count": 0}

    def _failing_create(*, body, fields, supportsAllDrives=False, media_body=None):
        chain = MagicMock()
        # フォルダ作成は成功
        if body.get("mimeType") == "application/vnd.google-apps.folder":
            new_id = f"folder-{len(fakes['create_calls'])+1}"
            fakes["create_calls"].append({
                "body": body, "fields": fields,
                "supportsAllDrives": supportsAllDrives,
                "media_body": media_body, "returned_id": new_id,
            })
            chain.execute.return_value = {"id": new_id, "name": body.get("name")}
            return chain
        # ファイル本体: 常に失敗
        file_attempts["count"] += 1
        fakes["create_calls"].append({
            "body": body, "fields": fields,
            "supportsAllDrives": supportsAllDrives,
            "media_body": media_body, "returned_id": None,
        })
        def _raise():
            raise HttpError("upload boom")
        chain.execute.side_effect = _raise
        return chain

    fakes["service"].files.return_value.create.side_effect = _failing_create

    # time.sleep をモックしてリトライ遅延の引数を記録
    sleep_args: list[float] = []
    monkeypatch.setattr(gdrive_uploader.time, "sleep",
                        lambda s: sleep_args.append(s))

    ok = gdrive_uploader.upload_once_gdrive(
        folder_id="root-folder",
        sa_key_b64=_valid_sa_key_b64(),
        customer_id="tribe-001",
        max_retry=5,
    )
    assert ok is False
    # ファイル create が 5 回呼ばれた
    assert file_attempts["count"] == 5
    # 指数バックオフ: 5, 10, 20, 40 (最後の試行後は待たない → 4 回)
    assert sleep_args == [5.0, 10.0, 20.0, 40.0]


# =============================================================================
# 4. 成功時にマーカーが作成される
# =============================================================================

def test_marker_created_on_success(isolated, monkeypatch):
    import gdrive_uploader  # type: ignore

    _make_jsonl(isolated, "2026-04-30.jsonl")
    _install_fake_gdrive(monkeypatch, gdrive_uploader)

    ok = gdrive_uploader.upload_once_gdrive(
        folder_id="root-folder",
        sa_key_b64=_valid_sa_key_b64(),
        customer_id="tribe-001",
        max_retry=1,
    )
    assert ok is True

    marker = isolated / "WorkScope" / "uploaded_markers" / "2026-04-30.jsonl.uploaded"
    assert marker.exists()
    body = json.loads(marker.read_text(encoding="utf-8"))
    assert "uploaded_at" in body


# =============================================================================
# 5. 既にマーカーがある jsonl はスキップされる
# =============================================================================

def test_already_uploaded_files_are_skipped(isolated, monkeypatch):
    import gdrive_uploader  # type: ignore

    _make_jsonl(isolated, "2026-04-30.jsonl")
    # マーカーを事前作成
    marker_dir = isolated / "WorkScope" / "uploaded_markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "2026-04-30.jsonl.uploaded").write_text("{}", encoding="utf-8")

    fakes = _install_fake_gdrive(monkeypatch, gdrive_uploader)

    ok = gdrive_uploader.upload_once_gdrive(
        folder_id="root-folder",
        sa_key_b64=_valid_sa_key_b64(),
        customer_id="tribe-001",
        max_retry=1,
    )
    # 未送信が無いので成功扱い、create は一切呼ばれない
    assert ok is True
    assert len(fakes["create_calls"]) == 0
    # build() も pending が無い段階で early return するため呼ばれない
    fakes["build"].assert_not_called()


# =============================================================================
# 6. base64 キー不正時はエラーログを出して False を返す
# =============================================================================

def test_invalid_base64_returns_false(isolated, monkeypatch, caplog):
    import gdrive_uploader  # type: ignore

    _make_jsonl(isolated, "2026-04-30.jsonl")
    _install_fake_gdrive(monkeypatch, gdrive_uploader)

    with caplog.at_level("ERROR"):
        ok = gdrive_uploader.upload_once_gdrive(
            folder_id="root-folder",
            sa_key_b64="this-is-not-valid-base64-!!!!",
            customer_id="tribe-001",
            max_retry=1,
        )
    assert ok is False
    assert any("base64" in rec.message.lower() for rec in caplog.records)


def test_valid_base64_but_non_json_returns_false(isolated, monkeypatch, caplog):
    """base64 デコードは通るが中身が JSON でないケース."""
    import gdrive_uploader  # type: ignore

    _make_jsonl(isolated, "2026-04-30.jsonl")
    _install_fake_gdrive(monkeypatch, gdrive_uploader)

    bad = base64.b64encode(b"not a json at all").decode("ascii")
    with caplog.at_level("ERROR"):
        ok = gdrive_uploader.upload_once_gdrive(
            folder_id="root-folder",
            sa_key_b64=bad,
            customer_id="tribe-001",
            max_retry=1,
        )
    assert ok is False
    assert any("json" in rec.message.lower() for rec in caplog.records)


# =============================================================================
# 7. _HAS_GDRIVE=False のとき start/trigger_now が安全に no-op
# =============================================================================

def test_no_op_when_gdrive_lib_missing(isolated, monkeypatch):
    import gdrive_uploader  # type: ignore

    # _HAS_GDRIVE を強制 False に
    monkeypatch.setattr(gdrive_uploader, "_HAS_GDRIVE", False)

    sched = gdrive_uploader.GDriveUploadScheduler(
        folder_id="root-folder",
        service_account_key_b64=_valid_sa_key_b64(),
        customer_id="tribe-001",
    )
    # configured は False
    assert sched.configured is False
    # start は no-op (スレッド作成しない)
    sched.start()
    assert sched._thread is None
    # trigger_now は False を返す (例外を投げない)
    assert sched.trigger_now() is False
    # stop も例外を出さない
    sched.stop()


def test_upload_once_returns_false_when_gdrive_lib_missing(isolated, monkeypatch):
    import gdrive_uploader  # type: ignore

    _make_jsonl(isolated, "2026-04-30.jsonl")
    monkeypatch.setattr(gdrive_uploader, "_HAS_GDRIVE", False)

    ok = gdrive_uploader.upload_once_gdrive(
        folder_id="root-folder",
        sa_key_b64=_valid_sa_key_b64(),
        customer_id="tribe-001",
        max_retry=1,
    )
    assert ok is False


# =============================================================================
# 補足: 設定不足時 (folder/customer/key 欠落) は upload_once も False
# =============================================================================

def test_upload_once_returns_false_when_not_configured(isolated):
    import gdrive_uploader  # type: ignore
    ok = gdrive_uploader.upload_once_gdrive(
        folder_id="", sa_key_b64="", customer_id="", max_retry=1,
    )
    assert ok is False


# =============================================================================
# 補足: pending が無い場合は build せず True
# =============================================================================

def test_upload_once_no_pending_is_success(isolated, monkeypatch):
    import gdrive_uploader  # type: ignore

    # events_dir は空のまま
    events_dir = isolated / "WorkScope" / "data" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    fakes = _install_fake_gdrive(monkeypatch, gdrive_uploader)
    ok = gdrive_uploader.upload_once_gdrive(
        folder_id="root-folder",
        sa_key_b64=_valid_sa_key_b64(),
        customer_id="tribe-001",
        max_retry=1,
    )
    assert ok is True
    # pending が空なら API 呼び出しは一切走らない
    fakes["build"].assert_not_called()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
