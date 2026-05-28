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

# v1.1-lite: 収集モード（"lite" のときはスクショ系メニューを隠す）
try:
    from config import COLLECTION_MODE as _COLLECTION_MODE
except Exception:
    _COLLECTION_MODE = "full"

if TYPE_CHECKING:  # 循環 import 回避
    from collector import Collector  # noqa: F401
    from config import CollectorConfig  # noqa: F401


logger = logging.getLogger(__name__)


# ---- 同梱ドキュメントのパス解決 -----------------------------------------------

def bundled_doc_path(name: str) -> Path:
    """同梱ドキュメント (consent_form.html 等) の絶対パスを返す.

    探索順:
      1. PyInstaller --onefile 展開先 ``sys._MEIPASS/docs/<name>``
      2. インストーラがコピーした ``%APPDATA%/WorkScope/docs/<name>``
      3. リポジトリの ``<repo>/docs/<name>``（開発時）
    """
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "docs" / name)
    candidates.append(app_data_dir() / "docs" / name)
    candidates.append(Path(__file__).resolve().parent.parent / "docs" / name)
    for p in candidates:
        try:
            if p.exists():
                return p
        except OSError:
            continue
    # 見つからなくても呼び出し元の "open" にエラーを任せるため最初の候補を返す
    return candidates[0]


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


def _make_open_action(path: Path) -> Callable[[Any, Any], None]:
    """pystray の MenuItem の action 用 closure factory.

    なぜ factory か:
        pystray の ``_assert_action`` がデフォルト引数付き lambda を
        ``ValueError`` で拒否する（lambda は callable のはずだが、内部の
        signature 検査で引数数が action 規約 ``(icon, item)`` と一致しない
        ため）。一旦 named local function に包んで closure 化することで
        引数数を厳密に 2 にして拒否回避する。

    現地で 2026-05-15 に発覚した tray.run クラッシュループの恒久対策。
    """
    def _action(_icon: Any, _item: Any) -> None:
        _open_with_os(path)
    return _action


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
    return _windows_confirm(
        f"{APP_NAME} Collector を完全に終了しますか？\n\n録画は停止します。",
        f"{APP_NAME} の終了確認",
    )


# Windows MessageBox フラグ定数
# pystray のメニューコールバック内から MessageBox を呼ぶと、Z order の問題で
# トレイメニュー or 他のウィンドウの背面に隠れてしまう既知の問題があるため、
# MB_TOPMOST + MB_SETFOREGROUND で必ず最前面に出す.
_MB_OK = 0x00
_MB_OKCANCEL = 0x01
_MB_ICONQUESTION = 0x20
_MB_ICONINFORMATION = 0x40
_MB_TOPMOST = 0x40000       # 最前面表示 (z-order)
_MB_SETFOREGROUND = 0x10000  # フォアグラウンド化
_IDOK = 1


def _windows_confirm(message: str, title: str) -> bool:
    """Windows MessageBox の OK/Cancel 確認. 非Windowsは True 返却.

    背面に隠れて消えない問題対策に MB_TOPMOST + MB_SETFOREGROUND を必ず付与。
    """
    if sys.platform == "win32":
        try:
            import ctypes
            ret = ctypes.windll.user32.MessageBoxW(
                0, message, title,
                _MB_OKCANCEL | _MB_ICONQUESTION | _MB_TOPMOST | _MB_SETFOREGROUND,
            )
            return ret == _IDOK
        except Exception:
            logger.exception("MessageBoxW failed; treating as confirmed")
    return True


def _windows_info(message: str, title: str) -> None:
    """Windows MessageBox の情報表示. 非Windowsはログ出力.

    背面に隠れて消えない問題対策に MB_TOPMOST + MB_SETFOREGROUND を必ず付与。
    """
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, message, title,
                _MB_OK | _MB_ICONINFORMATION | _MB_TOPMOST | _MB_SETFOREGROUND,
            )
            return
        except Exception:
            logger.exception("MessageBoxW(info) failed")
    logger.info("info dialog: [%s] %s", title, message.replace("\n", " | "))


# ---- v1.0: 同意撤回・データ削除・手動送信 -------------------------------------

def _on_revoke_consent(tray: "Tray") -> None:
    """同意撤回: consent_signed.json 削除 → アプリ終了."""
    if not _windows_confirm(
        f"{APP_NAME} の利用同意を撤回しますか？\n\n"
        "撤回後はアプリが終了し、次回起動時に再度同意が必要です。\n"
        "撤回を取り消すには、収集データの削除も別途必要です。",
        f"{APP_NAME} 同意撤回の確認",
    ):
        return
    try:
        from consent import revoke_consent  # type: ignore
        revoke_consent()
        _windows_info(
            "同意を撤回しました。アプリを終了します。",
            f"{APP_NAME} 同意撤回",
        )
    except Exception:
        logger.exception("revoke_consent failed")
    tray.stop()


