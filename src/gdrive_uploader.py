"""WorkScope Collector v1.1-lite → Google Drive 直送アップロード機能.

設計方針 (lite 版固有):
- v1.1-lite 顧客は OCR/マスキング/スクショ生成を行わず、JSONL イベントだけを
  そのまま顧客フォルダ (Google Drive 配下) へ gzip 圧縮して直送する。
- マスキング前提が「無い」運用なので、uploader.py の PII 再スキャン
  (scan_pending_for_leakage) は実施しない。代わりにアップロード先を
  事前認証ユーザーのマイドライブ内の顧客フォルダに厳密に隔離する。
- 1日1ファイル方式: 未送信 jsonl が複数日ぶんあっても、まとめて1つの zip に
  せず、1ファイル単位で /<customer_id>/<YYYY-MM-DD>/events_<HHMMSS>.jsonl.gz
  にアップロードする (差分再送・部分復旧をシンプルに保つため)。

認証方式 (OAuth Refresh Token 方式 — v1.1-lite で SA から変更):
- サービスアカウント (SA) ではなく、OAuth User Account の Refresh Token を
  ビルド時に EXE に埋め込む。
- 理由: SA はマイドライブに quota が無く書き込めない。共有ドライブが必須だが
  Workspace 契約 (Business Standard 以上) が必要。個人 Gmail (Google One)
  では共有ドライブ作成不可。
- 対策: 髙石さん (pondering1083@gmail.com) のマイドライブ (5TB 空き) を
  使い、事前に OAuth で発行した Refresh Token を埋め込む。
- 将来: Workspace 契約後は共有ドライブ + SA 方式へ移行予定。

uploader.py との関係:
- 既存 UploadScheduler と同じインターフェース (start / stop / trigger_now /
  configured property) を持つ GDriveUploadScheduler を提供。main.py 側は
  UPLOAD_BACKEND の値で UploadScheduler / GDriveUploadScheduler を切り替える
  だけで済むように作る。
- 未送信ファイル列挙 (list_pending_events) とマーカー (_upload_marker_dir,
  _mark_uploaded) は uploader.py からそのまま import して再利用し、
  「どのファイルが送信済みか」の判定ロジックを二系統に分岐させない。

依存関係 (オプショナル):
- google-api-python-client / google-auth が未インストールの開発環境
  (Mac 等) でも import 失敗で落ちないよう、ガード付きで取り込み、
  _HAS_GDRIVE=False のとき configured は False を返して no-op 動作する。
"""

from __future__ import annotations

import base64
import binascii
import gzip
import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from google.oauth2.credentials import Credentials  # type: ignore
    from googleapiclient.discovery import build  # type: ignore
    from googleapiclient.http import MediaIoBaseUpload  # type: ignore
    from googleapiclient.errors import HttpError  # type: ignore
    _HAS_GDRIVE = True
except ImportError:  # pragma: no cover - 開発環境で google ライブラリ未導入の場合
    Credentials = None  # type: ignore
    build = None  # type: ignore
    MediaIoBaseUpload = None  # type: ignore
    HttpError = Exception  # type: ignore
    _HAS_GDRIVE = False

# uploader.py の未送信判定 / マーカー管理を再利用する。lite 版でも
# 同じ %APPDATA%/WorkScope/uploaded_markers を共有することで、後で
# uploader.py に戻すケースでも整合性を保つ。
from uploader import (  # type: ignore  # noqa: E402
    list_pending_events,
    _mark_uploaded,
    _upload_marker_dir,
)


logger = logging.getLogger(__name__)


# Drive API: drive.file スコープのみ要求 (このアプリが作成したファイルにのみアクセス可能)。
# OAuth ユーザー全体の Drive を漁る必要はないため、最小権限で運用する。
# Google が drive (full) スコープを restricted scope にしてブロックしているため
# drive.file が事実上の唯一の選択肢でもある。
_GDRIVE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive.file",
)

# OAuth Refresh Token を access token に交換するエンドポイント (Google 固定値)
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Drive 上での folder MIME type
_FOLDER_MIME = "application/vnd.google-apps.folder"

# jsonl ファイル名から日付 (YYYY-MM-DD) を抽出するパターン
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


# ---- 認証 ----------------------------------------------------------------

