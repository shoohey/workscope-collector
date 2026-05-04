"""タスクトレイ常駐 UI.

可視性命: 薬剤師さんが「録画中」を一目で把握でき、いつでも一時停止・確認・終了できる。
隠してこっそり録るためのツールではない。

- 緑丸 = 録画中 (active)
- グレー丸 = 一時停止中 (paused)
- メニューから直近スクショ・データフォルダ・同意書を即時確認可能
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

try:
    import pystray  # type: ignore
    from pystray import Menu, MenuItem  # type: ignore
except Exception:  # pragma: no cover - Mac でも pip install 可能だが防御的に
    pystray = None  # type: ignore
    Menu = None  # type: ignore
    MenuItem = None  # type: ignore

import icons
from config import (
    APP_NAME,
    app_data_dir,
    data_root,
    pause_flag_file,
    screenshots_dir,
)
from version import __version__

if TYPE_CHECKING:  # 循環 import 回避
    from collector import Collector  # noqa: F401
    from config import CollectorConfig  # noqa: F401


logger = logging.getLogger(__name__)


# ---- 同梱ドキュメントのパス解決 -----------------------------------------------

def bundled_doc_path(name: str) -> Path:
    """同梱ドキュメント (consent_form.html 等) の絶対パスを返す.

    PyInstaller --onefile 実行時は ``sys._MEIPASS`` の展開先、
    開発時は ``<repo>/docs/`` を参照する。存在チェックは行わない。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        # PyInstaller spec の datas で docs/ を同梱する想定
        return Path(meipass) / "docs" / name
    return Path(__file__).resolve().parent.parent / "docs" / name


# ---- 補助 ---------------------------------------------------------------------

def _open_with_os(path: Path) -> None:
    """OS 標準のビューア / エクスプローラで開く.

    Windows: ``os.startfile``
    macOS:   ``open`` コマンド
    Linux:   ``xdg-open`` コマンド
    """
    p = str(path)
    try:
        if sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", p])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", p])
    except Exception:
        logger.exception("failed to open: %s", p)


def _format_session_time(start_ts: Optional[float]) -> str:
    if start_ts is None:
        return "--:--"
    elapsed = max(0, int(time.time() - start_ts))
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _short_ts_label(iso_or_filename: str) -> str:
    """ファイル名 ``YYYY-MM-DDTHH-MM-SS-mmm_xxxxxxxx.jpg`` から表示用ラベルを作る."""
    base = iso_or_filename.split("_", 1)[0]
    # YYYY-MM-DDTHH-MM-SS-mmm
    try:
        dt = datetime.strptime(base[:19], "%Y-%m-%dT%H-%M-%S")
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return base[:19]


# ---- メッセージボックス -------------------------------------------------------

def _show_about_dialog() -> None:
    """About ダイアログ. Windows は MessageBox、その他は logger に出すだけ."""
    msg_lines = [
        f"{APP_NAME} Collector v{__version__}",
        "",
        "薬局業務の可視化のためのバックグラウンド記録ツールです。",
        "",
        f"データ保存先: {data_root()}",
        f"設定ファイル: {app_data_dir() / 'config.json'}",
        "",
        "問い合わせ: support@tribe.example",  # TODO: 正式な連絡先
    ]
    msg = "\n".join(msg_lines)
    if sys.platform == "win32":
        try:
            import ctypes
            MB_ICONINFORMATION = 0x40
            ctypes.windll.user32.MessageBoxW(0, msg, f"{APP_NAME} について", MB_ICONINFORMATION)
            return
        except Exception:
            logger.exception("MessageBoxW failed; falling back to log")
    logger.info("About: %s", msg.replace("\n", " | "))


def _confirm_quit() -> bool:
    """終了確認. Windows は MessageBox の OK/Cancel、その他は True を返す."""
    if sys.platform == "win32":
        try:
            import ctypes
            MB_OKCANCEL = 0x01
            MB_ICONQUESTION = 0x20
            IDOK = 1
            ret = ctypes.windll.user32.MessageBoxW(
                0,
                f"{APP_NAME} Collector を完全に終了しますか？\n\n録画は停止します。",
                f"{APP_NAME} の終了確認",
                MB_OKCANCEL | MB_ICONQUESTION,
            )
            return ret == IDOK
        except Exception:
            logger.exception("MessageBoxW failed; treating as confirmed")
    return True


# ---- Tray 本体 ---------------------------------------------------------------

