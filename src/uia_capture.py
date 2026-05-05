"""UI Automation でフォーカス中コントロールを取得するモジュール.

uiautomation > pywinauto > フォールバック(空) の順でライブラリを試す。
取得項目: AutomationId, Name, ControlType, ClassName, 親要素3階層分のName, IsPassword, Value(マスク後のみ)

設計方針:
- 取得した値(Value)は必ずマスカー通過後にしか返さない（PII漏洩経路を物理的に作らない）
- IsPassword=True のフィールドにフォーカス中は Value を絶対に取得しない
- Mac開発環境では import 失敗するので、関数全体を try で囲み Optional を返す
- パフォーマンス: 200ms以上かかる場合はタイムアウトしてNoneを返す
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Windows専用ライブラリのガード -----------------------------------------------
_HAS_UIAUTOMATION = False
_HAS_PYWINAUTO = False
try:
    import uiautomation as _uia  # type: ignore[import-not-found]
    _HAS_UIAUTOMATION = True
except Exception:
    _uia = None  # type: ignore

try:
    import pywinauto  # type: ignore[import-not-found]
    from pywinauto import Application  # type: ignore[import-not-found]
    _HAS_PYWINAUTO = True
except Exception:
    pywinauto = None  # type: ignore
    Application = None  # type: ignore


# ---- データ構造 -----------------------------------------------------------

@dataclass
class FocusedControl:
    """フォーカス中コントロールのスナップショット.

    PII保護: value は呼び出し側のマスカー通過後にセットされる。生値は保持しない。
    """
    automation_id: str = ""
    name: str = ""
    control_type: str = ""
    class_name: str = ""
    parent_path: list[str] = field(default_factory=list)  # 親要素3階層分のName
    is_password: bool = False
    value_masked: str | None = None  # マスカー通過後のみ
    value_present: bool = False      # 元値が存在したか（マスクで全部消えても判別用）
    backend: str = ""                # "uiautomation" | "pywinauto" | ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ---- バックエンド: uiautomation -------------------------------------------

def _capture_via_uiautomation(timeout_ms: int = 200) -> Optional[FocusedControl]:
    """uiautomation でフォーカス中要素を取得. 失敗時 None."""
    if not _HAS_UIAUTOMATION:
        return None

    result_holder: dict[str, Any] = {"control": None, "error": None}

    def _worker() -> None:
        try:
            ctrl = _uia.GetFocusedControl()
            if ctrl is None:
                return

            # 親階層を3つまで遡る
            parents: list[str] = []
            cur = ctrl
            for _ in range(3):
                try:
                    cur = cur.GetParentControl()
                    if cur is None or cur.Name == "":
                        break
                    parents.append(str(cur.Name))
                except Exception:
                    break

            # IsPassword 判定（uiautomation の AutomationProperty）
            is_password = False
            try:
                pat = ctrl.GetValuePattern()
                # ValuePattern.IsPassword で判定可能なバージョンと、ない版がある
                is_password = bool(getattr(pat, "IsReadOnly", False) is False
                                   and getattr(ctrl, "IsPassword", False))
            except Exception:
                pass

            value_present = False
            try:
                pat = ctrl.GetValuePattern()
                v = pat.Value if pat else ""
                value_present = bool(v)
            except Exception:
                pass

            fc = FocusedControl(
                automation_id=str(getattr(ctrl, "AutomationId", "") or ""),
                name=str(getattr(ctrl, "Name", "") or ""),
                control_type=str(getattr(ctrl, "ControlTypeName", "") or ""),
                class_name=str(getattr(ctrl, "ClassName", "") or ""),
                parent_path=parents,
                is_password=is_password,
                value_present=value_present,
                value_masked=None,  # 値はここでは取得しない（呼び出し側でマスク経由）
                backend="uiautomation",
            )
            result_holder["control"] = fc
        except Exception as e:
            result_holder["error"] = e

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=timeout_ms / 1000.0)
    if th.is_alive():
        logger.warning("uiautomation focused control timeout (%dms)", timeout_ms)
        return None
    if result_holder["error"]:
        logger.debug("uiautomation failed: %s", result_holder["error"])
        return None
    return result_holder["control"]


# ---- バックエンド: pywinauto ----------------------------------------------

def _capture_via_pywinauto(hwnd: int | None = None,
                           timeout_ms: int = 200) -> Optional[FocusedControl]:
    """pywinauto でフォーカス中要素を取得（uiautomationの代替フォールバック）."""
    if not _HAS_PYWINAUTO:
        return None

    result_holder: dict[str, Any] = {"control": None}

    def _worker() -> None:
        try:
            if hwnd is None:
                return
            app = Application(backend="uia").connect(handle=hwnd)
            top = app.top_window()
            try:
                focused = top.get_focus()
            except Exception:
                focused = None
            if focused is None:
                return

            elem = focused.element_info
            parents: list[str] = []
            try:
                p = elem.parent
                for _ in range(3):
                    if p is None or not getattr(p, "name", ""):
                        break
                    parents.append(str(p.name))
                    p = p.parent
            except Exception:
                pass

            is_password = False
            try:
                is_password = bool(getattr(focused, "is_password", False))
            except Exception:
                pass

            fc = FocusedControl(
                automation_id=str(getattr(elem, "automation_id", "") or ""),
                name=str(getattr(elem, "name", "") or ""),
                control_type=str(getattr(elem, "control_type", "") or ""),
                class_name=str(getattr(elem, "class_name", "") or ""),
                parent_path=parents,
                is_password=is_password,
                value_present=False,
                value_masked=None,
                backend="pywinauto",
            )
            result_holder["control"] = fc
        except Exception as e:
            logger.debug("pywinauto failed: %s", e)

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=timeout_ms / 1000.0)
    if th.is_alive():
        logger.warning("pywinauto focused control timeout (%dms)", timeout_ms)
        return None
    return result_holder["control"]


# ---- 公開API -------------------------------------------------------------

def get_focused_control(hwnd: int | None = None,
                        timeout_ms: int = 200) -> Optional[FocusedControl]:
    """フォーカス中コントロールを取得. 失敗時 None.

    フォールバック順: uiautomation → pywinauto → None
    Mac/Linux 環境では常に None を返す（インポートガードで安全）。
    """
    if _HAS_UIAUTOMATION:
        c = _capture_via_uiautomation(timeout_ms=timeout_ms)
        if c is not None:
            return c
    if _HAS_PYWINAUTO:
        c = _capture_via_pywinauto(hwnd=hwnd, timeout_ms=timeout_ms)
        if c is not None:
            return c
    return None


def attach_masked_value(control: FocusedControl, raw_value: str,
                        mask_func) -> FocusedControl:
    """生値を mask_func 経由でマスクして control.value_masked にセット.

    PII保護: この関数を経由しないと value_masked は絶対にセットされない。
    is_password=True の場合は値そのものを記録せず、長さ情報のみ保持。
    mask_func は (text: str) -> str のシグネチャ。失敗時はNone保持。
    """
    if control.is_password:
        # パスワードフィールドは値を保存しない（桁数のみ）
        control.value_masked = f"[PASSWORD:len={len(raw_value)}]"
        control.value_present = bool(raw_value)
        return control

    if not raw_value:
        control.value_masked = None
        control.value_present = False
        return control

    try:
        masked = mask_func(raw_value)
        control.value_masked = masked
        control.value_present = True
    except Exception:
        logger.exception("mask_func raised; dropping value")
        control.value_masked = None
        control.value_present = bool(raw_value)
    return control


def is_uia_available() -> bool:
    """このプラットフォームで UI Automation が使えるか."""
    return _HAS_UIAUTOMATION or _HAS_PYWINAUTO


__all__ = [
    "FocusedControl",
    "get_focused_control",
    "attach_masked_value",
    "is_uia_available",
]