def _decode_oauth_credentials(b64_creds: str) -> Optional[dict]:
    """base64 エンコードされた OAuth 資格情報 JSON をデコード.

    期待する JSON 形式::

        {
            "refresh_token": "1//0abc...",
            "client_id": "xxx.apps.googleusercontent.com",
            "client_secret": "GOCSPX-..."
        }

    不正な base64 / JSON / 必須キー欠落の場合は None を返し、上位でエラーログを出す。
    """
    if not b64_creds:
        return None
    try:
        raw = base64.b64decode(b64_creds, validate=True)
    except (binascii.Error, ValueError) as e:
        logger.error("gdrive: invalid base64 oauth credentials: %s", e)
        return None
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.error("gdrive: oauth credentials is not valid JSON: %s", e)
        return None
    if not isinstance(info, dict):
        logger.error("gdrive: oauth credentials must be a JSON object")
        return None
    # 必須キーチェック
    for required_key in ("refresh_token", "client_id", "client_secret"):
        if not info.get(required_key):
            logger.error(
                "gdrive: oauth credentials missing required key: %s",
                required_key,
            )
            return None
    return info


def _build_drive_service(b64_creds: str):
    """Drive API クライアントを構築. 失敗時は None.

    OAuth Refresh Token から google.oauth2.credentials.Credentials を組み立て、
    Google Drive v3 サービスクライアントを返す。Access Token は Credentials が
    自動的にリフレッシュする (リクエスト時に lazy refresh)。
    """
    if not _HAS_GDRIVE:
        logger.warning("gdrive: google-api-python-client not installed; skipping")
        return None
    info = _decode_oauth_credentials(b64_creds)
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
        # cache_discovery=False: oauth2client cache 警告を抑制
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return service
    except Exception:
        logger.exception("gdrive: failed to build Drive service")
        return None


# ---- フォルダ操作 (find or create) --------------------------------------

def _find_or_create_folder(service, name: str, parent_id: str) -> Optional[str]:
    """parent_id 配下に name フォルダがあれば ID を返す、無ければ作って ID を返す.

    Drive API の探索は eq クエリで行い、ヒットがあれば最初の1件を採用する。
    マイドライブ運用なので supportsAllDrives は付けない (付けても無害)。
    """
    if service is None:
        return None
    # 名前に ' が含まれるとクエリが壊れるため最小限のエスケープ
    safe_name = name.replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and "
        f"mimeType = '{_FOLDER_MIME}' and "
        f"'{parent_id}' in parents and trashed = false"
    )
    try:
        resp = service.files().list(
            q=query,
            fields="files(id, name)",
            pageSize=10,
        ).execute()
        files = resp.get("files", []) or []
        if files:
            return files[0]["id"]
        # 作成
        meta = {
            "name": name,
            "mimeType": _FOLDER_MIME,
            "parents": [parent_id],
        }
        created = service.files().create(
            body=meta,
            fields="id",
        ).execute()
        return created.get("id")
    except HttpError:
        logger.exception("gdrive: folder lookup/create failed for %s", name)
        return None


# ---- アップロード本体 ---------------------------------------------------

def _gzip_bytes(src_path: Path) -> bytes:
    """jsonl を gzip 圧縮してバイト列で返す."""
    with open(src_path, "rb") as f:
        raw = f.read()
    buf = io.BytesIO()
    # mtime=0 で再現性ある圧縮 (ファイル名は filename=None で含めない)
    with gzip.GzipFile(fileobj=buf, mode="wb", filename="", mtime=0) as gz:
        gz.write(raw)
    return buf.getvalue()


def _extract_date(jsonl_name: str) -> str:
    """jsonl のファイル名から YYYY-MM-DD を抽出. 失敗時は今日の日付."""
    m = _DATE_PREFIX_RE.match(jsonl_name)
    if m:
        return m.group(1)
    return datetime.now().strftime("%Y-%m-%d")


def _upload_one(
    service,
    folder_id: str,
    customer_id: str,
    jsonl_path: Path,
    max_retry: int = 5,
) -> bool:
    """1つの jsonl を gzip 圧縮してアップロード. 成功時 True.

    アップロード経路:
      folder_id (顧客ルート) / customer_id / YYYY-MM-DD / events_HHMMSS.jsonl.gz
    """
    if service is None:
        return False

    date_str = _extract_date(jsonl_path.name)

    # 顧客フォルダ → 日付フォルダの順で取得 or 作成
    customer_folder = _find_or_create_folder(service, customer_id, folder_id)
    if customer_folder is None:
        logger.error("gdrive: could not resolve customer folder %s", customer_id)
        return False
    date_folder = _find_or_create_folder(service, date_str, customer_folder)
    if date_folder is None:
        logger.error("gdrive: could not resolve date folder %s", date_str)
        return False

    try:
        gz_bytes = _gzip_bytes(jsonl_path)
    except OSError:
        logger.exception("gdrive: failed to gzip %s", jsonl_path)
        return False

    upload_name = f"events_{datetime.now().strftime('%H%M%S')}.jsonl.gz"
    media = MediaIoBaseUpload(
        io.BytesIO(gz_bytes),
        mimetype="application/gzip",
        resumable=False,
    )
    meta = {
        "name": upload_name,
        "parents": [date_folder],
    }

    delay = 5.0
    for attempt in range(1, max_retry + 1):
        try:
            service.files().create(
                body=meta,
                media_body=media,
                fields="id, name",
            ).execute()
            logger.info(
                "gdrive: uploaded %s → /%s/%s/%s",
                jsonl_path.name, customer_id, date_str, upload_name,
            )
            return True
        except HttpError as e:
            logger.warning(
                "gdrive: upload attempt %d/%d failed for %s: %s",
                attempt, max_retry, jsonl_path.name, e,
            )
        except Exception:
            logger.exception(
                "gdrive: unexpected error on attempt %d/%d for %s",
                attempt, max_retry, jsonl_path.name,
            )
        if attempt < max_retry:
            time.sleep(delay)
            delay = min(delay * 2, 80.0)  # 5 → 10 → 20 → 40 → 80 で頭打ち
    logger.error("gdrive: upload failed after %d retries for %s",
                 max_retry, jsonl_path.name)
    return False