class Tray:
    """タスクトレイ常駐 UI 本体.

    ``Collector`` と ``EventStore`` への参照を弱く保ち、3 秒ごとに状態を更新する。
    pystray はメインスレッドで ``run()`` する必要があるため、
    ``run()`` の呼び出し元 (``main.py``) がメインスレッドであること。
    """

    POLL_INTERVAL_SEC = 3.0

    def __init__(
        self,
        collector: "Collector | None" = None,
        config: "CollectorConfig | None" = None,
        get_stats_fn: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> None:
        self._collector = collector
        self._config = config
        self._session_start = time.time()

        # storage.get_stats を遅延 import（テスト時の差し替え可能）
        if get_stats_fn is None:
            try:
                from storage import get_stats as _real_get_stats  # type: ignore
                self._get_stats: Callable[[], dict[str, Any]] = _real_get_stats
            except Exception:
                logger.exception("storage.get_stats import failed; using stub")
                self._get_stats = lambda: {"today_event_count": 0, "recent": []}
        else:
            self._get_stats = get_stats_fn

        self._stats: dict[str, Any] = {"today_event_count": 0, "recent": []}
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._icon: Any = None  # pystray.Icon
        self._first_balloon_shown = False

    # ---- pause 状態 ----------------------------------------------------------
    @staticmethod
    def is_paused() -> bool:
        return pause_flag_file().exists()

    def _toggle_pause(self) -> None:
        flag = pause_flag_file()
        if flag.exists():
            try:
                flag.unlink()
                logger.info("resumed (pause flag removed)")
            except OSError:
                logger.exception("failed to remove pause flag")
        else:
            try:
                flag.write_text(datetime.now().isoformat(), encoding="utf-8")
                logger.info("paused (pause flag created)")
            except OSError:
                logger.exception("failed to create pause flag")
        self._refresh_icon()

    # ---- メニュー構築 --------------------------------------------------------
    def _status_text(self) -> str:
        if self.is_paused():
            return "一時停止中"
        cnt = self._stats.get("today_event_count", 0)
        sess = _format_session_time(self._session_start)
        return f"録画中（イベント数: {cnt} / セッション時間: {sess}）"

    def _pause_label(self, _item: Any = None) -> str:
        return "再開" if self.is_paused() else "一時停止"

    def _build_recent_submenu(self) -> Any:
        """直近スクショのサブメニューを毎回再生成する (動的)."""
        recent_items: list[Any] = []
        try:
            sd = screenshots_dir()
            files = sorted(sd.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
        except OSError:
            files = []

        if not files:
            recent_items.append(MenuItem("（まだスクショがありません）", None, enabled=False))
        else:
            for p in files:
                label = f"{_short_ts_label(p.name)}  {p.name}"
                # path をデフォルト引数で束縛して late binding 回避
                recent_items.append(
                    MenuItem(
                        label,
                        lambda _icon, _item, path=p: _open_with_os(path),
                    )
                )
        return Menu(*recent_items)

    def _build_menu(self) -> Any:
        if Menu is None or MenuItem is None:
            return None

        return Menu(
            MenuItem(lambda _i: self._status_text(), None, enabled=False),
            Menu.SEPARATOR,
            MenuItem(
                lambda _i: self._pause_label(),
                lambda _icon, _item: self._toggle_pause(),
            ),
            MenuItem(
                "直近のスクショを確認",
                self._build_recent_submenu(),
            ),
            Menu.SEPARATOR,
            MenuItem(
                "データフォルダを開く",
                lambda _icon, _item: _open_with_os(data_root()),
            ),
            MenuItem(
                "設定ファイルを開く",
                lambda _icon, _item: _open_with_os(app_data_dir() / "config.json"),
            ),
            MenuItem(
                "同意書を表示",
                lambda _icon, _item: _open_with_os(bundled_doc_path("consent_form.html")),
            ),
            MenuItem(
                "このツールについて",
                lambda _icon, _item: _show_about_dialog(),
            ),
            Menu.SEPARATOR,
            MenuItem(
                "完全に終了",
                lambda _icon, _item: self._on_quit(),
            ),
        )

    # ---- アイコン更新 --------------------------------------------------------
    def _current_image(self) -> Any:
        return icons.paused_icon() if self.is_paused() else icons.active_icon()

    def _refresh_icon(self) -> None:
        if self._icon is None:
            return
        try:
            self._icon.icon = self._current_image()
            self._icon.title = f"{APP_NAME} Collector — {self._status_text()}"
            # メニューを再構築してサブメニュー (直近スクショ) も更新
            self._icon.menu = self._build_menu()
        except Exception:
            logger.exception("refresh_icon failed")

    # ---- ポーリングスレッド --------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._stats = self._get_stats() or {}
            except Exception:
                logger.exception("get_stats failed")
            self._refresh_icon()
            self._stop_event.wait(self.POLL_INTERVAL_SEC)

    # ---- 終了 ----------------------------------------------------------------
    def _on_quit(self) -> None:
        if not _confirm_quit():
            return
        logger.info("user requested quit from tray")
        self.stop()

    def stop(self) -> None:
        """Collector を止め、トレイをクローズする."""
        self._stop_event.set()
        if self._collector is not None:
            try:
                self._collector.stop()
            except Exception:
                logger.exception("collector.stop failed")
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                logger.exception("tray icon.stop failed")

    # ---- 起動 ----------------------------------------------------------------
    def run(self) -> None:
        """メインスレッドで pystray を起動する (ブロッキング)."""
        if pystray is None:
            logger.error("pystray is not available; tray UI cannot start")
            return

        # Windows 以外でも起動はするが、トースト通知などは Windows 固有。
        if sys.platform != "win32":
            logger.warning(
                "pystray is running on non-Windows (%s). "
                "本番動作は Windows のみサポート。", sys.platform,
            )

        self._icon = pystray.Icon(
            name=APP_NAME,
            icon=self._current_image(),
            title=f"{APP_NAME} Collector — {self._status_text()}",
            menu=self._build_menu(),
        )

        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="tray-poll", daemon=True,
        )
        self._poll_thread.start()

        def _on_ready(icon: Any) -> None:
            icon.visible = True
            if not self._first_balloon_shown:
                self._first_balloon_shown = True
                if sys.platform == "win32":
                    try:
                        icon.notify(
                            f"{APP_NAME} Collector が起動しました。録画中です。",
                            f"{APP_NAME}",
                        )
                    except Exception:
                        logger.exception("balloon notify failed")

        try:
            self._icon.run(setup=_on_ready)
        finally:
            self._stop_event.set()


__all__ = ["Tray", "bundled_doc_path"]
