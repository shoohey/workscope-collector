"""マルチモニター・キャプチャのテスト (v1.0.1).

検証項目:
- ``capture_all_monitors`` が全物理モニターを順番に返す
- フォーカス側モニターが戻り値の先頭に並ぶ
- ``Collector._handle`` が複数モニター時に
  ``event["screenshot"]`` + ``event["additional_screenshots"][]`` の両方を出す
- 各モニターのスクショファイル名に ``_mon{N}`` サフィックスが付く
- 各モニターで OCR→マスクが独立に走る（一方のPIIが他方に漏れない）
- シングルモニター環境では ``additional_screenshots=[]`` で従来挙動
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _isolate_appdata(tmp: Path) -> None:
    os.environ["APPDATA"] = str(tmp)
    for m in ("storage", "collector", "config", "window_titles", "masker", "ocr",
              "profile_loader", "app_classifier", "uia_capture", "input_events"):
        sys.modules.pop(m, None)


class _StubOCR:
    def __init__(self, boxes_per_call: list[list]) -> None:
        # 呼び出しごとに別の OCR 結果を返す（モニターごとに異なるテキストを擬似化）
        self._queue = list(boxes_per_call)

    def extract(self, _image) -> list:
        if not self._queue:
            return []
        return list(self._queue.pop(0))


def _white(w: int = 600, h: int = 400, color=(255, 255, 255)) -> Image.Image:
    return Image.new("RGB", (w, h), color)


class _FakeShot:
    """mss.mss().grab() の戻り値を模した最小オブジェクト."""
    def __init__(self, width: int, height: int, color=(255, 255, 255)) -> None:
        self.size = (width, height)
        # mss は raw RGB バイト列 (BGRA→RGB変換済) を rgb 属性に持つ
        img = Image.new("RGB", (width, height), color)
        self.rgb = img.tobytes()


class _FakeMssContext:
    """``with mss.mss() as sct:`` の文脈マネージャを模す."""

    def __init__(self, monitors: list[dict[str, int]], grab_colors: dict[int, tuple]) -> None:
        # monitors[0] = 全体結合仮想、[1..] = 物理モニター
        self.monitors = monitors
        self._grab_colors = grab_colors

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def grab(self, mon: dict[str, int]):
        # mon は monitors リストのいずれか。対応する index を逆引き
        for idx, m in enumerate(self.monitors):
            if m is mon:
                color = self._grab_colors.get(idx, (255, 255, 255))
                return _FakeShot(int(m["width"]), int(m["height"]), color)
        return _FakeShot(int(mon["width"]), int(mon["height"]))


def _install_fake_mss(collector_mod, monitors: list[dict[str, int]], grab_colors: dict[int, tuple]):
    """collector モジュールの mss を fake に差し替えて返す."""
    fake_mss_mod = types.SimpleNamespace(
        mss=lambda: _FakeMssContext(monitors, grab_colors),
    )
    collector_mod.mss = fake_mss_mod
    collector_mod._HAS_MSS = True
    return fake_mss_mod


@pytest.fixture()
def isolated_env(tmp_path):
    _isolate_appdata(tmp_path)
    yield tmp_path


def _make_collector(stub_boxes: list[list], **cfg):
    import config as cfg_mod  # type: ignore
    import collector as collector_mod  # type: ignore
    base = {
        "min_dwell_seconds_for_capture": 0.0,
        "max_capture_per_minute": 120,
        # マルチモニター挙動を有効化（v1.0.1 デフォルト）
        "capture_active_monitor_only": False,
    }
    base.update(cfg)
    return collector_mod, collector_mod.Collector(
        cfg=cfg_mod.CollectorConfig(**base),
        ocr_engine=_StubOCR(stub_boxes),
    )


def _info(collector_mod, monitor: int = 1, hwnd: int = 1):
    return collector_mod.WindowInfo(
        hwnd=hwnd, title="処方せん入力", process_name="ReceptyNEXT.exe",
        process_path="C:\\ReceptyNEXT.exe", pid=999,
        rect=(0, 0, 1920, 1080), monitor=monitor,
    )


# ============================================================================
# 1. capture_all_monitors 単体
# ============================================================================

def test_capture_all_monitors_returns_one_per_physical_monitor(isolated_env, monkeypatch):
    import collector as collector_mod  # type: ignore

    monitors = [
        {"left": 0, "top": 0, "width": 3840, "height": 1080},  # [0] 結合仮想
        {"left": 0, "top": 0, "width": 1920, "height": 1080},  # [1] ノートPC内蔵
        {"left": 1920, "top": 0, "width": 1920, "height": 1080},  # [2] 外部
    ]
    _install_fake_mss(collector_mod, monitors, grab_colors={1: (10, 10, 10), 2: (200, 200, 200)})

    info = _info(collector_mod, monitor=1)
    frames = collector_mod.capture_all_monitors(info)

    assert len(frames) == 2
    indices = [idx for idx, _ in frames]
    assert set(indices) == {1, 2}
    # フォーカス側 (monitor=1) が先頭
    assert indices[0] == 1


def test_capture_all_monitors_focus_on_external_puts_external_first(isolated_env, monkeypatch):
    import collector as collector_mod  # type: ignore

    monitors = [
        {"left": 0, "top": 0, "width": 3840, "height": 1080},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 1920, "top": 0, "width": 1920, "height": 1080},
    ]
    _install_fake_mss(collector_mod, monitors, grab_colors={})

    info = _info(collector_mod, monitor=2)  # 外部側にフォーカス
    frames = collector_mod.capture_all_monitors(info)

    assert len(frames) == 2
    assert frames[0][0] == 2  # フォーカス側が先頭
    assert frames[1][0] == 1


def test_capture_all_monitors_fallback_to_capture_active_when_no_mss(isolated_env, monkeypatch):
    """mss 不在環境 (Mac/テスト) では capture_active 経由でシングル要素を返す."""
    import collector as collector_mod  # type: ignore

    monkeypatch.setattr(collector_mod, "_HAS_MSS", False)
    monkeypatch.setattr(collector_mod, "capture_active",
                        lambda _info=None: _white())

    info = _info(collector_mod, monitor=1)
    frames = collector_mod.capture_all_monitors(info)

    assert len(frames) == 1
    assert frames[0][0] == 1
    assert frames[0][1].size == (600, 400)


# ============================================================================
# 2. Collector._handle がマルチモニター時に additional_screenshots を出す
# ============================================================================

def test_handle_emits_additional_screenshots_with_two_monitors(isolated_env, monkeypatch):
    import collector as collector_mod  # type: ignore
    collector_mod_ref, collector = _make_collector(stub_boxes=[[], []])

    monitors = [
        {"left": 0, "top": 0, "width": 3840, "height": 1080},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 1920, "top": 0, "width": 1920, "height": 1080},
    ]
    _install_fake_mss(collector_mod_ref, monitors, grab_colors={1: (50, 50, 50), 2: (200, 200, 200)})

    # OCR 空でも保存されるよう strict/drop を緩めて raw_capture_mode=True に倒さず通したい:
    # → strict=False にすればOCR空でも drop されない
    collector._cfg.mask_strict_mode = False
    collector._cfg.drop_image_if_unmaskable = False

    info = _info(collector_mod_ref, monitor=1)
    event = collector.process(info)

    assert event is not None
    assert event["screenshot"] is not None
    assert event["screenshot"]["monitor_index"] == 1  # フォーカス側
    assert "additional_screenshots" in event
    assert len(event["additional_screenshots"]) == 1
    assert event["additional_screenshots"][0]["monitor_index"] == 2


def test_screenshot_filenames_include_monitor_suffix(isolated_env, monkeypatch):
    import collector as collector_mod  # type: ignore
    collector_mod_ref, collector = _make_collector(stub_boxes=[[], []])

    monitors = [
        {"left": 0, "top": 0, "width": 3840, "height": 1080},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 1920, "top": 0, "width": 1920, "height": 1080},
    ]
    _install_fake_mss(collector_mod_ref, monitors, grab_colors={1: (50, 50, 50), 2: (200, 200, 200)})

    collector._cfg.mask_strict_mode = False
    collector._cfg.drop_image_if_unmaskable = False

    info = _info(collector_mod_ref, monitor=1)
    event = collector.process(info)

    primary_name = event["screenshot"]["filename"]
    extra_name = event["additional_screenshots"][0]["filename"]
    assert primary_name.endswith("_mon1.jpg"), f"unexpected primary filename: {primary_name}"
    assert extra_name.endswith("_mon2.jpg"), f"unexpected extra filename: {extra_name}"
    # ファイル名は別物（衝突なし）
    assert primary_name != extra_name


def test_single_monitor_environment_has_empty_additional(isolated_env, monkeypatch):
    """物理モニター1台環境では additional_screenshots は空."""
    import collector as collector_mod  # type: ignore
    collector_mod_ref, collector = _make_collector(stub_boxes=[[]])

    monitors = [
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
    ]
    _install_fake_mss(collector_mod_ref, monitors, grab_colors={1: (255, 255, 255)})

    collector._cfg.mask_strict_mode = False
    collector._cfg.drop_image_if_unmaskable = False

    info = _info(collector_mod_ref, monitor=1)
    event = collector.process(info)

    assert event["screenshot"]["monitor_index"] == 1
    assert event["additional_screenshots"] == []


def test_capture_active_monitor_only_true_preserves_legacy(isolated_env, monkeypatch):
    """capture_active_monitor_only=True (オプトアウト) で従来挙動になる."""
    import collector as collector_mod  # type: ignore
    collector_mod_ref, collector = _make_collector(
        stub_boxes=[[]],
        capture_active_monitor_only=True,
    )
    monkeypatch.setattr(collector_mod_ref, "capture_active",
                        lambda _info=None: _white())

    collector._cfg.mask_strict_mode = False
    collector._cfg.drop_image_if_unmaskable = False

    info = _info(collector_mod_ref, monitor=1)
    event = collector.process(info)

    assert event["screenshot"] is not None
    assert event["additional_screenshots"] == []


# ============================================================================
# 3. 各モニターで OCR/マスクが独立に走る (PII 漏洩しないこと)
# ============================================================================

def test_pii_in_monitor1_does_not_leak_to_monitor2(isolated_env, monkeypatch):
    """モニター1にPIIが映っていても、モニター2のOCR要約に漏れないこと."""
    import collector as collector_mod  # type: ignore
    from ocr import OCRBox  # type: ignore

    # OCR エンジンは呼び出し順に別のboxを返す:
    # 1回目 = mon1 (PII含む), 2回目 = mon2 (PII無し)
    boxes_mon1 = [OCRBox(text="鈴木太郎 様", bbox=(0, 0, 200, 50), confidence=0.95)]
    boxes_mon2 = [OCRBox(text="調剤指示", bbox=(0, 0, 200, 50), confidence=0.95)]

    collector_mod_ref, collector = _make_collector(stub_boxes=[boxes_mon1, boxes_mon2])

    monitors = [
        {"left": 0, "top": 0, "width": 3840, "height": 1080},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 1920, "top": 0, "width": 1920, "height": 1080},
    ]
    _install_fake_mss(collector_mod_ref, monitors, grab_colors={1: (50, 50, 50), 2: (200, 200, 200)})

    info = _info(collector_mod_ref, monitor=1)
    event = collector.process(info)

    assert event is not None
    primary_summary = (event["screenshot"] or {}).get("ocr_text_summary", "")
    extras = event["additional_screenshots"]
    extra_summary = (extras[0] if extras else {}).get("ocr_text_summary", "")

    # mon1 のPIIは mon2 に漏れていない
    assert "鈴木太郎" not in extra_summary, (
        f"PII LEAK across monitors: '鈴木太郎' appeared in mon2 summary: {extra_summary!r}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
