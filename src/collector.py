"""フォアグラウンドウィンドウ変化を駆動にするコレクター本体.

- ``WindowChangeWatcher``: SetWinEventHook によるイベント駆動監視
- ``capture_active``: アクティブモニターのみキャプチャ
- ``get_active_window_info``: アクティブウィンドウのメタ取得
- ``Collector``: メインループ。フィルタ→キャプチャ→マスキング→保存
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable, Optional

try:  # PIL は Mac でも入る
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover
    Image = None  # type: ignore

try:
    import mss  # type: ignore
    import mss.tools  # type: ignore
    _HAS_MSS = True
except Exception:  # pragma: no cover - Mac でも mss は入るが防御的
    mss = None  # type: ignore
    _HAS_MSS = False

# Windows 専用 API は try でガード（Mac でも import できるように）
try:
    import win32gui  # type: ignore
    import win32process  # type: ignore
    import win32con  # type: ignore
    import ctypes  # noqa: F401
    from ctypes import wintypes  # noqa: F401
    _HAS_WIN32 = True
except Exception:
    win32gui = None  # type: ignore
    win32process = None  # type: ignore
    win32con = None  # type: ignore
    _HAS_WIN32 = False

try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except Exception:
    psutil = None  # type: ignore
    _HAS_PSUTIL = False

from config import (
    CollectorConfig,
    load_config,
    logs_dir,
    pause_flag_file,
)
from storage import (
    EventStore,
    ScreenshotStore,
    cleanup_old_data,
    iso_ts,
)
from window_titles import is_blocklisted, mask_window_title


# ---- ロガー --------------------------------------------------------------

def _setup_logger() -> logging.Logger:
    log = logging.getLogger("workscope.collector")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    try:
        log_path = logs_dir() / "collector.log"
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
    except Exception:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    ))
    log.addHandler(handler)
    return log


logger = _setup_logger()


# ---- OCR + マスキング ----------------------------------------------------

# OCR と masker は遅延 import（PaddleOCR は重く Mac 開発環境では未導入のため）
try:
    from masker import MaskResult, mask_image  # type: ignore
    _HAS_MASKER = True
except Exception:
    _HAS_MASKER = False
    MaskResult = None  # type: ignore

try:
    from ocr import OCRBox, OCREngine  # type: ignore
    _HAS_OCR = True
except Exception:
    _HAS_OCR = False
    OCRBox = None  # type: ignore
    OCREngine = None  # type: ignore


# ---- ウィンドウ情報取得 --------------------------------------------------

@dataclass
class WindowInfo:
    """アクティブウィンドウのメタ情報."""
    hwnd: int
    title: str
    process_name: str
    process_path: str
    pid: int
    rect: tuple[int, int, int, int]  # x, y, w, h
    monitor: int


def _get_process_info(pid: int) -> tuple[str, str]:
    """psutil でプロセス名とパスを取る。失敗時は空文字."""
    if not _HAS_PSUTIL or pid <= 0:
        return "", ""
    try:
        proc = psutil.Process(pid)
        try:
            path = proc.exe()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            path = ""
        try:
            name = proc.name()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            name = os.path.basename(path) if path else ""
        return name, path
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        return "", ""


def _monitor_index_for_rect(
    rect: tuple[int, int, int, int],
    monitors: list[dict[str, int]],
) -> int:
    """矩形の中心が含まれるモニターのインデックス（mss 形式: 0=全体, 1..=各モニター）."""
    if not monitors or len(monitors) <= 1:
        return 0
    cx = rect[0] + rect[2] // 2
    cy = rect[1] + rect[3] // 2
    for i, m in enumerate(monitors[1:], start=1):
        mx, my = int(m["left"]), int(m["top"])
        mw, mh = int(m["width"]), int(m["height"])
        if mx <= cx < mx + mw and my <= cy < my + mh:
            return i
    return 1  # フォールバック: 最初の物理モニター


def get_active_window_info() -> Optional[WindowInfo]:
    """フォアグラウンドウィンドウのメタ情報を返す（取得不能なら None）."""
    if not _HAS_WIN32:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        title = ""
        try:
            title = win32gui.GetWindowText(hwnd) or ""
        except Exception:
            title = ""
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = 0
        proc_name, proc_path = _get_process_info(pid)
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            left = top = right = bottom = 0
        rect = (left, top, max(0, right - left), max(0, bottom - top))

        # モニター解決は mss で
        mon_idx = 0
        if _HAS_MSS:
            try:
                with mss.mss() as sct:
                    mon_idx = _monitor_index_for_rect(rect, sct.monitors)
            except Exception:
                mon_idx = 0

        return WindowInfo(
            hwnd=int(hwnd),
            title=title,
            process_name=proc_name,
            process_path=proc_path,
            pid=int(pid),
            rect=rect,
            monitor=mon_idx,
        )
    except Exception:
        logger.exception("get_active_window_info failed")
        return None


# ---- スクショ ------------------------------------------------------------

def capture_active(info: WindowInfo | None = None) -> Optional["Image.Image"]:
    """アクティブモニターのみをキャプチャして PIL.Image を返す."""
    if not _HAS_MSS or Image is None:
        logger.warning("capture_active: mss/Pillow unavailable")
        return None
    info = info or get_active_window_info()
    try:
        with mss.mss() as sct:
            monitors = sct.monitors
            mon_idx = info.monitor if (info and 0 < info.monitor < len(monitors)) else 1
            shot = sct.grab(monitors[mon_idx])
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            return img
    except Exception:
        logger.exception("capture_active failed")
        return None


# ---- ウィンドウ変化監視 --------------------------------------------------

class WindowChangeWatcher:
    """SetWinEventHook で EVENT_SYSTEM_FOREGROUND を監視.

    pywin32 不在時は noop（テスト・Mac開発用）。
    """

    EVENT_SYSTEM_FOREGROUND = 0x0003
    WINEVENT_OUTOFCONTEXT = 0x0000

    def __init__(self, on_change: Callable[[WindowInfo], None]) -> None:
        self._on_change = on_change
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._hook = None

    @property
    def available(self) -> bool:
        return _HAS_WIN32

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        if not _HAS_WIN32:
            logger.warning("WindowChangeWatcher: pywin32 unavailable; noop mode")
            return
        self._thread = threading.Thread(target=self._run, name="ws-winevent", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # メッセージループには PostThreadMessage で WM_QUIT が必要だが、
        # daemon=True なので終了時にプロセスごと閉じる前提で簡略化。
        self._thread = None

    # 単発 fire（テスト用）
    def fire_manual(self, info: WindowInfo) -> None:
        try:
            self._on_change(info)
        except Exception:
            logger.exception("on_change raised")

    def _run(self) -> None:  # pragma: no cover - Windows 専用
        import ctypes
        from ctypes import wintypes

        WinEventProcType = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.HWND,
            wintypes.LONG,
            wintypes.LONG,
            wintypes.DWORD,
            wintypes.DWORD,
        )

        def _callback(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
            try:
                info = get_active_window_info()
                if info is not None:
                    self._on_change(info)
            except Exception:
                logger.exception("WinEvent callback failed")

        cb = WinEventProcType(_callback)
        user32 = ctypes.windll.user32
        self._hook = user32.SetWinEventHook(
            self.EVENT_SYSTEM_FOREGROUND,
            self.EVENT_SYSTEM_FOREGROUND,
            0,
            cb,
            0,
            0,
            self.WINEVENT_OUTOFCONTEXT,
        )

        # メッセージループ
        msg = wintypes.MSG()
        while not self._stop.is_set():
            res = user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1)
            if res:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.05)

        if self._hook:
            user32.UnhookWinEvent(self._hook)
            self._hook = None


# ---- フィルタ判定 --------------------------------------------------------

def _in_quiet_hours(cfg: CollectorConfig, now: datetime | None = None) -> bool:
    """quiet_hours の範囲内なら True."""
    s = cfg.quiet_hours_start
    e = cfg.quiet_hours_end
    if s is None or e is None:
        return False
    now = now or datetime.now()
    cur = dtime(now.hour, now.minute)
    start = dtime(int(s) % 24, 0)
    end = dtime(int(e) % 24, 0)
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    # 跨ぎ（22-6 など）
    return cur >= start or cur < end


# ---- Collector 本体 ------------------------------------------------------

@dataclass
class _FocusState:
    """直前フォーカスの追跡用."""
    hwnd: int = 0
    process_name: str = ""
    focus_since: float = 0.0


class Collector:
    """メインループ.

    ``WindowChangeWatcher`` から ``WindowInfo`` を受けて、
    フィルタ→キャプチャ→マスキング→保存→イベント追記 を行う。
    """

    def __init__(
        self,
        cfg: CollectorConfig | None = None,
        event_store: EventStore | None = None,
        screenshot_store: ScreenshotStore | None = None,
        ocr_engine: Any = None,
    ) -> None:
        self._cfg = cfg or load_config()
        self._events = event_store or EventStore()
        self._shots = screenshot_store or ScreenshotStore(jpeg_quality=self._cfg.jpeg_quality)
        self._session_id = str(uuid.uuid4())
        self._seq = 0
        self._lock = threading.Lock()
        self._focus = _FocusState()
        self._capture_times: deque[float] = deque(maxlen=self._cfg.max_capture_per_minute + 16)
        self._watcher = WindowChangeWatcher(self._on_window_change)
        self._stop = threading.Event()
        self._last_cleanup_day: str | None = None

        # OCR エンジンは遅延インスタンス化（PaddleOCR は初期化が重い）
        self._ocr_engine: Any = ocr_engine
        self._ocr_init_attempted = ocr_engine is not None

    # --- 公開 -------------------------------------------------------------
    @property
    def session_id(self) -> str:
        return self._session_id

    def start(self) -> None:
        logger.info("Collector start session=%s", self._session_id)
        try:
            cleanup_old_data(self._cfg)
        except Exception:
            logger.exception("initial cleanup failed")
        self._watcher.start()

    def run(self) -> None:
        """Collector をブロッキングで実行する（main.py のスレッドエントリポイント）.

        ``start()`` で WindowChangeWatcher を起動した後、``stop()`` が呼ばれる
        まで待機する。WindowChangeWatcher 自身は daemon スレッドで動くため、
        この while ループはアプリ終了時の cleanup フックとしての役割。
        """
        self.start()
        while not self._stop.is_set():
            self._stop.wait(1.0)
        logger.info("Collector.run loop exited")

    def stop(self) -> None:
        logger.info("Collector stop session=%s", self._session_id)
        self._stop.set()
        self._watcher.stop()
        self._events.close()

    # テスト用: ループを介さずイベントを 1 回処理
    def process(self, info: WindowInfo) -> dict[str, Any] | None:
        return self._on_window_change(info)

    # --- 内部 -------------------------------------------------------------
    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _is_paused(self) -> bool:
        try:
            return pause_flag_file().exists()
        except Exception:
            return False

    def _under_rate_limit(self, now: float) -> bool:
        cap = max(1, int(self._cfg.max_capture_per_minute))
        # 直近 60 秒以内のキャプチャを数える
        cutoff = now - 60.0
        while self._capture_times and self._capture_times[0] < cutoff:
            self._capture_times.popleft()
        return len(self._capture_times) < cap

    def _maybe_daily_cleanup(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_cleanup_day == today:
            return
        try:
            cleanup_old_data(self._cfg)
        except Exception:
            logger.exception("daily cleanup failed")
        self._last_cleanup_day = today

    def _ensure_ocr(self) -> Any:
        """OCREngine を遅延初期化して返す（不可なら None）."""
        if self._ocr_engine is not None:
            return self._ocr_engine
        if self._ocr_init_attempted:
            return None
        self._ocr_init_attempted = True
        if not _HAS_OCR:
            logger.warning("OCREngine not importable; running without OCR")
            return None
        try:
            self._ocr_engine = OCREngine(
                languages=list(self._cfg.ocr_languages),
                max_image_side=int(self._cfg.ocr_max_image_side),
            )
            logger.info("OCREngine initialized")
        except Exception:
            logger.exception("OCREngine init failed; running without OCR")
            self._ocr_engine = None
        return self._ocr_engine

    def _run_ocr(self, image: "Image.Image") -> list:
        """画像から OCRBox 一覧を取得（失敗時は空）."""
        engine = self._ensure_ocr()
        if engine is None:
            return []
        try:
            return engine.extract(image)
        except Exception:
            logger.exception("OCR extract failed")
            return []

    def _on_window_change(self, info: WindowInfo) -> dict[str, Any] | None:
        """ウィンドウ変化 1 回ぶんの処理。例外はログして None を返す."""
        try:
            return self._handle(info)
        except Exception:
            logger.exception("collector handle failed")
            return None

    def _handle(self, info: WindowInfo) -> dict[str, Any] | None:
        self._maybe_daily_cleanup()

        now = time.time()
        prev = self._focus
        # 同一 hwnd への再通知は無視
        if info.hwnd == prev.hwnd:
            return None

        # 滞在時間: 前回フォーカスから現在まで
        dwell_ms_prev = int((now - prev.focus_since) * 1000) if prev.focus_since > 0 else 0
        prev_proc = prev.process_name

        # フォーカス記録は常に更新（フィルタで弾いてもタイムラインの起点にはする）
        self._focus = _FocusState(
            hwnd=info.hwnd,
            process_name=info.process_name,
            focus_since=now,
        )

        # ---- フィルタ ----
        if self._is_paused():
            logger.debug("paused: skip")
            return None

        if _in_quiet_hours(self._cfg):
            logger.debug("quiet hours: skip capture")
            return None

        # min_dwell: 直前ウィンドウに最低限留まっていたか
        if dwell_ms_prev > 0 and dwell_ms_prev < int(self._cfg.min_dwell_seconds_for_capture * 1000):
            logger.debug("dwell too short (%dms): skip", dwell_ms_prev)
            return None

        # blocklist（allowlist で例外）
        allow = any(
            (a or "").lower() in (info.process_name or "").lower()
            for a in self._cfg.allowlist_processes if a
        )
        if not allow and is_blocklisted(
            info.title,
            info.process_name,
            self._cfg.blocklist_processes,
            self._cfg.blocklist_title_substrings,
        ):
            logger.info("blocklisted: %s", info.process_name)
            return None

        # rate limit
        if not self._under_rate_limit(now):
            logger.warning("rate limit exceeded: skip")
            return None

        # ---- キャプチャ ----
        img = capture_active(info)
        if img is None:
            logger.warning("capture failed; emit metadata-only event")
            return self._write_event(info, prev_proc, dwell_ms_prev, None, None)

        # ---- OCR + マスキング ----
        # 失敗時の安全側挙動:
        #   strict マスキング前の生スクショを絶対にディスクに残さない。
        #   drop_image_if_unmaskable=True なら画像なしのメタイベントのみ書く。
        if not _HAS_MASKER:
            logger.error("masker module unavailable; refusing to save raw screenshot")
            return self._write_event(info, prev_proc, dwell_ms_prev, None, None)

        ocr_boxes = self._run_ocr(img)

        try:
            mr = mask_image(img, ocr_boxes, strict=bool(self._cfg.mask_strict_mode))
        except Exception:
            logger.exception("mask_image failed")
            if self._cfg.drop_image_if_unmaskable:
                logger.warning("drop image because mask_image raised")
                return self._write_event(info, prev_proc, dwell_ms_prev, None, None)
            return self._write_event(info, prev_proc, dwell_ms_prev, None, None)

        if mr.unmaskable and self._cfg.drop_image_if_unmaskable:
            logger.warning(
                "unmaskable content suspected (boxes=%d, mask_count=%d); drop image",
                len(ocr_boxes),
                mr.mask_count,
            )
            return self._write_event(info, prev_proc, dwell_ms_prev, None, None)

        # OCR が完全に空（パドル未導入 等）の場合、テキストが読めていないので
        # 「マスクすべきか判定できない」状態。strict + drop_image_if_unmaskable
        # の組み合わせならスクショは保存しない（メタのみ残す）。
        if not ocr_boxes and self._cfg.mask_strict_mode and self._cfg.drop_image_if_unmaskable:
            logger.warning(
                "OCR returned 0 boxes in strict mode; drop image (metadata-only event)"
            )
            return self._write_event(info, prev_proc, dwell_ms_prev, None, None)

        # ---- 保存 ----
        try:
            saved = self._shots.save(mr.masked_image, session_id=self._session_id)
            self._capture_times.append(now)
        except Exception:
            logger.exception("screenshot save failed")
            return self._write_event(info, prev_proc, dwell_ms_prev, None, None)

        ss_payload = {
            "filename": saved.filename,
            "width": saved.width,
            "height": saved.height,
            "ocr_text_summary": mr.text_summary,
            "ocr_token_count": len(ocr_boxes),
            "mask_applied_count": int(mr.mask_count),
            "mask_categories": list(mr.mask_categories),
            "unmaskable_suspected": bool(mr.unmaskable),
        }
        return self._write_event(info, prev_proc, dwell_ms_prev, ss_payload, saved.path)

    def _write_event(
        self,
        info: WindowInfo,
        prev_proc: str,
        dwell_ms_prev: int,
        screenshot: dict[str, Any] | None,
        _path: Path | None,
    ) -> dict[str, Any]:
        title_masked, title_hash, title_categories = mask_window_title(info.title)
        event: dict[str, Any] = {
            "session_id": self._session_id,
            "event_seq": self._next_seq(),
            "ts": iso_ts(),
            "event_type": "window_focus",
            "app": {
                "process_name": info.process_name,
                "process_path": info.process_path,
                "pid": info.pid,
            },
            "window": {
                "title": title_masked,
                "title_raw_hash": title_hash,
                "title_mask_categories": title_categories,
                "hwnd": info.hwnd,
                "rect": list(info.rect),
                "monitor": info.monitor,
            },
            "dwell_ms_prev": dwell_ms_prev,
            "screenshot": screenshot,
            "transition_from_app": prev_proc,
        }
        try:
            self._events.append(event)
        except Exception:
            logger.exception("event append failed")
        return event


__all__ = [
    "Collector",
    "WindowChangeWatcher",
    "WindowInfo",
    "capture_active",
    "get_active_window_info",
]
