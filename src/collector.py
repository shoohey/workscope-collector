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

# v1.0: アプリ自動分類 + UI Automation + キーストロークロガー
try:
    from app_classifier import classify as _classify_app  # type: ignore
    _HAS_APP_CLASSIFIER = True
except Exception:
    _classify_app = None  # type: ignore
    _HAS_APP_CLASSIFIER = False

try:
    from uia_capture import get_focused_control as _get_focused_control  # type: ignore
    _HAS_UIA = True
except Exception:
    _get_focused_control = None  # type: ignore
    _HAS_UIA = False

try:
    from input_events import InputEventLogger, KeyEvent, MouseEvent, resolve_click_target  # type: ignore
    _HAS_INPUT_EVENTS = True
except Exception:
    InputEventLogger = None  # type: ignore
    KeyEvent = None  # type: ignore
    MouseEvent = None  # type: ignore
    resolve_click_target = None  # type: ignore
    _HAS_INPUT_EVENTS = False


SCHEMA_VERSION = 2


def _mask_text_with_default_profile(text: str) -> str:
    """テキスト1個をデフォルトプロファイルでマスクするヘルパ.

    OCRBox を1つ生成してマスカーに通し、PII カテゴリにマッチすれば
    [MASKED:<category>] に置換、マッチしなければ原文を返す。
    Codex High#3 の UIA name/parent_path マスク経路で使用。
    """
    if not text:
        return ""
    try:
        from masker import _classify_box, DEFAULT_RULES  # type: ignore[attr-defined]
        if OCRBox is None:
            return text
        box = OCRBox(text=text, bbox=(0, 0, 1, 1), confidence=1.0)
        # strict=False: フィールド名/親要素名はラベルが多いので過剰マスク回避
        cats, _ = _classify_box(box, set(), DEFAULT_RULES, False, ())
        if cats:
            return f"[MASKED:{cats[0]}]"
        return text
    except Exception:
        logger.exception("_mask_text_with_default_profile failed")
        return text


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
    """アクティブモニターのみをキャプチャして PIL.Image を返す.

    後方互換のため残置。フォーカス側1モニターだけを欲しい場合に使う。
    マルチモニター環境で全画面を取りたい場合は ``capture_all_monitors`` を使う。
    """
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


