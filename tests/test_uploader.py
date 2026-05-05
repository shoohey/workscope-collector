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
    """未送信ファイルが無ければ何もしないが成功扱い."""
    from uploader import upload_once  # type: ignore
    # upload_once は endpoint 通信前に pending チェックしているので、
    # endpoint に到達しないことを利用 (ここでは endpoint 設定されたが pendingが0)
    events_dir = isolated / "WorkScope" / "data" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    # JSONL がない状態で呼ぶ
    result = upload_once("https://example.com/uploads", "fake-api-key", max_retry=1)
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
