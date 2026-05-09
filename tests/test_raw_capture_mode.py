"""raw_capture_mode（USB回収前提・データ取得最優先）の挙動テスト.

v1.1で追加された CollectorConfig.raw_capture_mode=True (既定) のもとで、
PaddleOCR が初期化失敗していても・mask_image が例外を投げても・unmaskable
判定でも、スクショが必ず保存され続けることを保証する。

JSONL の screenshot ペイロードには degraded_reason が記録され、解析側で
このスクショが未マスクであることを識別できる。

既存の test_pii_safety.py は raw_capture_mode=False を明示してstrict
ドロップ挙動を検証する。本ファイルはその対称となる新既定挙動を検証する。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

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
        "raw_capture_mode": True,  # 本テスト群の主役
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
    d = appdata / "WorkScope" / "data" / "screenshots"
    if not d.exists():
        return 0
    return len(list(d.glob("*.jpg")))


def _patch_capture_active(collector_mod, monkeypatch):
    monkeypatch.setattr(
        collector_mod, "capture_active", lambda _info=None: _white_image()
    )


# ============================================================================
# raw_capture_mode=True: OCR0件でも保存される（薬局PCの典型ケース）
# ============================================================================

def test_raw_mode_saves_when_ocr_returns_empty(isolated_env, monkeypatch):
    """PaddleOCR初期化失敗でOCRが0件返しても、raw_capture_modeなら保存される."""
    collector_mod, collector = _make_collector(
        stub_boxes=[],
        mask_strict_mode=True,
        drop_image_if_unmaskable=True,
    )
    _patch_capture_active(collector_mod, monkeypatch)

    info = _info(collector_mod)
    event = collector.process(info)

    assert _count_screenshots(isolated_env) == 1, \
        "raw_capture_mode=True なのにスクショが保存されていない"

    assert event is not None
    ss = event.get("screenshot")
    assert ss is not None, "screenshot ペイロードが None"
    assert ss.get("degraded_reason") == "ocr_empty", \
        "degraded_reason=ocr_empty が記録されていない"
    assert ss.get("mask_applied_count") == 0


# ============================================================================
# raw_capture_mode=True: mask_image() 例外でも保存される
# ============================================================================

def test_raw_mode_saves_when_mask_image_raises(isolated_env, monkeypatch):
    """mask_image() が例外を投げても、raw_capture_modeなら生画像を保存する."""
    from ocr import OCRBox  # type: ignore

    collector_mod, collector = _make_collector(
        stub_boxes=[OCRBox(text="鈴木太郎 様", bbox=(50, 60, 350, 100), confidence=0.95)],
        drop_image_if_unmaskable=True,
    )
    _patch_capture_active(collector_mod, monkeypatch)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("simulated mask failure")

    monkeypatch.setattr(collector_mod, "mask_image", _raise)

    info = _info(collector_mod)
    event = collector.process(info)

    assert _count_screenshots(isolated_env) == 1, \
        "raw_capture_mode=True で mask_image 例外時もスクショは保存されるべき"

    assert event is not None
    ss = event.get("screenshot")
    assert ss is not None
    assert ss.get("degraded_reason") == "mask_exception"


# ============================================================================
# raw_capture_mode=True: masker モジュール未読込でも保存される
# ============================================================================

def test_raw_mode_saves_when_masker_unavailable(isolated_env, monkeypatch):
    """masker import 失敗時も raw_capture_mode なら生画像を保存する."""
    collector_mod, collector = _make_collector(stub_boxes=[])
    _patch_capture_active(collector_mod, monkeypatch)

    monkeypatch.setattr(collector_mod, "_HAS_MASKER", False)

    info = _info(collector_mod)
    event = collector.process(info)

    assert _count_screenshots(isolated_env) == 1
    assert event is not None
    ss = event.get("screenshot")
    assert ss is not None
    assert ss.get("degraded_reason") == "masker_unavailable"


# ============================================================================
# raw_capture_mode=True: 正常パス（OCR成功）では従来通り黒塗りマスクを適用
# ============================================================================

def test_raw_mode_normal_path_still_masks(isolated_env, monkeypatch):
    """OCRが成功したケースでは raw_capture_mode でも黒塗りマスクは正常に適用される."""
    from ocr import OCRBox  # type: ignore

    collector_mod, collector = _make_collector(
        stub_boxes=[OCRBox(text="鈴木太郎 様", bbox=(50, 60, 350, 100), confidence=0.95)],
        drop_image_if_unmaskable=True,
    )
    _patch_capture_active(collector_mod, monkeypatch)

    info = _info(collector_mod)
    event = collector.process(info)

    assert _count_screenshots(isolated_env) == 1
    assert event is not None
    ss = event.get("screenshot")
    assert ss is not None
    # 正常パスの screenshot ペイロードでは degraded_reason=None
    assert ss.get("degraded_reason") is None, \
        "正常パスなのに degraded_reason が立っている"
    assert ss.get("mask_applied_count") >= 1, \
        "正常パスで黒塗りが1件も入っていない"


# ============================================================================
# raw_capture_mode=True + unmaskable: degraded扱いで text_summary は出力しない
# ============================================================================

def test_raw_mode_unmaskable_drops_text_summary(isolated_env, monkeypatch):
    """unmaskable + raw_mode のケースで mr.text_summary が JSONL に残らないこと.

    Codex P2 指摘: unmaskable=True 時に normal path に fall-through すると、
    未分類 OCR box のテキストが verbatim で text_summary に残ってしまう。
    raw_capture_mode は「画像は保存するがテキスト要約は出さない」degraded 扱いで
    統一する。
    """
    from ocr import OCRBox  # type: ignore

    # 漢字+4桁数字 を持つが、どのマスクルールにもマッチしない box を作る
    # （unmaskable=True を誘発）。"様/さん/殿/氏" 等の honorific を含めない、
    # かつ "番号"/"ID"/"電話" 等の context keyword も含めない。
    suspicious_text = "備考欄 12345"
    boxes = [OCRBox(text=suspicious_text, bbox=(50, 60, 350, 100), confidence=0.95)]

    collector_mod, collector = _make_collector(
        stub_boxes=boxes,
        mask_strict_mode=True,
        drop_image_if_unmaskable=True,
    )
    _patch_capture_active(collector_mod, monkeypatch)

    info = _info(collector_mod)
    event = collector.process(info)

    assert _count_screenshots(isolated_env) == 1, \
        "raw_capture_mode で unmaskable でも画像は保存されるべき"

    assert event is not None
    ss = event.get("screenshot")
    assert ss is not None
    # 重要: 疑わしいOCRテキストが verbatim で残っていないこと
    assert ss.get("ocr_text_summary") == "", \
        f"PII LEAK: unmaskable時に ocr_text_summary が出力された: {ss.get('ocr_text_summary')!r}"
    assert ss.get("degraded_reason") == "unmaskable"
    assert suspicious_text not in str(event), \
        "PII LEAK: 疑わしいテキストが event のどこかに残っている"


# ============================================================================
# raw_capture_mode=False（後方互換）: PII漏洩テストと同等の挙動を確認
# ============================================================================

def test_strict_mode_still_drops_when_raw_disabled(isolated_env, monkeypatch):
    """raw_capture_mode=False を明示すれば v1.0 のstrictドロップ挙動が維持される."""
    collector_mod, collector = _make_collector(
        stub_boxes=[],
        mask_strict_mode=True,
        drop_image_if_unmaskable=True,
        raw_capture_mode=False,
    )
    _patch_capture_active(collector_mod, monkeypatch)

    info = _info(collector_mod)
    event = collector.process(info)

    # 旧strict挙動: OCR0件 + drop=True で画像保存しない
    assert _count_screenshots(isolated_env) == 0
    assert event is not None
    assert event.get("screenshot") is None


# ============================================================================
# load_config の安全ガード: upload_enabled=True と raw_capture_mode=True の併用禁止
# ============================================================================

def test_load_config_forces_raw_off_when_upload_enabled(isolated_env):
    """upload_enabled=True と raw_capture_mode=True が両方 config.json に
    書かれていても、load_config() は raw_capture_mode を強制 False に戻す.

    Codex P1 対応: クラウド送信運用への未マスクスクショ流出を構造的に防ぐ。
    """
    import json

    import config as cfg_mod  # type: ignore

    # config.json を作成（危険な組み合わせ）
    appdata_dir = isolated_env / "WorkScope"
    appdata_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = appdata_dir / "config.json"
    cfg_path.write_text(
        json.dumps({
            "upload_enabled": True,
            "raw_capture_mode": True,
        }),
        encoding="utf-8",
    )

    cfg = cfg_mod.load_config()

    assert cfg.upload_enabled is True, "upload_enabled は config.json の値が反映されるべき"
    assert cfg.raw_capture_mode is False, \
        "PII LEAK RISK: upload_enabled=True と raw_capture_mode=True の併用が許された"


def test_load_config_keeps_raw_on_when_upload_disabled(isolated_env):
    """upload_enabled=False (USB回収運用) なら raw_capture_mode=True は許可される."""
    import json

    import config as cfg_mod  # type: ignore

    appdata_dir = isolated_env / "WorkScope"
    appdata_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = appdata_dir / "config.json"
    cfg_path.write_text(
        json.dumps({
            "upload_enabled": False,
            "raw_capture_mode": True,
        }),
        encoding="utf-8",
    )

    cfg = cfg_mod.load_config()

    assert cfg.upload_enabled is False
    assert cfg.raw_capture_mode is True, \
        "USB回収運用 (upload_enabled=False) で raw_capture_mode=True が落とされた"
