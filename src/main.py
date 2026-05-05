"""WorkScope Collector エントリポイント.

責務:
1. 多重起動防止 (PID lock)
2. ロガー初期化 (TimedRotatingFileHandler 日次)
3. 設定ロード
4. Collector を別スレッドで起動
5. Tray をメインスレッドで起動 (pystray の要件)
6. SIGINT / SIGTERM で graceful shutdown
7. 未捕捉例外を crash.log に記録 (自動再起動はしない)
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import os
import signal
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# src/ を sys.path に追加（PyInstaller / 直接起動 両対応）
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import APP_NAME, app_data_dir, load_config, logs_dir, CUSTOMER_NAME, DEFAULT_PROFILE, UPLOAD_ENDPOINT, UPLOAD_API_KEY  # noqa: E402
from version import __version__  # noqa: E402

# v1.0: 同意ゲート (consent_signed.json が無ければダイアログ → 同意 or 終了)
try:
    from consent import ensure_consent_or_exit, is_consented  # type: ignore
    _HAS_CONSENT = True
except Exception:
    _HAS_CONSENT = False
    ensure_consent_or_exit = None  # type: ignore
    is_consented = None  # type: ignore

# v1.0: クラウドアップロードスケジューラ (UPLOAD_ENDPOINT + UPLOAD_API_KEY が両方あれば起動)
try:
    from uploader import UploadScheduler  # type: ignore
    _HAS_UPLOADER = True
except Exception:
    _HAS_UPLOADER = False
    UploadScheduler = None  # type: ignore


logger = logging.getLogger("workscope")


# ---- ロガー初期化 -------------------------------------------------------------

def _init_logging() -> None:
    """日次ローテーションのファイルロガーを構成.

    PyInstaller --noconsole 環境では stdout/stderr が無いため、ファイル一本化。
    """
    log_path = logs_dir() / "main.log"
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 既存ハンドラを除去 (再初期化対策)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_path),
        when="midnight",
        interval=1,
        backupCount=14,
        encoding="utf-8",
        delay=False,
        utc=False,
    )
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(fmt)
    root.addHandler(handler)

    # 開発時のみ stderr にも出す (frozen 時は stderr が None のことがある)
    if not getattr(sys, "frozen", False) and sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)


# ---- crash.log への未捕捉例外記録 --------------------------------------------

def _crash_log_path() -> Path:
    return logs_dir() / "crash.log"


def _write_crash(exc_type: type, exc_value: BaseException, tb: Any) -> None:
    try:
        with open(_crash_log_path(), "a", encoding="utf-8") as f:
            f.write(f"\n----- crash @ {datetime.now().isoformat()} -----\n")
            traceback.print_exception(exc_type, exc_value, tb, file=f)
    except OSError:
        pass


def _excepthook(exc_type: type, exc_value: BaseException, tb: Any) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        # 通常終了として扱う
        sys.__excepthook__(exc_type, exc_value, tb)
        return
    logger.critical("uncaught exception", exc_info=(exc_type, exc_value, tb))
    _write_crash(exc_type, exc_value, tb)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:  # type: ignore[name-defined]
    if issubclass(args.exc_type, KeyboardInterrupt):
        return
    logger.critical(
        "uncaught exception in thread %r",
        args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    _write_crash(args.exc_type, args.exc_value, args.exc_traceback)


# ---- 多重起動防止 -------------------------------------------------------------

class SingleInstanceLock:
    """PID ファイルベースの単純な多重起動ガード.

    Windows の OS ロック (msvcrt.locking) も併用して、
    同一プロセスの再起動でも安全にロックを更新できるようにする。
    Mac/Linux では PID 生存チェックのみ。
    """

    def __init__(self) -> None:
        self.path = app_data_dir() / "app.lock"
        self._fp = None  # type: ignore[assignment]
        self._acquired = False

    def acquire(self) -> bool:
        # 既存 PID が生きているか確認
        if self.path.exists():
            try:
                old_pid = int(self.path.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                old_pid = 0
            if old_pid > 0 and _is_pid_alive(old_pid) and old_pid != os.getpid():
                logger.error(
                    "another instance is running (pid=%d); abort", old_pid,
                )
                return False
        # 取得
        try:
            self._fp = open(self.path, "w", encoding="utf-8")
            self._fp.write(str(os.getpid()))
            self._fp.flush()
            try:
                # POSIX: fcntl で排他, Windows: msvcrt.locking。失敗しても致命ではない
                if sys.platform == "win32":
                    import msvcrt  # type: ignore
                    msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl  # type: ignore
                    fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:
                # ロックは取れなかったが PID は書けたので緩く許可
                logger.warning("os-level lock failed; using PID-only guard")
            self._acquired = True
            atexit.register(self.release)
            return True
        except OSError:
            logger.exception("failed to acquire lock at %s", self.path)
            return False

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            if self._fp is not None:
                try:
                    if sys.platform == "win32":
                        import msvcrt  # type: ignore
                        try:
                            self._fp.seek(0)
                            msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
                        except Exception:
                            pass
                    else:
                        import fcntl  # type: ignore
                        try:
                            fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
                        except Exception:
                            pass
                finally:
                    self._fp.close()
                    self._fp = None
            try:
                # 自分が書いた PID と一致する場合のみ削除
                if self.path.exists():
                    pid_in_file = self.path.read_text(encoding="utf-8").strip()
                    if pid_in_file == str(os.getpid()):
                        self.path.unlink()
            except OSError:
                pass
        finally:
            self._acquired = False


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
            )
            if not h:
                return False
            try:
                code = ctypes.c_ulong(0)
                ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return pid_exists_via_proc(pid)
        except OSError:
            return False


def pid_exists_via_proc(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


# ---- Collector 起動 -----------------------------------------------------------

def _start_collector(config: Any) -> tuple[Any, Optional[threading.Thread]]:
    """Collector を別スレッドで起動して (collector, thread) を返す.

    Collector が未実装 (別エージェント実装中) の場合、None を返してトレイのみ起動する。
    """
    try:
        from collector import Collector  # type: ignore
    except Exception:
        logger.warning("collector module not available yet; tray-only mode")
        return None, None

    try:
        collector = Collector(config)
    except Exception:
        logger.exception("Collector instantiation failed; tray-only mode")
        return None, None

    def _run() -> None:
        try:
            collector.run()
        except Exception:
            logger.exception("Collector.run crashed")

    th = threading.Thread(target=_run, name="collector", daemon=True)
    th.start()
    logger.info("collector thread started")
    return collector, th


# ---- main ---------------------------------------------------------------------

def main() -> int:
    _init_logging()
    sys.excepthook = _excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook  # type: ignore[assignment]

    logger.info("==== %s Collector v%s starting ====", APP_NAME, __version__)
    logger.info("python=%s platform=%s pid=%d", sys.version.split()[0], sys.platform, os.getpid())

    # 多重起動防止
    lock = SingleInstanceLock()
    if not lock.acquire():
        # トレイダイアログを出すと余計に混乱するので、ログのみで終了
        return 2

    # v1.0: 同意ゲート（同意書なしではデータ収集を絶対に開始しない）
    if _HAS_CONSENT and ensure_consent_or_exit is not None:
        try:
            consented = ensure_consent_or_exit(
                customer_name=CUSTOMER_NAME or "",
                industry_profile=DEFAULT_PROFILE or "",
                upload_endpoint=UPLOAD_ENDPOINT or "",
            )
        except Exception:
            logger.exception("consent gate raised; treating as not-consented")
            consented = False
        if not consented:
            logger.warning("consent denied or canceled; exiting")
            lock.release()
            return 3

    # 設定
    try:
        config = load_config()
    except Exception:
        logger.exception("load_config failed; using defaults")
        from config import CollectorConfig
        config = CollectorConfig()

    # Collector
    collector, _collector_thread = _start_collector(config)

    # v1.0: クラウドアップロードスケジューラ起動 (config 設定 or ビルド埋め込みエンドポイントが両方あれば)
    upload_endpoint = UPLOAD_ENDPOINT or ""
    upload_key = UPLOAD_API_KEY or ""
    upload_sched = None
    if _HAS_UPLOADER and getattr(config, "upload_enabled", False) and upload_endpoint and upload_key:
        try:
            upload_sched = UploadScheduler(
                endpoint=upload_endpoint,
                api_key=upload_key,
                interval_hours=getattr(config, "upload_interval_hours", 24.0),
                quiet_hours_only=getattr(config, "upload_quiet_hours_only", True),
                max_retry=getattr(config, "upload_max_retry", 5),
                max_archive_mb=getattr(config, "upload_max_archive_mb", 200),
            )
            upload_sched.start()
        except Exception:
            logger.exception("UploadScheduler start failed; continuing in USB mode")
            upload_sched = None

    # Tray (メインスレッド)
    from tray import Tray  # 遅延 import: PIL 依存で初期化が重い
    tray = Tray(collector=collector, config=config)

    # シグナルハンドラ
    def _shutdown(signum: int, _frame: Any) -> None:
        logger.info("signal received: %d", signum)
        try:
            tray.stop()
        except Exception:
            logger.exception("tray.stop failed in signal handler")

    try:
        signal.signal(signal.SIGINT, _shutdown)
    except (ValueError, OSError):
        pass
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _shutdown)
        except (ValueError, OSError):
            pass

    try:
        tray.run()  # ブロッキング
    except KeyboardInterrupt:
        logger.info("interrupted by keyboard")
    except Exception:
        logger.exception("tray.run crashed")
        return 1
    finally:
        try:
            tray.stop()
        except Exception:
            pass
        if upload_sched is not None:
            try:
                upload_sched.stop()
            except Exception:
                pass
        lock.release()
        logger.info("==== %s Collector exited ====", APP_NAME)

    return 0


if __name__ == "__main__":
    sys.exit(main())