def _on_delete_local_data(tray: "Tray") -> None:
    """ローカル収集データを全削除. screenshots/events/uploaded_markers."""
    if not _windows_confirm(
        f"{APP_NAME} がこの端末に保存した収集データを全て削除しますか？\n\n"
        "・マスク済みスクリーンショット (data/screenshots/)\n"
        "・イベントログ (data/events/)\n"
        "・アップロード済みマーカー (uploaded_markers/)\n"
        "は完全に消去されます。\n"
        "（同意状態とアプリ設定は保持されます）",
        f"{APP_NAME} データ削除の確認",
    ):
        return
    deleted = 0
    try:
        for sub in ("data/screenshots", "data/events", "uploaded_markers"):
            target = app_data_dir() / sub
            if not target.exists():
                continue
            for p in target.iterdir():
                try:
                    if p.is_file():
                        p.unlink()
                        deleted += 1
                except OSError:
                    logger.exception("failed to unlink %s", p)
    except Exception:
        logger.exception("delete_local_data failed")
    _windows_info(
        f"収集データ {deleted} 件を削除しました。",
        f"{APP_NAME} データ削除",
    )


def _on_upload_now(tray: "Tray") -> None:
    """手動アップロード. UploadScheduler 経由で 1回 upload_once 実行."""
    sched = getattr(tray, "_upload_scheduler", None)
    if sched is None or not getattr(sched, "configured", False):
        _windows_info(
            f"{APP_NAME} はクラウドアップロードが無効化されています。\n"
            "（USB回収モード）",
            f"{APP_NAME} アップロード",
        )
        return
    if not _windows_confirm(
        "今すぐクラウドへ送信しますか？\n\n"
        "送信前にPII漏洩スキャンが実行され、検出時は自動的に送信中止します。",
        f"{APP_NAME} 手動アップロード",
    ):
        return
    try:
        ok = sched.trigger_now()
        msg = "送信が完了しました。" if ok else (
            "送信に失敗しました。\n"
            "- ネットワーク接続を確認してください\n"
            "- ログ (logs/main.log) で詳細を確認できます"
        )
    except Exception as e:
        msg = f"送信中にエラーが発生しました: {e}"
    _windows_info(msg, f"{APP_NAME} 手動アップロード")


def _on_show_status(tray: "Tray") -> None:
    """収集ステータス（イベント数・最終送信・データ量）を表示."""
    try:
        from storage import get_stats  # type: ignore
        stats = get_stats() or {}
    except Exception:
        stats = {}
    today_count = stats.get("today_event_count", 0)
    bytes_total = stats.get("bytes_total", 0)
    bytes_mb = bytes_total / (1024 * 1024) if bytes_total else 0

    # アップロード状態
    last_upload = "未送信 (USB回収モード)"
    sched = getattr(tray, "_upload_scheduler", None)
    if sched is not None and getattr(sched, "configured", False):
        marker_dir = app_data_dir() / "uploaded_markers"
        if marker_dir.exists():
            markers = list(marker_dir.glob("*.uploaded"))
            if markers:
                latest = max(markers, key=lambda p: p.stat().st_mtime)
                ts = datetime.fromtimestamp(latest.stat().st_mtime)
                last_upload = f"{ts.strftime('%Y-%m-%d %H:%M')} ({len(markers)} ファイル送信済)"
            else:
                last_upload = "未送信 (送信予定あり)"

    msg = (
        f"{APP_NAME} Collector v{__version__}\n\n"
        f"・本日のイベント数: {today_count}\n"
        f"・累計データ量: {bytes_mb:.1f} MB\n"
        f"・最終アップロード: {last_upload}\n"
        f"・状態: {'一時停止中' if Tray.is_paused() else '録画中'}"
    )
    _windows_info(msg, f"{APP_NAME} 収集状況")


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
        upload_scheduler: Any = None,  # v1.0: UploadScheduler 参照（手動送信用）
    ) -> None:
        self._collector = collector
        self._config = config
        self._upload_scheduler = upload_scheduler
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
                recent_items.append(MenuItem(label, _make_open_action(p)))
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
                # v1.1-lite: liteモードはスクショ取得しないので非表示
                visible=lambda _i: _COLLECTION_MODE != "lite",
            ),
            MenuItem(
                "収集状況を表示",
                lambda _icon, _item: threading.Thread(
                    target=_on_show_status, args=(self,), daemon=True).start(),
            ),
            Menu.SEPARATOR,
            # v1.0: クラウドアップロード関連
            MenuItem(
                "今すぐクラウドへ送信",
                lambda _icon, _item: threading.Thread(
                    target=_on_upload_now, args=(self,), daemon=True).start(),
                visible=lambda _i: self._upload_scheduler is not None
                and getattr(self._upload_scheduler, "configured", False),
            ),
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
            # v1.0: 撤回・データ削除（PII保護の権利行使）
            MenuItem(
                "収集データを削除",
                lambda _icon, _item: threading.Thread(
                    target=_on_delete_local_data, args=(self,), daemon=True).start(),
            ),
            MenuItem(
                "同意を撤回して終了",
                lambda _icon, _item: threading.Thread(
                    target=_on_revoke_consent, args=(self,), daemon=True).start(),
            ),
            MenuItem(
                "完全に終了",
                lambda _icon, _item: threading.Thread(
                    target=self._on_quit, daemon=True).start(),
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
