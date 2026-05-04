"""イベント JSONL とマスク済み画像の永続化レイヤ.

- ``EventStore``: 日次ローテーション JSONL ライター
- ``ScreenshotStore``: マスク済み画像 (JPEG) の保存
- ``cleanup_old_data``: 保持日数を超えたファイルを削除
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:  # PIL は Mac でも入る想定
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover - 防御的
    Image = None  # type: ignore

from config import (
    CollectorConfig,
    events_dir,
    screenshots_dir,
)


logger = logging.getLogger(__name__)


# ---- 時刻ユーティリティ ----------------------------------------------------

def _local_now() -> datetime:
    """ローカルタイムゾーンの aware datetime."""
    return datetime.now(timezone.utc).astimezone()


def _today_str(dt: datetime | None = None) -> str:
    return (dt or _local_now()).strftime("%Y-%m-%d")


def iso_ts(dt: datetime | None = None) -> str:
    """ミリ秒精度の ISO8601（オフセット付き）."""
    dt = dt or _local_now()
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond // 1000:03d}" + dt.strftime("%z")


def filename_ts(dt: datetime | None = None) -> str:
    """ファイル名で使える時刻表現 ``YYYY-MM-DDTHH-MM-SS-mmm``."""
    dt = dt or _local_now()
    return dt.strftime("%Y-%m-%dT%H-%M-%S") + f"-{dt.microsecond // 1000:03d}"


# ---- EventStore -----------------------------------------------------------

class EventStore:
    """日次ローテーション JSONL ライター.

    スレッドセーフ。書き込みごとに fsync は行わないが、行単位で flush する。
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._dir = Path(base_dir) if base_dir else events_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._current_day: str | None = None
        self._fp: io.TextIOWrapper | None = None

    # --- 内部 ---------------------------------------------------------------
    def _path_for(self, day: str) -> Path:
        return self._dir / f"{day}.jsonl"

    def _ensure_open(self, day: str) -> None:
        if self._current_day == day and self._fp is not None:
            return
        self._close()
        self._fp = open(self._path_for(day), "a", encoding="utf-8", buffering=1)
        self._current_day = day

    def _close(self) -> None:
        if self._fp is not None:
            try:
                self._fp.flush()
                self._fp.close()
            except Exception:
                logger.exception("EventStore close failed")
            finally:
                self._fp = None
                self._current_day = None

    # --- 公開 API -----------------------------------------------------------
    def append(self, event: dict[str, Any]) -> Path:
        """イベントを 1 行追記し、書き込み先のパスを返す."""
        ts = event.get("ts")
        if isinstance(ts, str) and len(ts) >= 10:
            day = ts[:10]
        else:
            day = _today_str()
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._ensure_open(day)
            assert self._fp is not None
            self._fp.write(line + "\n")
            self._fp.flush()
            return self._path_for(day)

    def close(self) -> None:
        with self._lock:
            self._close()

    # --- 読み出し（タスクトレイ統計用） -------------------------------------
    def read_day(self, day: str | None = None) -> list[dict[str, Any]]:
        """指定日の JSONL を全件読み込む（壊れた行は無視）."""
        day = day or _today_str()
        p = self._path_for(day)
        out: list[dict[str, Any]] = []
        if not p.exists():
            return out
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Skip broken JSONL line in %s", p)
        except OSError:
            logger.exception("read_day failed: %s", p)
        return out


# ---- ScreenshotStore ------------------------------------------------------

@dataclass
class SavedScreenshot:
    """保存結果."""
    filename: str
    path: Path
    width: int
    height: int
    bytes_written: int


class ScreenshotStore:
    """マスク済み画像 (JPEG) の保存."""

    def __init__(self, base_dir: Path | None = None, jpeg_quality: int = 70) -> None:
        self._dir = Path(base_dir) if base_dir else screenshots_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._jpeg_quality = int(jpeg_quality)

    def save(self, image: "Image.Image", session_id: str | None = None) -> SavedScreenshot:
        """PIL.Image を JPEG で保存して結果を返す."""
        if Image is None:
            raise RuntimeError("Pillow is not available")
        ts = filename_ts()
        # short hash: ファイル名衝突回避＋トレーサビリティ
        seed = f"{ts}-{session_id or ''}-{os.getpid()}-{time.time_ns()}"
        short = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
        filename = f"{ts}_{short}.jpg"
        path = self._dir / filename
        rgb = image.convert("RGB") if image.mode != "RGB" else image
        rgb.save(path, format="JPEG", quality=self._jpeg_quality, optimize=True)
        size = path.stat().st_size
        return SavedScreenshot(
            filename=filename,
            path=path,
            width=rgb.width,
            height=rgb.height,
            bytes_written=size,
        )


# ---- 容量管理 / 統計 ------------------------------------------------------

def _is_older_than(path: Path, days: int, now: datetime | None = None) -> bool:
    if days <= 0:
        return False
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone()
    except OSError:
        return False
    cutoff = (now or _local_now()) - timedelta(days=days)
    return mtime < cutoff


def cleanup_old_data(cfg: CollectorConfig | None = None) -> dict[str, int]:
    """古いスクショ / イベントを削除し、削除件数を返す."""
    cfg = cfg or CollectorConfig()
    deleted_shots = 0
    deleted_events = 0
    now = _local_now()

    sd = screenshots_dir()
    for p in sd.glob("*.jpg"):
        if _is_older_than(p, cfg.keep_screenshots_days, now):
            try:
                p.unlink()
                deleted_shots += 1
            except OSError:
                logger.exception("unlink failed: %s", p)

    ed = events_dir()
    for p in ed.glob("*.jsonl"):
        if _is_older_than(p, cfg.keep_events_days, now):
            try:
                p.unlink()
                deleted_events += 1
            except OSError:
                logger.exception("unlink failed: %s", p)

    logger.info("cleanup: %d screenshots, %d event logs deleted", deleted_shots, deleted_events)
    return {"screenshots": deleted_shots, "events": deleted_events}


def _dir_size(path: Path, pattern: str = "*") -> int:
    total = 0
    for p in path.glob(pattern):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def get_stats(store: EventStore | None = None, recent_n: int = 10) -> dict[str, Any]:
    """タスクトレイ UI 用の統計情報."""
    store = store or EventStore()
    today = _today_str()
    today_events = store.read_day(today)
    recent = today_events[-recent_n:][::-1]
    stripped: list[dict[str, Any]] = []
    for e in recent:
        stripped.append({
            "ts": e.get("ts"),
            "event_seq": e.get("event_seq"),
            "process_name": (e.get("app") or {}).get("process_name"),
            "title": (e.get("window") or {}).get("title"),
            "dwell_ms_prev": e.get("dwell_ms_prev"),
        })
    bytes_screenshots = _dir_size(screenshots_dir(), "*.jpg")
    bytes_events = _dir_size(events_dir(), "*.jsonl")
    return {
        "today": today,
        "today_event_count": len(today_events),
        "recent": stripped,
        "bytes_screenshots": bytes_screenshots,
        "bytes_events": bytes_events,
        "bytes_total": bytes_screenshots + bytes_events,
    }


__all__ = [
    "EventStore",
    "ScreenshotStore",
    "SavedScreenshot",
    "cleanup_old_data",
    "get_stats",
    "iso_ts",
    "filename_ts",
]
