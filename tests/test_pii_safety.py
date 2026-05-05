"""PII漏洩防止テスト（Codexレビュー指摘の構造的封じ込め）.

「マスキング失敗時に生スクショがディスクに残ること」を絶対に発生させない、
というv1.0の不変条件を機械的に保証する3本のテスト。

CIゲートで必須化: 1本でも落ちたら配布禁止。

テスト対象の3経路（collector.py:_handle 内）:
1. masker モジュールが import 失敗 → _HAS_MASKER=False で生画像保存禁止
2. OCRが0件 + strict + drop_image_if_unmaskable=True → メタイベントのみ
3. mask_image() 内部で例外 → 画像保存せずメタイベントに切り替え
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, List

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _isolate_appdata(tmp: Path) -> None:
    os.environ["APPDATA"] = str(tmp)
    for m in ("storage", "collector", "config", "window_titles", "masker", "ocr", "profile_loader"):
        sys.modules.pop(m, None)


class _StubOCR:
    def __init__(self, boxes: list) -> None:
        self._boxes = boxes

    def extract(self, _image) -> list:
        return list(self._boxes)


def _white_image(w: int = 600, h: int = 400) -> Image.Image:
    return Image.new("RGB", (w, h), (255, 255, 255))


@pytest.fixture()
def isolated_env(tmp_path):
    _isolate_appdata(tmp_path)
    yield tmp_path


def _make_collector(stub_boxes: list, **cfg_kwargs):
    import config as cfg_mod  # type: ignore
    import collector as collector_mod  # type: ignore

    base = {
        "min_dwell_seconds_for_capture": 0.0,
        "max_capture_per_minute": 120,
    }
    base.update(cfg_kwargs)
    cfg = cfg_mod.CollectorConfig(**base)
    return collector_mod, collector_mod.Collector(cfg=cfg, ocr_engine=_StubOCR(stub_boxes))


def _info(collector_mod, hwnd: int = 1):
    return collector_mod.WindowInfo(
        hwnd=hwnd,
        title="Receipt 入力",
        process_name="Receipt.exe",
        process_path="C:\\Receipt.exe",
        pid=999,
        rect=(0, 0, 1920, 1080),
        monitor=1,
    )


def _count_screenshots(appdata: Path) -> int:
    """%APPDATA%/WorkScope/data/screenshots に保存されたJPEG数."""
    d = appdata / "WorkScope" / "data" / "screenshots"
    if not d.exists():
        return 0
    return len(list(d.glob("*.jpg")))


def _patch_capture_active(collector_mod, monkeypatch):
    """capture_active を白画像を返すスタブに差し替え（mss/pywin32 不在環境用）."""
    monkeypatch.setattr(
        collector_mod, "capture_active", lambda _info=None: _white_image()
    )


# ============================================================================
# PII漏洩テスト #1: masker モジュールが import 失敗時
# ============================================================================

def test_no_raw_image_when_masker_unavailable(isolated_env, monkeypatch):
    """masker が import 失敗していたら、生スクショは絶対にディスクに書かれない.

    Codex critical#3: 'masker未導入時に生画像保存に倒れる余地' を構造的に塞ぐ。
    """
    collector_mod, collector = _make_collector(stub_boxes=[])
    _patch_capture_active(collector_mod, monkeypatch)

    # masker を意図的に "import 失敗" 状態に
    monkeypatch.setattr(collector_mod, "_HAS_MASKER", False)

    info = _info(collector_mod)
    event = collector.process(info)

    # スクショが1枚も残っていないこと（最重要不変条件）
    assert _count_screenshots(isolated_env) == 0, \
        "PII LEAK: masker 不在時に生画像がディスクに残った"

    # メタイベントのみ記録されていること（業務分析は継続可能）
    assert event is not None
    assert event.get("event_type") == "window_focus"
    assert event.get("screenshot") is None, \
        "PII LEAK: screenshot ペイロードが None でない"


# ============================================================================
# PII漏洩テスト #2: OCRが0件返す + strict + drop_image_if_unmaskable=True
# ============================================================================

def test_no_raw_image_when_ocr_returns_empty(isolated_env, monkeypatch):
    """OCR が 0 件返した時、strict mode かつ drop_image_if_unmaskable=True なら
    マスクすべきか判定不能なので画像は捨てる.

    Codex critical#3: 'OCR空 = マスクすべきか判定不能' のリスク経路。
    """
    collector_mod, collector = _make_collector(
        stub_boxes=[],  # OCRが空を返す
        mask_strict_mode=True,
        drop_image_if_unmaskable=True,
    )
    _patch_capture_active(collector_mod, monkeypatch)

    info = _info(collector_mod)
    event = collector.process(info)

    # OCR が読めなかった画面 = マスクできたか判定不能 = 画像捨てる
    assert _count_screenshots(isolated_env) == 0, \
        "PII LEAK: OCR0件+strict+drop_if_unmaskable で画像が残った"

    assert event is not None
    assert event.get("screenshot") is None


def test_image_kept_when_ocr_empty_but_drop_disabled(isolated_env, monkeypatch):
    """drop_image_if_unmaskable=False なら OCR0件でも画像保存される（コントロール群）."""
    collector_mod, collector = _make_collector(
        stub_boxes=[],
        mask_strict_mode=True,
        drop_image_if_unmaskable=False,
    )
    _patch_capture_active(collector_mod, monkeypatch)

    info = _info(collector_mod)
    event = collector.process(info)

    # drop が無効 = OCR空でも保存される（このオプションは v1.0 では非推奨）
    assert _count_screenshots(isolated_env) >= 1
    assert event is not None
    assert event.get("screenshot") is not None


# ============================================================================
# PII漏洩テスト #3: mask_image() 自体が例外を投げる
# ============================================================================

def test_no_raw_image_when_mask_image_raises(isolated_env, monkeypatch):
    """mask_image() が例外を投げたら、画像保存はせずメタイベントに切り替える.

    Codex critical#3: 'mask処理例外時の漏洩経路' を構造的に塞ぐ。
    drop_image_if_unmaskable の真偽に関わらず、例外時は画像を残さない。
    """
    from ocr import OCRBox  # type: ignore

    collector_mod, collector = _make_collector(
        stub_boxes=[OCRBox(text="鈴木太郎 様", bbox=(50, 60, 350, 100), confidence=0.95)],
        drop_image_if_unmaskable=True,
    )
    _patch_capture_active(collector_mod, monkeypatch)

    # mask_image 関数を例外を投げるダミーに差し替え
    def _raise(*_args, **_kwargs):
        raise RuntimeError("simulated mask failure")

    monkeypatch.setattr(collector_mod, "mask_image", _raise)

    info = _info(collector_mod)
    event = collector.process(info)

    assert _count_screenshots(isolated_env) == 0, \
        "PII LEAK: mask_image() 例外時に生画像が残った"

    assert event is not None
    assert event.get("screenshot") is None


def test_no_raw_image_when_mask_raises_even_drop_disabled(isolated_env, monkeypatch):
    """drop_image_if_unmaskable=False でも、mask_image() 例外時は画像保存しない（最重要）."""
    from ocr import OCRBox  # type: ignore

    collector_mod, collector = _make_collector(
        stub_boxes=[OCRBox(text="鈴木太郎 様", bbox=(50, 60, 350, 100), confidence=0.95)],
        drop_image_if_unmaskable=False,  # drop 無効でも
    )
    _patch_capture_active(collector_mod, monkeypatch)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("simulated mask failure")

    monkeypatch.setattr(collector_mod, "mask_image", _raise)

    info = _info(collector_mod)
    event = collector.process(info)

    # mask 例外時は drop の設定に関わらず「マスク前画像」を残してはいけない
    assert _count_screenshots(isolated_env) == 0, \
        "PII LEAK: mask_image()例外+drop無効でも生画像が残った（最重要不変条件）"
    assert event is not None
    assert event.get("screenshot") is None


# ============================================================================
# 補助テスト: マスク済み画像は保存される（コントロール群、誤陽性検出）
# ============================================================================

def test_masked_image_is_saved_normally(isolated_env, monkeypatch):
    """正常パス（OCRあり+マスク成功）では保存される（テスト自体の妥当性確認）."""
    from ocr import OCRBox  # type: ignore

    collector_mod, collector = _make_collector(
        stub_boxes=[OCRBox(text="鈴木太郎 様", bbox=(50, 60, 350, 100), confidence=0.95)],
        drop_image_if_unmaskable=True,
    )
    _patch_capture_active(collector_mod, monkeypatch)

    info = _info(collector_mod)
    event = collector.process(info)

    # 正常パスでは保存される（PII漏洩テストが false positive でないことの保証）
    assert _count_screenshots(isolated_env) >= 1
    assert event is not None
    assert event.get("screenshot") is not None
    # mask が適用されている
    assert event["screenshot"]["mask_applied_count"] >= 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
