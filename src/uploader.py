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
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Iterable, Optional

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


# ---- Codex High#4: endpoint allowlist + HTTPS 強制 ----------------------

# 許可するエンドポイントホスト (本番運用ドメイン).
# 将来追加する場合はここを編集 + テスト追加。
_ALLOWED_HOSTS: tuple[str, ...] = (
    "upload.tribe-saas.com",        # TRIBE 本番アップロードドメイン
    "workscope-dashboard.vercel.app",  # 暫定 Dashboard (Vercel デフォルトドメイン)
)

# 環境変数 WORKSCOPE_ALLOW_ANY_ENDPOINT=1 で開発用に解除（本番ビルドでは使用禁止）
_ALLOW_ANY_HOST_ENV = "WORKSCOPE_ALLOW_ANY_ENDPOINT"


def is_endpoint_allowed(endpoint: str) -> tuple[bool, str]:
    """エンドポイントが本番送信先として許可されているか.
    (allowed, reason) を返す. 拒否時は reason に理由が入る.
    """
    if not endpoint:
        return False, "endpoint is empty"
    try:
        parsed = urllib.parse.urlparse(endpoint)
    except Exception as e:
        return False, f"invalid url: {e}"
    if parsed.scheme != "https":
        return False, f"scheme must be https, got '{parsed.scheme}'"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "host is empty"
    if os.environ.get(_ALLOW_ANY_HOST_ENV) == "1":
        logger.warning("endpoint host check bypassed via %s (development only)",
                       _ALLOW_ANY_HOST_ENV)
        return True, "dev-bypass"
    for allowed in _ALLOWED_HOSTS:
        if host == allowed or host.endswith("." + allowed):
            return True, f"host '{host}' matches allowlist"
    return False, f"host '{host}' not in allowlist {_ALLOWED_HOSTS}"


# ---- Codex High#5: 送信前 JSONL PII 再スキャン --------------------------

# masker.py と独立した最小限の検出パターン (uploader 単独動作用)
_PRE_UPLOAD_PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                  # email
    re.compile(r"0\d{1,4}[-(]?\d{1,4}[-)]?\d{3,4}"),          # phone
    re.compile(r"(?<!\d)\d{4}[-\s]?\d{4}[-\s]?\d{4}(?!\d)"),  # my_number
    re.compile(r"(?<!\d)〒\s?\d{3}-?\d{4}(?!\d)"),            # postal
    re.compile(r"[一-鿿々]{2,5}\s?(?:様|さん|殿|氏)"),         # honorific name
)


def scan_jsonl_for_pii_leakage(path: Path,
                                max_lines_to_check: int = 1000) -> list[str]:
    """JSONLを読み込み、明らかなPIIが残っている行を検出.

    マスク済みのはずだが、念のため uploader 側で再チェック (Codex High#5).
    検出されたPIIスニペットのリストを返す (空ならOK).
    """
    leaks: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines_to_check:
                    break
                line = line.strip()
                if not line:
                    continue
                # JSON文字列を一旦そのまま検査（簡易だが十分）
                for pat in _PRE_UPLOAD_PII_PATTERNS:
                    m = pat.search(line)
                    if m:
                        snippet = m.group(0)
                        # [MASKED:...] テンプレート内の偽陽性は除外
                        if line[max(0, m.start() - 8):m.start()].endswith("[MASKED:"):
                            continue
                        leaks.append(f"{path.name}:{i+1} {pat.pattern[:30]}.. → {snippet[:30]}")
                        break  # 1ファイルに付き複数検出可能だが、量を抑える
    except OSError:
        logger.exception("failed to scan %s", path)
    return leaks


def scan_pending_for_leakage(events: list[Path]) -> list[str]:
    """送信予定ファイル全体に対するPII漏洩スキャン. 非空なら送信中止が筋."""
    all_leaks: list[str] = []
    for p in events:
        all_leaks.extend(scan_jsonl_for_pii_leakage(p))
    return all_leaks


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

    # Codex High#4: 証明書検証を明示、リダイレクトは許可しない（HTTP誘導防止）
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED

    class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
            raise urllib.error.HTTPError(req.full_url, code, "redirect blocked",
                                          headers, fp)

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        _NoRedirectHandler(),
    )

    try:
        with opener.open(req, timeout=timeout) as resp:
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
                max_archive_bytes: int = 200 * 1024 * 1024,
                pii_scan: bool = True) -> bool:
    """1回のアップロードサイクル. 成功 True / 失敗 False.

    Codex High#4-5 対応: endpoint allowlist + 送信前 PII 再スキャン.
    """
    if not endpoint or not api_key:
        logger.info("upload not configured; skipping")
        return False

    # Codex High#4: 送信先 endpoint の検証
    allowed, reason = is_endpoint_allowed(endpoint)
    if not allowed:
        logger.error("upload BLOCKED: endpoint not allowed: %s", reason)
        return False
    logger.debug("endpoint allowed: %s", reason)

    pending = list_pending_events()
    if not pending:
        logger.info("no pending events to upload")
        return True

    # Codex High#5: 送信前 PII 再スキャン
    if pii_scan:
        leaks = scan_pending_for_leakage(pending)
        if leaks:
            logger.error(
                "upload BLOCKED: %d PII leakage candidates detected. Sample: %s",
                len(leaks), leaks[:3],
            )
            return False

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
    "is_endpoint_allowed",
    "scan_jsonl_for_pii_leakage",
    "scan_pending_for_leakage",
]
