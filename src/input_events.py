"""キーストローク種別ロガー + マウスクリック対象推定.

PII保護の絶対不変条件:
- 文字キーは「桁数のみ」記録、値は絶対に保存しない
- IsPassword=True のフィールドにフォーカス中は **キーロギング自体を停止**
- ナビゲーションキー(Tab/Enter/Esc/Fキー/Ctrl+組み合わせ等)は個別記録
- マウスクリックは座標 + 直近OCRボックスから対象テキスト推定（テキストはマスク後）

Mac開発環境では keyboard/pynput が無くても動くようガード。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---- ライブラリガード -----------------------------------------------------
_HAS_KEYBOARD = False
_HAS_PYNPUT = False
try:
    import keyboard as _keyboard  # type: ignore[import-not-found]
    _HAS_KEYBOARD = True
except Exception:
    _keyboard = None  # type: ignore

try:
    from pynput import keyboard as _pynput_kb, mouse as _pynput_mouse  # type: ignore[import-not-found]
    _HAS_PYNPUT = True
except Exception:
    _pynput_kb = None  # type: ignore
    _pynput_mouse = None  # type: ignore


# ---- ナビゲーションキー定義 -----------------------------------------------
# これらは「種別」として個別記録する。それ以外の文字キーは桁数のみ。
NAV_KEYS = frozenset([
    "tab", "enter", "return", "esc", "escape", "backspace", "delete",
    "home", "end", "page up", "page down", "pageup", "pagedown",
    "up", "down", "left", "right",
    "insert", "ins",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
    "caps lock", "capslock", "num lock", "numlock", "scroll lock",
    "print screen", "printscreen", "pause", "menu",
])

MODIFIER_KEYS = frozenset([
    "ctrl", "control", "shift", "alt", "win", "windows", "cmd", "command",
])


# ---- データ構造 -----------------------------------------------------------

@dataclass
class KeyEvent:
    """キーボードイベント（種別記録のみ）."""
    ts: float                    # event time (UNIX)
    event_type: str              # "key_typed" or "key_combo"
    key_name: str = ""           # ナビゲーションキー名 (Tab/Enter等) or "" (文字キー)
    text_keys_count: int = 0     # 文字キー入力の累積桁数（リセット = ナビキー入力時）
    modifiers: list[str] = field(default_factory=list)  # ["Ctrl", "Shift"] etc

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "event_type": self.event_type,
            "key_name": self.key_name,
            "text_keys_count": self.text_keys_count,
            "modifiers": list(self.modifiers),
        }


@dataclass
class MouseEvent:
    """マウスクリックイベント."""
    ts: float
    event_type: str              # "mouse_click"
    button: str                  # "left" / "right" / "middle"
    coords: tuple[int, int]      # (x, y) スクリーン座標
    target_text_masked: str | None = None   # 直近OCRボックスから推定した対象（マスク後）

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "event_type": self.event_type,
            "button": self.button,
            "coords": list(self.coords),
            "target_text_masked": self.target_text_masked,
        }


# ---- キーロガー本体 ------------------------------------------------------

class InputEventLogger:
    """キーボード+マウスをフックして KeyEvent / MouseEvent を生成.

    PII保護:
    - is_password_field_active() が True を返す間はキーロギング停止
    - 文字キーは桁数カウントのみ、値は記録しない
    - ナビゲーションキーは個別記録
    """

    def __init__(
        self,
        on_key: Callable[[KeyEvent], None],
        on_mouse: Callable[[MouseEvent], None],
        is_password_field_active: Callable[[], bool] = lambda: False,
        flush_text_count_after_seconds: float = 2.0,
    ) -> None:
        self._on_key = on_key
        self._on_mouse = on_mouse
        self._is_password = is_password_field_active
        self._flush_after = flush_text_count_after_seconds

        self._lock = threading.Lock()
        self._text_keys_count = 0
        self._last_text_key_ts: float = 0.0
        self._modifiers_held: set[str] = set()
        self._stop = threading.Event()
        self._kb_thread: threading.Thread | None = None
        self._mouse_listener: object | None = None
        self._kb_listener: object | None = None

    # --- 公開 -----------------------------------------------------------
    @property
    def available(self) -> bool:
        return _HAS_PYNPUT or _HAS_KEYBOARD

    def start(self) -> None:
        """フックを開始. ライブラリがない環境では何もしない（安全）."""
        if self._kb_listener is not None or self._mouse_listener is not None:
            return
        if _HAS_PYNPUT:
            self._start_pynput()
        elif _HAS_KEYBOARD:
            self._start_keyboard()
        else:
            logger.warning("InputEventLogger: no keyboard/pynput available; noop")
        # Codex High#1: 文字キー桁数の定期 flush スレッドを起動
        # 文字入力後にナビキーを押さなくても、_flush_after 秒経過で flush する
        if self._kb_thread is None and (_HAS_PYNPUT or _HAS_KEYBOARD):
            self._kb_thread = threading.Thread(
                target=self._flush_loop, name="ws-input-flush", daemon=True
            )
            self._kb_thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._kb_listener is not None:
                self._kb_listener.stop()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            if self._mouse_listener is not None:
                self._mouse_listener.stop()  # type: ignore[attr-defined]
        except Exception:
            pass
        self._kb_listener = None
        self._mouse_listener = None
        self._flush_text_count(force=True)
        # join: スレッドが flush_after 秒以内に終了する想定
        if self._kb_thread is not None:
            try:
                self._kb_thread.join(timeout=max(self._flush_after * 2, 2.0))
            except Exception:
                pass
            self._kb_thread = None

    def _flush_loop(self) -> None:
        """定期 flush ループ: _last_text_key_ts から _flush_after 秒経過したら flush."""
        check_interval = max(0.5, self._flush_after / 2.0)
        while not self._stop.wait(check_interval):
            with self._lock:
                last_ts = self._last_text_key_ts
                count = self._text_keys_count
            if count > 0 and last_ts > 0 and (time.time() - last_ts) >= self._flush_after:
                self._flush_text_count()

    # --- バックエンド: pynput ------------------------------------------
    def _start_pynput(self) -> None:
        def _on_press(key) -> None:  # type: ignore[no-untyped-def]
            self._handle_key_pynput(key, pressed=True)

        def _on_release(key) -> None:  # type: ignore[no-untyped-def]
            self._handle_key_pynput(key, pressed=False)

        def _on_click(x: int, y: int, button, pressed: bool) -> None:  # type: ignore[no-untyped-def]
            if not pressed:
                return
            if self._is_password():
                return
            btn_name = str(button).split(".")[-1].lower()
            self._on_mouse(MouseEvent(
                ts=time.time(),
                event_type="mouse_click",
                button=btn_name,
                coords=(int(x), int(y)),
            ))

        self._kb_listener = _pynput_kb.Listener(
            on_press=_on_press, on_release=_on_release
        )
        self._mouse_listener = _pynput_mouse.Listener(on_click=_on_click)
        self._kb_listener.start()  # type: ignore[attr-defined]
        self._mouse_listener.start()  # type: ignore[attr-defined]

    def _handle_key_pynput(self, key, pressed: bool) -> None:  # type: ignore[no-untyped-def]
        if self._is_password():
            return  # PII保護: パスワードフィールド中は完全停止

        key_str = self._key_to_str_pynput(key)
        is_modifier = key_str in MODIFIER_KEYS

        if pressed:
            if is_modifier:
                self._modifiers_held.add(key_str)
                return
            if key_str in NAV_KEYS or self._modifiers_held:
                # ナビキー or 修飾キー組み合わせ → 個別記録（前回までの文字数も flush）
                self._flush_text_count()
                event_type = "key_combo" if self._modifiers_held else "key_typed"
                self._on_key(KeyEvent(
                    ts=time.time(),
                    event_type=event_type,
                    key_name=key_str,
                    text_keys_count=0,
                    modifiers=sorted(self._modifiers_held),
                ))
            else:
                # 文字キー: 桁数カウント、値は記録しない
                with self._lock:
                    self._text_keys_count += 1
                    self._last_text_key_ts = time.time()
        else:
            if is_modifier:
                self._modifiers_held.discard(key_str)

    @staticmethod
    def _key_to_str_pynput(key) -> str:  # type: ignore[no-untyped-def]
        try:
            if hasattr(key, "name") and key.name:
                return str(key.name).lower()
            if hasattr(key, "char") and key.char:
                return str(key.char)
        except Exception:
            pass
        return str(key).lower()

    # --- バックエンド: keyboard ----------------------------------------
    def _start_keyboard(self) -> None:
        # keyboard ライブラリは Windows でも root/admin 権限が必要なので、
        # フォールバック扱い。基本は pynput を使う。
        logger.info("InputEventLogger: using 'keyboard' backend (fallback)")

    # --- 文字キーカウント flush ----------------------------------------
    def _flush_text_count(self, force: bool = False) -> None:
        """累積した text_keys_count を1イベントとして発行."""
        with self._lock:
            count = self._text_keys_count
            self._text_keys_count = 0
        if count <= 0:
            return
        self._on_key(KeyEvent(
            ts=time.time(),
            event_type="key_typed",
            key_name="",  # 文字キーバッチ
            text_keys_count=count,
            modifiers=[],
        ))


# ---- click resolver: クリック座標 → 対象テキスト推定 ----------------------

def resolve_click_target(
    coords: tuple[int, int],
    ocr_boxes: list,
    mask_func: Callable[[str], str] | None = None,
) -> Optional[str]:
    """クリック座標と直近OCRボックスから対象テキスト（マスク後）を推定.

    OCRボックスは masker.OCRBox 互換（bbox=(x1,y1,x2,y2), text=str）を期待。
    マスカーが指定されればマスク済みテキストを返す（PII保護）。
    マッチなしは None。
    """
    x, y = coords
    # 1. 矩形の中にある box を最優先
    inside: list[tuple[float, str]] = []
    for box in ocr_boxes:
        bb = getattr(box, "bbox", None)
        if not bb or len(bb) != 4:
            continue
        x1, y1, x2, y2 = bb
        text = getattr(box, "text", "") or ""
        if x1 <= x <= x2 and y1 <= y <= y2:
            # 中央距離で最も近い box を選ぶため距離を補助に保持
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            inside.append((dist, text))
    if inside:
        inside.sort(key=lambda t: t[0])
        chosen = inside[0][1]
    else:
        # 2. 含まれる box が無い場合、80px以内で最も近い box
        # （マウスクリックは数十px単位でズレることが多いので許容範囲を広めに取る）
        nearest: tuple[float, str] | None = None
        for box in ocr_boxes:
            bb = getattr(box, "bbox", None)
            if not bb or len(bb) != 4:
                continue
            x1, y1, x2, y2 = bb
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if dist <= 80 and (nearest is None or dist < nearest[0]):
                nearest = (dist, getattr(box, "text", "") or "")
        if nearest is None:
            return None
        chosen = nearest[1]

    if mask_func is None:
        return chosen
    try:
        return mask_func(chosen)
    except Exception:
        logger.exception("mask_func raised in resolve_click_target; dropping text")
        return None


__all__ = [
    "KeyEvent",
    "MouseEvent",
    "InputEventLogger",
    "NAV_KEYS",
    "MODIFIER_KEYS",
    "resolve_click_target",
]