def capture_all_monitors(
    info: WindowInfo | None = None,
) -> list[tuple[int, "Image.Image"]]:
    """物理モニターを全て個別にキャプチャして返す.

    戻り値: ``[(monitor_index, PIL.Image), ...]``
    - ``monitor_index`` は ``mss`` の 1始まり物理モニター番号
      （``sct.monitors[0]`` は全モニター結合の仮想スクリーンなので除外）
    - フォーカス側のモニターを先頭に並べ替えるので、JSONLの ``screenshot``
      フィールドにはフォーカス側が入る
    - mss/Pillow 不在環境（Mac開発・テスト）では ``capture_active`` に
      フォールバックして1要素のリストを返す

    マルチモニター環境（ノートPC＋外部モニター等）で「フォーカスしてない
    参照画面（処方箋PDF、カルテ等）」を取りこぼさないために導入。
    """
    info = info or get_active_window_info()

    def _fallback_single() -> list[tuple[int, "Image.Image"]]:
        img = capture_active(info)
        if img is None:
            return []
        idx = info.monitor if (info and info.monitor > 0) else 1
        return [(idx, img)]

    if not _HAS_MSS or Image is None:
        # mss/Pillow 不在環境（Mac/テスト）: capture_active 経由でシングルモニター扱い
        return _fallback_single()

    out: list[tuple[int, "Image.Image"]] = []
    try:
        with mss.mss() as sct:
            monitors = sct.monitors
            # monitors[0] は全モニター結合仮想スクリーン → 物理モニターは 1..
            physical = list(enumerate(monitors))[1:]
            if not physical:
                # 物理モニター列挙不能 → capture_active にフォールバック
                return _fallback_single()

            # フォーカス側を先頭に並べ替え（info.monitor が有効な物理 idx の時のみ）
            focus_idx = info.monitor if (info and 0 < info.monitor < len(monitors)) else None
            if focus_idx is not None:
                physical.sort(key=lambda p: 0 if p[0] == focus_idx else 1)

            for idx, mon in physical:
                try:
                    shot = sct.grab(mon)
                    img = Image.frombytes("RGB", shot.size, shot.rgb)
                    out.append((idx, img))
                except Exception:
                    logger.exception("capture_all_monitors: grab failed for monitor %d", idx)
                    # 1枚失敗しても他のモニターは取り続ける
                    continue
            if not out:
                return _fallback_single()
            return out
    except Exception:
        logger.exception("capture_all_monitors failed")
        return out or _fallback_single()


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

        # ---- キャプチャ（マルチモニター対応） ----
        # capture_active_monitor_only=True なら従来通りフォーカス側1枚のみ。
        # False なら全物理モニターを個別にキャプチャ→各画面で OCR→マスク→保存。
        # フォーカス側を必ず先頭に並べる（先頭が event["screenshot"] に入る）。
        if bool(self._cfg.capture_active_monitor_only):
            img = capture_active(info)
            frames: list[tuple[int, "Image.Image"]] = (
                [(info.monitor if info.monitor > 0 else 1, img)] if img is not None else []
            )
        else:
            frames = capture_all_monitors(info)

        if not frames:
            logger.warning("capture failed; emit metadata-only event")
            return self._write_event(info, prev_proc, dwell_ms_prev, None, [], None)

        raw_mode = bool(self._cfg.raw_capture_mode)

        # 各画面をフル処理（OCR→マスク→保存）し、SS payload のリストを得る
        payloads: list[dict[str, Any] | None] = []
        for monitor_index, img in frames:
            payload = self._process_one_capture(
                info=info,
                img=img,
                monitor_index=monitor_index,
                raw_mode=raw_mode,
            )
            payloads.append(payload)

        # 全画面失敗 → metadata-only
        if all(p is None for p in payloads):
            return self._write_event(info, prev_proc, dwell_ms_prev, None, [], None)

        # 先頭（=フォーカス側）を主スクショ、残りを additional に
        primary = payloads[0]
        additional = [p for p in payloads[1:] if p is not None]

        # 主スクショが None の場合は、最初の有効な追加スクショを主に昇格
        if primary is None:
            for i, p in enumerate(payloads[1:], start=1):
                if p is not None:
                    primary = p
                    additional = [q for q in (payloads[1:i] + payloads[i + 1:]) if q is not None]
                    break

        # 主スクショ確定でレート制限カウントを1回だけ進める
        self._capture_times.append(now)

        return self._write_event(
            info, prev_proc, dwell_ms_prev, primary, additional, None
        )

    def _process_one_capture(
        self,
        info: WindowInfo,
        img: "Image.Image",
        monitor_index: int,
        raw_mode: bool,
    ) -> dict[str, Any] | None:
        """1モニター分の OCR→マスク→保存 を行い、screenshot payload を返す.

        - 失敗時は ``None`` を返す（caller 側で metadata-only に降格判断）
        - ``monitor_index`` は ``mss`` の 1始まり物理モニター番号で、
          保存ファイル名に ``_mon{N}`` サフィックスとして記録される
        - ``raw_mode`` が True のとき、OCR/マスク失敗経路でも生画像を保存する
        """
        if not _HAS_MASKER:
            if raw_mode:
                return self._save_unmasked_payload(
                    img, monitor_index=monitor_index, degraded_reason="masker_unavailable"
                )
            logger.error(
                "masker module unavailable; refusing to save raw screenshot (mon=%d)",
                monitor_index,
            )
            return None

        ocr_boxes = self._run_ocr(img)

        try:
            mr = mask_image(img, ocr_boxes, strict=bool(self._cfg.mask_strict_mode))
        except Exception:
            logger.exception("mask_image failed (mon=%d)", monitor_index)
            if raw_mode:
                return self._save_unmasked_payload(
                    img, monitor_index=monitor_index, degraded_reason="mask_exception"
                )
            return None

        if mr.unmaskable and self._cfg.drop_image_if_unmaskable:
            if raw_mode:
                logger.warning(
                    "unmaskable content suspected (mon=%d, boxes=%d) but raw_capture_mode=True"
                    " -> save partially-masked image (text summary suppressed)",
                    monitor_index, len(ocr_boxes),
                )
                return self._save_unmasked_payload(
                    mr.masked_image, monitor_index=monitor_index, degraded_reason="unmaskable"
                )
            logger.warning(
                "unmaskable content suspected (mon=%d, boxes=%d, mask_count=%d); drop image",
                monitor_index, len(ocr_boxes), mr.mask_count,
            )
            return None

        # OCR が完全に空（パドル未導入 等）の場合、テキストが読めていない。
        if not ocr_boxes and self._cfg.mask_strict_mode and self._cfg.drop_image_if_unmaskable:
            if raw_mode:
                return self._save_unmasked_payload(
                    img, monitor_index=monitor_index, degraded_reason="ocr_empty"
                )
            logger.warning(
                "OCR returned 0 boxes in strict mode (mon=%d); drop image",
                monitor_index,
            )
            return None

        try:
            saved = self._shots.save(
                mr.masked_image,
                session_id=self._session_id,
                monitor_index=monitor_index,
            )
        except Exception:
            logger.exception("screenshot save failed (mon=%d)", monitor_index)
            return None

        return {
            "filename": saved.filename,
            "width": saved.width,
            "height": saved.height,
            "monitor_index": monitor_index,
            "ocr_text_summary": mr.text_summary,
            "ocr_token_count": len(ocr_boxes),
            "mask_applied_count": int(mr.mask_count),
            "mask_categories": list(mr.mask_categories),
            "unmaskable_suspected": bool(mr.unmaskable),
            "degraded_reason": None,
        }

    def _save_unmasked_payload(
        self,
        img: "Image.Image",
        monitor_index: int,
        degraded_reason: str,
    ) -> dict[str, Any] | None:
        """raw_capture_mode 用: OCR/マスク失敗時に生画像を保存して payload を返す."""
        try:
            saved = self._shots.save(
                img,
                session_id=self._session_id,
                monitor_index=monitor_index,
            )
        except Exception:
            logger.exception("unmasked screenshot save failed (mon=%d)", monitor_index)
            return None

        logger.warning(
            "raw_capture_mode: saved UNMASKED screenshot (mon=%d, reason=%s, file=%s)",
            monitor_index, degraded_reason, saved.filename,
        )
        return {
            "filename": saved.filename,
            "width": saved.width,
            "height": saved.height,
            "monitor_index": monitor_index,
            "ocr_text_summary": "",
            "ocr_token_count": 0,
            "mask_applied_count": 0,
            "mask_categories": [],
            "unmaskable_suspected": True,
            "degraded_reason": degraded_reason,
        }

    def _write_event(
        self,
        info: WindowInfo,
        prev_proc: str,
        dwell_ms_prev: int,
        screenshot: dict[str, Any] | None,
        additional_screenshots: list[dict[str, Any]] | None = None,
        _path: Path | None = None,
    ) -> dict[str, Any]:
        title_masked, title_hash, title_categories = mask_window_title(info.title)

        # v1.0: アプリ自動分類（業務フロー解析・RPA出口振り分けの基準）
        app_category = ""
        rpa_target = ""
        if _HAS_APP_CLASSIFIER and _classify_app is not None:
            try:
                cls = _classify_app(
                    process_name=info.process_name,
                    process_path=info.process_path,
                    window_title=info.title,
                )
                app_category = cls.category
                rpa_target = cls.rpa_target
            except Exception:
                logger.exception("app classification failed")

        # v1.0: UI Automation でフォーカス中コントロール取得（Win32実機のみ実動）
        # Codex High#2: 各 window_focus で stale 解消のため一旦 False に戻す。
        #              UIA 成功時のみ True にする。UIA 失敗時はパスワード状態は不明として False。
        # Codex High#3: name/parent_path もマスカー経由化する。
        self._password_field_active = False
        focused_control = None
        if _HAS_UIA and _get_focused_control is not None:
            try:
                fc = _get_focused_control(hwnd=info.hwnd, timeout_ms=200)
                if fc is not None:
                    # name/parent_path をマスカー通過させる (PII保護)
                    try:
                        from uia_capture import apply_masks_to_focused_control  # type: ignore
                        fc = apply_masks_to_focused_control(fc, _mask_text_with_default_profile)
                    except Exception:
                        logger.exception("apply_masks_to_focused_control failed; dropping name/parent")
                        fc.name = ""
                        fc.parent_path = []
                    focused_control = fc.to_dict()
                    self._password_field_active = bool(fc.is_password)
            except Exception:
                logger.exception("UIA focused control failed")

        event: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self._session_id,
            "event_seq": self._next_seq(),
            "ts": iso_ts(),
            "event_type": "window_focus",
            "app": {
                "process_name": info.process_name,
                "process_path": info.process_path,
                "pid": info.pid,
                "category": app_category,
                "rpa_target": rpa_target,
            },
            "window": {
                "title": title_masked,
                "title_raw_hash": title_hash,
                "title_mask_categories": title_categories,
                "hwnd": info.hwnd,
                "rect": list(info.rect),
                "monitor": info.monitor,
            },
            "focused_control": focused_control,
            "dwell_ms_prev": dwell_ms_prev,
            "screenshot": screenshot,
            "additional_screenshots": list(additional_screenshots or []),
            "transition_from_app": prev_proc,
        }
        try:
            self._events.append(event)
        except Exception:
            logger.exception("event append failed")
        return event

    # ---- v1.0: 入力イベント (key_typed/key_combo/mouse_click) 取り込み ----

    def is_password_field_active(self) -> bool:
        """直近の UIA 取得結果からパスワードフィールドにフォーカスがあるかを返す.

        InputEventLogger に渡して、パスワード入力中はキーロギング自体を停止させる用途。
        """
        return getattr(self, "_password_field_active", False)

    def feed_key_event(self, ev: Any) -> dict[str, Any] | None:
        """KeyEvent を受け取って JSONL に書く.

        InputEventLogger からのコールバックとして使う。直接呼ばれてもよい。
        """
        if ev is None:
            return None
        try:
            data = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
        except Exception:
            logger.exception("key event payload conversion failed")
            return None
        out: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self._session_id,
            "event_seq": self._next_seq(),
            "ts": iso_ts(),
            "event_type": data.get("event_type", "key_typed"),
            "input": data,
            "app_focus_hwnd": getattr(self._focus, "hwnd", 0),
        }
        try:
            self._events.append(out)
        except Exception:
            logger.exception("key event append failed")
        return out

    def feed_mouse_event(self, ev: Any, ocr_boxes: list | None = None) -> dict[str, Any] | None:
        """MouseEvent を受け取って JSONL に書く. ocr_boxes があればクリック対象推定."""
        if ev is None:
            return None
        try:
            data = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
        except Exception:
            logger.exception("mouse event payload conversion failed")
            return None

        # クリック対象推定（マスカー通過後のテキストのみ）
        if ocr_boxes and resolve_click_target is not None and not data.get("target_text_masked"):
            try:
                from masker import mask_image  # noqa: F401  # マスカー利用可能性確認
                from masker import DEFAULT_RULES  # type: ignore
                # 簡易マスカー: 文字列1つを評価したい
                from masker import _classify_box  # type: ignore[attr-defined]

                def _mask(text: str) -> str:
                    # OCRBoxラッパーを作って分類
                    # strict=False: クリック対象はボタン名/ラベルが多いので、
                    # 「漢字2-5文字」の過剰マスク(name_like_kanji)を避ける。
                    # context-free な高信頼ルール(メアド/電話/敬称付き氏名等)は
                    # strict=False でも発火するのでPII漏洩リスクは維持される。
                    if OCRBox is None:
                        return text
                    box = OCRBox(text=text, bbox=(0, 0, 1, 1), confidence=1.0)
                    cats, _ = _classify_box(box, set(), DEFAULT_RULES, False, ())
                    if cats:
                        return f"[MASKED:{cats[0]}]"
                    return text

                target = resolve_click_target(tuple(data["coords"]), ocr_boxes, mask_func=_mask)
                data["target_text_masked"] = target
            except Exception:
                logger.exception("click target resolve failed")

        out: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self._session_id,
            "event_seq": self._next_seq(),
            "ts": iso_ts(),
            "event_type": "mouse_click",
            "input": data,
            "app_focus_hwnd": getattr(self._focus, "hwnd", 0),
        }
        try:
            self._events.append(out)
        except Exception:
            logger.exception("mouse event append failed")
        return out

    def start_input_logger(self) -> bool:
        """InputEventLogger を起動. ライブラリ無し環境では False を返す."""
        if not _HAS_INPUT_EVENTS or InputEventLogger is None:
            logger.info("InputEventLogger unavailable (Mac/Linux or libs missing)")
            return False
        try:
            self._input_logger = InputEventLogger(
                on_key=self.feed_key_event,
                on_mouse=self.feed_mouse_event,
                is_password_field_active=self.is_password_field_active,
            )
            if not self._input_logger.available:
                return False
            self._input_logger.start()
            logger.info("InputEventLogger started")
            return True
        except Exception:
            logger.exception("InputEventLogger start failed")
            return False

    def stop_input_logger(self) -> None:
        il = getattr(self, "_input_logger", None)
        if il is not None:
            try:
                il.stop()
            except Exception:
                logger.exception("InputEventLogger stop failed")
            self._input_logger = None


__all__ = [
    "Collector",
    "WindowChangeWatcher",
    "WindowInfo",
    "capture_active",
    "get_active_window_info",
]