def upload_once_gdrive(
    folder_id: str,
    oauth_credentials_b64: str,
    customer_id: str,
    max_retry: int = 5,
) -> bool:
    """1回のアップロードサイクル. 成功時 True (送信対象0件も True).

    lite 版仕様:
      - 未送信 jsonl を 1日1ファイル単位で個別アップロード
      - PII 再スキャンは行わない (lite はマスキング前提が無い)
      - 事前認証ユーザーのマイドライブ配下に隔離されているため
        外部漏洩経路は Refresh Token 漏洩のみ → 資格情報は build 時埋め込み
    """
    if not (folder_id and oauth_credentials_b64 and customer_id):
        logger.info("gdrive: not configured; skipping")
        return False
    if not _HAS_GDRIVE:
        logger.info("gdrive: google client libraries unavailable; skipping")
        return False

    pending = list_pending_events()
    if not pending:
        logger.info("gdrive: no pending events")
        return True

    service = _build_drive_service(oauth_credentials_b64)
    if service is None:
        return False

    all_ok = True
    for p in pending:
        ok = _upload_one(
            service, folder_id, customer_id, p, max_retry=max_retry,
        )
        if ok:
            # 1ファイルごとにマーカーを書く (途中失敗時の再送ロスを最小化)
            _mark_uploaded([p])
        else:
            all_ok = False
    return all_ok


# ---- バックグラウンドスケジューラ ----------------------------------------

class GDriveUploadScheduler:
    """v1.1-lite 向け Google Drive 直送スケジューラ.

    UploadScheduler (uploader.py) と同じインターフェースを提供する。
    main.py 側で UPLOAD_BACKEND によって差し替えるだけで運用できる。
    """

    def __init__(
        self,
        folder_id: str,
        oauth_credentials_b64: str,
        customer_id: str,
        interval_minutes: int = 60,
        max_retry: int = 5,
    ) -> None:
        self._folder_id = folder_id or ""
        self._oauth_creds = oauth_credentials_b64 or ""
        self._customer_id = customer_id or ""
        # interval は秒に変換。1分未満は不適切なので 60s に底上げ
        self._interval = max(60, int(interval_minutes) * 60)
        self._max_retry = max_retry
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def configured(self) -> bool:
        """folder_id / OAuth資格情報 / customer_id が全て揃い、google ライブラリが入っているか.

        _HAS_GDRIVE=False の場合は configured も False を返し、上位は no-op 扱いにする。
        """
        return bool(
            _HAS_GDRIVE
            and self._folder_id
            and self._oauth_creds
            and self._customer_id
        )

    def start(self) -> None:
        if not self.configured:
            logger.info("GDriveUploadScheduler: not configured (no-op)")
            return
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ws-gdrive-uploader", daemon=True,
        )
        self._thread.start()
        logger.info(
            "GDriveUploadScheduler started (interval=%dmin, customer=%s)",
            self._interval // 60, self._customer_id,
        )

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def trigger_now(self) -> bool:
        """手動トリガー (タスクトレイ「今すぐ送信」など)."""
        if not self.configured:
            return False
        try:
            return upload_once_gdrive(
                self._folder_id,
                self._oauth_creds,
                self._customer_id,
                max_retry=self._max_retry,
            )
        except Exception:
            logger.exception("gdrive: trigger_now raised")
            return False

    def _run(self) -> None:
        # 起動直後は1分待機（他の起動処理と被らないように）
        if self._stop.wait(timeout=60.0):
            return
        while not self._stop.is_set():
            try:
                upload_once_gdrive(
                    self._folder_id,
                    self._oauth_creds,
                    self._customer_id,
                    max_retry=self._max_retry,
                )
            except Exception:
                logger.exception("gdrive: upload cycle raised")
            if self._stop.wait(timeout=self._interval):
                return


__all__ = [
    "GDriveUploadScheduler",
    "upload_once_gdrive",
    "_HAS_GDRIVE",
]
