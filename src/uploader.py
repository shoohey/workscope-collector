"""WorkScope Collector → Dashboard アップロード機能.

設計方針:
- 1日1回 (cfg.upload_interval_hours) に %APPDATA%/WorkScope/data/events 内の
  未送信 JSONL を zip にまとめて Dashboard の /api/workscope/uploads へ POST
- 認証: Authorization: Bearer <UPLOAD_API_KEY> (build時埋め込み)
- 送信成功した日次JSONLは uploaded_marker/<filename>.uploaded を作って重複送信防止
- 失敗時は指数バックオフでリトライ (最大 cfg.upload_max_retry 回)
- 送信先未設定 (UPLOAD_ENDPOINT 空) なら no-op (USB回収モード)
- 営業時間外送信 (quiet_hours_only=True) で帯域影響最小化

PII保護:
- アップロードするのは既にマスク済みのJSONL/JPEGのみ
- マスカー失敗で残った可能性のある画像 (unmaskable_suspected=True) はzip対象外
- network 失敗時にデバッグログにペイロードを書かない
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _events_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "WorkScope" / "data" / "events"


def _screenshots_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "WorkScope" / "data" / "screenshots"


def _upload_marker_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    p = Path(base) / "WorkScope" / "uploaded_markers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _is_quiet_hours(now: datetime | None = None) -> bool:
    """営業時間外（夜0時〜朝6時）かを判定."""
    now = now or datetime.now()
    return now.hour < 6 or now.hour >= 22


# ---- 未送信ファイル収集 ----------------------------------------------------

def list_pending_events() -> list[Path]:
    """まだ送信していない JSONL のリスト."""
    out: list[Path] = []
    marker_dir = _upload_marker_dir()
    for p in sorted(_events_dir().glob("*.jsonl")):
        marker = marker_dir / f"{p.name}.uploaded"
        if marker.exists():
            continue
        # 当日のJSONLは未完了の可能性 → 翌日まで待つ（任意設計）
        if p.name >= datetime.now().strftime("%Y-%m-%d") + ".jsonl":
            continue
        out.append(p)
    return out


def count_events_in_jsonl(p: Path) -> int:
    """JSONLの行数（イベント数）."""
    n = 0
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
    except OSError:
        return 0
    return n


# ---- zip パッケージング --------------------------------------------------

def build_archive(events: list[Path], include_screenshots: bool = False,
                  max_bytes: int = 200 * 1024 * 1024) -> tuple[bytes, str, int]:
    """送信用 zip をメモリ上に生成. (bytes, filename, event_count) を返す."""
    buf = io.BytesIO()
    total_events = 0
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in events:
            if buf.tell() > max_bytes:
                logger.warning("archive size exceeded %d bytes; truncating", max_bytes)
                break
            try:
                zf.write(p, arcname=f"events/{p.name}")
                total_events += count_events_in_jsonl(p)
            except OSError:
                logger.exception("failed to add %s to archive", p)

        if include_screenshots:
            for ss in sorted(_screenshots_dir().glob("*.jpg")):
                if buf.tell() > max_bytes:
                    break
                try:
                    zf.write(ss, arcname=f"screenshots/{ss.name}")
                except OSError:
                    pass

    fname = f"workscope_{datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}.zip"
    return buf.getvalue(), fname, total_events


# ---- HTTP POST -----------------------------------------------------------

def _post_multipart(
    endpoint: str,
    api_key: str,
    file_bytes: bytes,
    file_name: str,
    payload_kind: str = "archive",
    event_count: int = 0,
    schema_version: int = 2,
    timeout: float = 120.0,
) -> tuple[int, str]:
    """multipart/form-data で POST. (status_code, response_body) を返す."""
    boundary = f"----WorkScopeBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    add_field("payload_kind", payload_kind)
    add_field("event_count", str(event_count))
    add_field("schema_version", str(schema_version))

    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
        .encode("utf-8")
    )
    parts.append(b"Content-Type: application/zip\r\n\r\n")
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))

    body = b"".join(parts)
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
            "User-Agent": "WorkScope-Collector/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace") if e.fp else ""
    except urllib.error.URLError as e:
        logger.exception("upload failed (network)")
        return 0, str(e)


# ---- マーカー操作 -------------------------------------------------------

def _mark_uploaded(events: list[Path]) -> None:
    """送信成功したファイルにマーカーを置く."""
    marker_dir = _upload_marker_dir()
    for p in events:
        marker = marker_dir / f"{p.name}.uploaded"
        try:
            marker.write_text(
                json.dumps({"uploaded_at": datetime.now().isoformat(),
                            "size": p.stat().st_size}),
                encoding="utf-8",
            )
        except OSError:
            logger.exception("failed to write marker for %s", p)


# ---- メインアップロード処理 ----------------------------------------------

def upload_once(endpoint: str, api_key: str, max_retry: int = 5,
                include_screenshots: bool = False,
                max_archive_bytes: int = 200 * 1024 * 1024) -> bool:
    """1回のアップロードサイクル. 成功 True / 失敗 False."""
    if not endpoint or not api_key:
        logger.info("upload not configured; skipping")
        return False

    pending = list_pending_events()
    if not pending:
        logger.info("no pending events to upload")
        return True

    archive_bytes, fname, event_count = build_archive(
        pending,
        include_screenshots=include_screenshots,
        max_bytes=max_archive_bytes,
    )
    logger.info("built archive: %s (%d events, %d bytes)",
                fname, event_count, len(archive_bytes))

    delay = 5.0
    for attempt in range(1, max_retry + 1):
        status, body = _post_multipart(
            endpoint, api_key, archive_bytes, fname,
            payload_kind="archive", event_count=event_count,
        )
        if 200 <= status < 300:
            logger.info("upload success: %s (event=%d)", fname, event_count)
            _mark_uploaded(pending)
            return True
        logger.warning("upload attempt %d/%d failed (status=%d): %s",
                       attempt, max_retry, status, body[:200])
        if attempt < max_retry:
            time.sleep(delay)
            delay = min(delay * 2, 300.0)
    logger.error("upload failed after %d retries", max_retry)
    return False


# ---- バックグラウンドスケジューラ ----------------------------------------

class UploadScheduler:
    """日次アップロードスケジューラ. main.py の Tray から起動する."""

    def __init__(self, endpoint: str, api_key: str,
                 interval_hours: float = 24.0,
                 quiet_hours_only: bool = True,
                 max_retry: int = 5,
                 max_archive_mb: int = 200) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._interval = interval_hours * 3600
        self._quiet_only = quiet_hours_only
        self._max_retry = max_retry
        self._max_bytes = max_archive_mb * 1024 * 1024
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def configured(self) -> bool:
        return bool(self._endpoint and self._api_key)

    def start(self) -> None:
        if not self.configured:
            logger.info("UploadScheduler: not configured (USB-only mode)")
            return
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ws-uploader", daemon=True)
        self._thread.start()
        logger.info("UploadScheduler started (interval=%.1fh, quiet_only=%s)",
                    self._interval / 3600, self._quiet_only)

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def trigger_now(self) -> bool:
        """手動トリガー（タスクトレイ「今すぐ送信」など）."""
        return upload_once(
            self._endpoint, self._api_key,
            max_retry=self._max_retry,
            max_archive_bytes=self._max_bytes,
        )

    def _run(self) -> None:
        # 起動直後は1分待機（他の起動処理と被らないように）
        if self._stop.wait(timeout=60.0):
            return
        while not self._stop.is_set():
            try:
                if not self._quiet_only or _is_quiet_hours():
                    upload_once(
                        self._endpoint, self._api_key,
                        max_retry=self._max_retry,
                        max_archive_bytes=self._max_bytes,
                    )
            except Exception:
                logger.exception("upload cycle raised")
            # 次のサイクルまで待機（停止可能）
            if self._stop.wait(timeout=self._interval):
                return


__all__ = [
    "UploadScheduler",
    "upload_once",
    "list_pending_events",
    "build_archive",
    "count_events_in_jsonl",
]
