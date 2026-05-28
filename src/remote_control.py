"""WorkScope Collector → リモート制御モジュール (v1.1-lite).

設計方針:
- Google Drive 上の ``{GDRIVE_FOLDER_ID}/{customer_id}/control.json`` を
  定期取得し、弊社オペレーターからの指示 (停止/再開/アンインストール/
  即時アップロード) をエッジ側に反映する。
- ポーリングは別スレッドで実施。停止は ``threading.Event`` で即時可能。
- フェイルセーフ: control.json が読めない/JSON不正/タイムスタンプ異常
  といった事象では「直前状態を維持」する。これにより通信障害や時計改竄
  攻撃でツールが暴走しないことを保証する。

control.json スキーマ::

    {
        "status": "active",                  # "active" | "paused"
        "poll_interval_minutes": 5,
        "upload_interval_minutes": 60,
        "action": null,                       # null | "uninstall" | "force_upload"
        "updated_at": "2026-05-28T15:00:00Z",
        "updated_by": "operator@tribe.example"
    }

status と action の組合せ::

    | status | action          | 動作                                 |
    | ------ | --------------- | ------------------------------------ |
    | active | null            | 通常収集 (pause_flag_file 削除)      |
    | paused | null            | 一時停止 (pause_flag_file 作成)      |
    | active | "uninstall"     | grace_period 分後に uninstall_cb 呼出 |
    | active | "force_upload"  | force_upload_callback() を即座に呼出 |

認証方式 (OAuth Refresh Token 方式 — v1.1-lite で SA から変更):
- gdrive_uploader.py と同様、SA ではなく OAuth Refresh Token を使う。
- 個人 Gmail のマイドライブで運用するため、SA だと quota の問題で
  書き込めない。Workspace 契約後は共有ドライブ + SA に戻す予定。

PII保護:
- 取得対象はオペレーション指示の control.json のみ。ユーザーデータは含まない。
- OAuth スコープは drive.file に限定 (このアプリが作成したファイルのみ)。
  control.json は事前にこのアプリ経由で作成 or 共有ドライブ上で同等のスコープ
  内で扱えること。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ---- Google API 依存 (gdrive_uploader と同じパターン) ---------------------
try:
    from google.oauth2.credentials import Credentials  # type: ignore
    from googleapiclient.discovery import build  # type: ignore
    from googleapiclient.http import MediaIoBaseDownload  # type: ignore
    _HAS_GDRIVE = True
except ImportError:  # pragma: no cover - 実機ビルド時には必ず揃っている想定
    Credentials = None  # type: ignore
    build = None  # type: ignore
    MediaIoBaseDownload = None  # type: ignore
    _HAS_GDRIVE = False


# ---- 定数 -----------------------------------------------------------------

# ステータスの許容値。これ以外は警告して "active" 扱い。
_VALID_STATUSES = ("active", "paused")

# 旧バージョン互換: pause_collection は paused と同じ扱い (後方互換)。
# ただし pause_collection は無視してよい (status="active" + action でない)
# 仕様なので action 側では受け付けない。

# poll_interval_minutes の許容範囲。範囲外は無視。
_POLL_INTERVAL_MIN = 1
_POLL_INTERVAL_MAX = 60

# updated_at が古すぎる場合のしきい値 (30日)。
_UPDATED_AT_MAX_AGE_DAYS = 30

# 起動直後のスリープ (他の起動処理と被らないように)
_INITIAL_SLEEP_SECONDS = 30.0

# Google Drive スコープ
# drive.file: このアプリが作成 or 開いたファイルだけアクセス可能。
# gdrive_uploader.py と同じスコープに揃え、書き込みも可能にしておく
# (uploader 側と OAuth セッションを共有するため、片方が読み取り専用だと
# 別資格情報が必要になり運用が煩雑になる)。
_GDRIVE_SCOPES = ("https://www.googleapis.com/auth/drive.file",)

# OAuth Refresh Token を access token に交換するエンドポイント (Google 固定値)
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


# ---- ヘルパー -------------------------------------------------------------

def _parse_updated_at(raw: Any) -> Optional[datetime]:
    """ISO8601 タイムスタンプを UTC aware datetime に変換.

    失敗時は None。Z表記、+00:00 表記の両方を受け入れる。
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _decode_oauth_credentials(creds_b64: str) -> Optional[dict]:
    """base64 エンコードされた OAuth 資格情報 JSON をデコードして dict化.

    期待する JSON 形式::

        {
            "refresh_token": "1//0abc...",
            "client_id": "xxx.apps.googleusercontent.com",
            "client_secret": "GOCSPX-..."
        }
    """
    if not creds_b64:
        return None
    try:
        info = json.loads(base64.b64decode(creds_b64).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        logger.exception("failed to decode oauth credentials")
        return None
    if not isinstance(info, dict):
        logger.error("oauth credentials must be a JSON object")
        return None
    for required_key in ("refresh_token", "client_id", "client_secret"):
        if not info.get(required_key):
            logger.error(
                "oauth credentials missing required key: %s",
                required_key,
            )
            return None
    return info


# ---- メインクラス ---------------------------------------------------------


class RemoteControlScheduler:
    """control.json を定期取得して状態を反映するスケジューラ.

    使い方::

        sched = RemoteControlScheduler(
            folder_id="<drive folder id>",
            oauth_credentials_b64="<base64 oauth creds>",
            customer_id="tribe-001",
            force_upload_callback=lambda: uploader.trigger_now(),
            uninstall_callback=lambda: uninstaller.run(),
        )
        sched.start()
        ...
        sched.stop()
    """

    def __init__(
        self,
        folder_id: str,
        oauth_credentials_b64: str,
        customer_id: str,
        poll_interval_minutes: int = 5,
        force_upload_callback: Optional[Callable[[], bool]] = None,
        uninstall_callback: Optional[Callable[[], None]] = None,
        grace_period_minutes: int = 10,
    ) -> None:
        self._folder_id = folder_id or ""
        self._oauth_credentials_b64 = oauth_credentials_b64 or ""
        self._customer_id = customer_id or ""
        self._poll_interval_minutes = self._sanitize_poll_interval(
            poll_interval_minutes, fallback=5
        )
        self._force_upload_callback = force_upload_callback
        self._uninstall_callback = uninstall_callback
        self._grace_period_minutes = max(0, int(grace_period_minutes))

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 直前に反映した状態 (フェイルセーフで「直前状態維持」する用)
        self._last_status: str = "active"
        self._last_applied_updated_at: Optional[datetime] = None

        # uninstall 猶予タイマー
        self._uninstall_timer_lock = threading.Lock()
        self._uninstall_timer: Optional[threading.Timer] = None

        # Drive サービスはスレッド開始時/呼び出し時にlazy初期化
        self._service: Any = None
        self._service_lock = threading.Lock()

    # ---- public properties -------------------------------------------------

    @property
    def configured(self) -> bool:
        """全パラメータが揃い、かつ google API ライブラリが import 可能か."""
        return bool(
            self._folder_id
            and self._oauth_credentials_b64
            and self._customer_id
            and _HAS_GDRIVE
        )

    # ---- public lifecycle --------------------------------------------------

    def start(self) -> None:
        if not self.configured:
            logger.info("RemoteControlScheduler: not configured; skipping")
            return
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ws-remote-control", daemon=True
        )
        self._thread.start()
        logger.info(
            "RemoteControlScheduler started (interval=%dmin, customer=%s)",
            self._poll_interval_minutes,
            self._customer_id,
        )

    def stop(self) -> None:
        self._stop.set()
        self._cancel_pending_uninstall(reason="scheduler stopped")
        self._thread = None

    # ---- public test hooks -------------------------------------------------

    def fetch_now(self) -> Optional[dict]:
        """control.json を Drive から取得して dict にして返す.

        失敗時は None。手動操作・テストから呼び出される。
        """
        if not self.configured:
            return None
        try:
            service = self._get_service()
            if service is None:
                return None
            file_id = self._find_control_file_id(service)
            if file_id is None:
                logger.warning(
                    "control.json not found in folder=%s/customer=%s",
                    self._folder_id, self._customer_id,
                )
                return None
            raw = self._download_file_content(service, file_id)
            if raw is None:
                return None
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                logger.warning("control.json is not a JSON object")
                return None
            return data
        except Exception:  # pragma: no cover - 防御的
            logger.exception("fetch_now failed")
            return None

    def apply_control(self, control: dict) -> None:
        """取得した control dict を解釈してエッジ側に反映.

        ここを単体でテストできるように public にしている。
        """
        if not isinstance(control, dict):
            logger.warning("apply_control: control is not dict; ignoring")
            return

        # ---- 1) updated_at 健全性チェック (時計改竄/古すぎ防止) ------------
        updated_raw = control.get("updated_at")
        updated_at = _parse_updated_at(updated_raw)
        if updated_at is None:
            logger.warning(
                "apply_control: updated_at unparsable (%r); keep previous state",
                updated_raw,
            )
            return
        now = _now_utc()
        if updated_at > now + timedelta(minutes=5):
            # +5分の許容は時計ズレ吸収用
            logger.warning(
                "apply_control: updated_at is in the future (%s > %s); "
                "possible clock tampering, keep previous state",
                updated_at, now,
            )
            return
        if updated_at < now - timedelta(days=_UPDATED_AT_MAX_AGE_DAYS):
            logger.warning(
                "apply_control: updated_at too old (%s, > %dd); keep previous state",
                updated_at, _UPDATED_AT_MAX_AGE_DAYS,
            )
            return

        # ---- 2) poll_interval_minutes の動的変更 (範囲外は維持) ------------
        new_poll = control.get("poll_interval_minutes")
        if isinstance(new_poll, int) and (
            _POLL_INTERVAL_MIN <= new_poll <= _POLL_INTERVAL_MAX
        ):
            if new_poll != self._poll_interval_minutes:
                logger.info(
                    "poll_interval_minutes: %d -> %d",
                    self._poll_interval_minutes, new_poll,
                )
                self._poll_interval_minutes = new_poll
        elif new_poll is not None:
            logger.warning(
                "poll_interval_minutes out of range (%r); keep %d",
                new_poll, self._poll_interval_minutes,
            )

        # ---- 3) status 反映 ------------------------------------------------
        status_raw = control.get("status", "active")
        if status_raw in _VALID_STATUSES:
            status = status_raw
        else:
            logger.warning(
                "status unexpected value (%r); treat as 'active'", status_raw
            )
            status = "active"

        self._apply_status(status)

        # ---- 4) action 反映 ------------------------------------------------
        action = control.get("action")
        if action == "uninstall":
            self._schedule_uninstall()
        elif action == "force_upload":
            self._invoke_force_upload()
        else:
            # action != "uninstall" になったら猶予中のアンインストールはキャンセル
            self._cancel_pending_uninstall(
                reason=f"action changed to {action!r}"
            )

        self._last_applied_updated_at = updated_at

    # ---- internal: スレッド本体 -------------------------------------------

    def _run(self) -> None:
        # 起動直後は初期化処理と被らないように待機 (停止可能)
        if self._stop.wait(timeout=_INITIAL_SLEEP_SECONDS):
            return
        while not self._stop.is_set():
            try:
                control = self.fetch_now()
                if control is None:
                    logger.warning(
                        "remote_control: fetch failed; keep previous state"
                    )
                else:
                    self.apply_control(control)
            except Exception:  # pragma: no cover - 防御的
                logger.exception("remote_control cycle raised")
            # 次サイクルまで待機 (poll_interval_minutes はサイクル中に
            # apply_control で更新されている可能性がある)
            if self._stop.wait(timeout=self._poll_interval_minutes * 60):
                return

    # ---- internal: status / action 反映 -----------------------------------

    def _apply_status(self, status: str) -> None:
        """pause_flag_file を生成/削除して collector に反映."""
        try:
            from config import pause_flag_file  # type: ignore
        except ImportError:
            logger.exception("config.pause_flag_file import failed")
            return

        flag = pause_flag_file()
        if status == "paused":
            try:
                flag.write_text(
                    datetime.now().isoformat(), encoding="utf-8"
                )
                if self._last_status != "paused":
                    logger.info("remote_control: paused")
            except OSError:
                logger.exception("failed to create pause flag")
        else:  # "active"
            if flag.exists():
                try:
                    flag.unlink()
                    if self._last_status != "active":
                        logger.info("remote_control: resumed")
                except OSError:
                    logger.exception("failed to remove pause flag")
        self._last_status = status

    def _invoke_force_upload(self) -> None:
        if self._force_upload_callback is None:
            logger.warning(
                "remote_control: force_upload requested but no callback set"
            )
            return
        try:
            logger.info("remote_control: force_upload triggered")
            self._force_upload_callback()
        except Exception:
            logger.exception("force_upload_callback raised")

    # ---- internal: uninstall 猶予タイマー ---------------------------------

    def _schedule_uninstall(self) -> None:
        if self._uninstall_callback is None:
            logger.warning(
                "remote_control: uninstall requested but no callback set"
            )
            return
        with self._uninstall_timer_lock:
            if self._uninstall_timer is not None:
                # 既にスケジュール済みなら二重登録しない
                return
            grace_seconds = self._grace_period_minutes * 60
            logger.warning(
                "remote_control: UNINSTALL scheduled in %d minute(s)",
                self._grace_period_minutes,
            )
            timer = threading.Timer(grace_seconds, self._run_uninstall)
            timer.daemon = True
            timer.name = "ws-uninstall-grace"
            self._uninstall_timer = timer
            timer.start()

    def _cancel_pending_uninstall(self, reason: str = "") -> None:
        with self._uninstall_timer_lock:
            timer = self._uninstall_timer
            if timer is None:
                return
            timer.cancel()
            self._uninstall_timer = None
        logger.info("remote_control: uninstall canceled (%s)", reason)

    def _run_uninstall(self) -> None:
        with self._uninstall_timer_lock:
            self._uninstall_timer = None
        if self._uninstall_callback is None:
            return
        try:
            logger.warning("remote_control: executing uninstall_callback")
            self._uninstall_callback()
        except Exception:
            logger.exception("uninstall_callback raised")

    # ---- internal: Google Drive --------------------------------------------

    def _get_service(self) -> Any:
        """Drive サービスを lazy 初期化して返す.

        OAuth Refresh Token から Credentials を組み立て、Drive v3 クライアントを
        返す。Access Token は Credentials が自動的にリフレッシュする。
        """
        with self._service_lock:
            if self._service is not None:
                return self._service
            if not _HAS_GDRIVE:
                return None
            info = _decode_oauth_credentials(self._oauth_credentials_b64)
            if info is None:
                return None
            try:
                creds = Credentials(
                    token=None,
                    refresh_token=info["refresh_token"],
                    client_id=info["client_id"],
                    client_secret=info["client_secret"],
                    token_uri=_GOOGLE_TOKEN_URI,
                    scopes=list(_GDRIVE_SCOPES),
                )
                # cache_discovery=False: ファイルキャッシュ警告抑止
                self._service = build(
                    "drive", "v3", credentials=creds, cache_discovery=False,
                )
                return self._service
            except Exception:
                logger.exception("failed to build Drive service")
                return None

    def _find_control_file_id(self, service: Any) -> Optional[str]:
        """folder_id 配下の customer_id サブフォルダにある control.json のIDを取得."""
        # 1) customer_id サブフォルダを探す
        sub_query = (
            f"'{self._folder_id}' in parents and name = '{self._customer_id}' "
            "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        try:
            sub_resp = service.files().list(
                q=sub_query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=10,
            ).execute()
        except Exception:
            logger.exception("Drive files.list (subfolder) failed")
            return None
        sub_files = sub_resp.get("files", []) if isinstance(sub_resp, dict) else []
        if not sub_files:
            logger.warning(
                "customer subfolder not found (folder=%s, customer=%s)",
                self._folder_id, self._customer_id,
            )
            return None
        subfolder_id = sub_files[0].get("id")
        if not subfolder_id:
            return None

        # 2) control.json を探す
        ctrl_query = (
            f"'{subfolder_id}' in parents and name = 'control.json' "
            "and trashed = false"
        )
        try:
            ctrl_resp = service.files().list(
                q=ctrl_query,
                spaces="drive",
                fields="files(id, name, modifiedTime)",
                pageSize=10,
            ).execute()
        except Exception:
            logger.exception("Drive files.list (control.json) failed")
            return None
        ctrl_files = ctrl_resp.get("files", []) if isinstance(ctrl_resp, dict) else []
        if not ctrl_files:
            return None
        return ctrl_files[0].get("id")

    def _download_file_content(self, service: Any, file_id: str) -> Optional[bytes]:
        try:
            request = service.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            if MediaIoBaseDownload is not None:
                downloader = MediaIoBaseDownload(buf, request)
                done = False
                while not done:
                    _status, done = downloader.next_chunk()
            else:  # pragma: no cover - 通常通らない
                buf.write(request.execute())
            return buf.getvalue()
        except Exception:
            logger.exception("Drive get_media failed")
            return None

    # ---- internal: utility -------------------------------------------------

    @staticmethod
    def _sanitize_poll_interval(value: Any, fallback: int) -> int:
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            return fallback
        if _POLL_INTERVAL_MIN <= ivalue <= _POLL_INTERVAL_MAX:
            return ivalue
        return fallback


__all__ = ["RemoteControlScheduler"]
