"""エンドツーエンドのパイプライン統合テスト.

Collector._handle 経由で
  画像生成 → ダミーOCR(OCRBox直渡し) → masker → storage 書き込み
までを通し、保存物（JPEG + JSONL）を検証する。

PaddleOCR / mss / pywin32 / Pillow Win 専用ハック等は一切使わず Mac/Linux でも実行可能。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _isolate_appdata(tmp: Path) -> None:
    os.environ["APPDATA"] = str(tmp)
    for m in ("storage", "collector", "config", "window_titles", "masker", "ocr"):
        sys.modules.pop(m, None)


class _StubOCR:
    """画像内容に依らず固定 OCRBox を返すスタブ OCR."""

    def __init__(self, boxes: list) -> None:
        self._boxes = boxes

    def extract(self, _image) -> list:
        return list(self._boxes)


@pytest.fixture()
def isolated_env(tmp_path):
    _isolate_appdata(tmp_path)
    yield tmp_path


def _white_image(w: int = 600, h: int = 400) -> Image.Image:
    return Image.new("RGB", (w, h), (255, 255, 255))


def _make_collector_with_stub_ocr(stub_boxes: list, **cfg_kwargs):
    """OCR を差し替えた Collector を作る."""
    import config as cfg_mod  # type: ignore
    import collector as collector_mod  # type: ignore

    # 既存パイプラインテストは v1.0 までの strict 挙動を検証する内容なので、
    # raw_capture_mode は明示的に False に固定（v1.1 で既定 True に変更）。
    base = {
        "min_dwell_seconds_for_capture": 0.0,
        "max_capture_per_minute": 120,
        "drop_image_if_unmaskable": False,
        "raw_capture_mode": False,
    }
    base.update(cfg_kwargs)
    cfg = cfg_mod.CollectorConfig(**base)
    return collector_mod, collector_mod.Collector(cfg=cfg, ocr_engine=_StubOCR(stub_boxes))


def _info(collector_mod, hwnd: int = 1, title: str = "Receipt 入力", proc: str = "Receipt.exe"):
    return collector_mod.WindowInfo(
        hwnd=hwnd,
        title=title,
        process_name=proc,
        process_path=f"C:\\{proc}",
        pid=999,
        rect=(0, 0, 1920, 1080),
        monitor=1,
    )


# ---- パイプラインテスト本体 ----------------------------------------------


def test_pipeline_masks_patient_name_and_saves_jpeg(isolated_env, monkeypatch):
    """OCRが患者氏名Boxを返したケース → 黒塗り済みJPEGとJSONL記録を確認."""
    from ocr import OCRBox  # type: ignore  # noqa: E402

    boxes: List = [
        OCRBox(text="鈴木太郎 様", bbox=(50, 60, 350, 100), confidence=0.95),
        OCRBox(text="処方せん入力", bbox=(50, 140, 280, 180), confidence=0.95),
    ]
    collector_mod, c = _make_collector_with_stub_ocr(boxes)

    # capture_active を白画像に差し替え（mss/Win依存を回避）
    monkeypatch.setattr(collector_mod, "capture_active", lambda _info=None: _white_image())

    info_a = _info(collector_mod, hwnd=1, proc="A.exe", title="A")
    info_b = _info(collector_mod, hwnd=2, proc="Receipt.exe", title="処方")
    c.process(info_a)
    # 1秒以上の dwell を擬似化
    c._focus.focus_since -= 1.0
    result = c.process(info_b)

    assert result is not None, "イベントが書かれていない"
    # スクショファイルが作られていること
    ss = result["screenshot"]
    assert ss is not None, "screenshot payload が None"
    assert ss["filename"].endswith(".jpg")
    saved_path = Path(ss["filename"])
    # 実体パスは config.screenshots_dir() にある
    from config import screenshots_dir  # type: ignore
    full = screenshots_dir() / ss["filename"]
    assert full.exists(), "保存先に JPEG が無い"

    # OCR テキスト要約に [MASKED:...] を含む
    assert "[MASKED:" in ss["ocr_text_summary"]
    # 患者氏名カテゴリが含まれる (v1.0汎用化: patient_name → personal_name に統合)
    assert any(c in ss["mask_categories"] for c in ("personal_name", "personal_name_kana", "name_like_kanji"))
    assert ss["mask_applied_count"] >= 1

    # 黒塗りされていることをピクセルレベルで確認
    import numpy as np  # type: ignore
    arr = np.array(Image.open(full))
    # 患者氏名 box の中央 (200, 80) 付近
    px = arr[80, 200]
    assert int(px[0]) < 30 and int(px[1]) < 30 and int(px[2]) < 30, \
        "患者氏名 box が黒塗りされていない"


def test_pipeline_drops_image_when_no_ocr_in_strict_mode(isolated_env, monkeypatch):
    """OCRが0boxを返した場合、strict + drop_image_if_unmaskable では画像保存しない."""
    collector_mod, c = _make_collector_with_stub_ocr(
        stub_boxes=[],
        mask_strict_mode=True,
        drop_image_if_unmaskable=True,
    )
    monkeypatch.setattr(collector_mod, "capture_active", lambda _info=None: _white_image())

    info_a = _info(collector_mod, hwnd=1, proc="A.exe", title="A")
    info_b = _info(collector_mod, hwnd=2, proc="Receipt.exe", title="患者一覧")
    c.process(info_a)
    c._focus.focus_since -= 1.0
    result = c.process(info_b)

    assert result is not None, "メタイベントは残るべき"
    assert result["screenshot"] is None, "画像なしで保存しているはず"

    # JPEG が保存されていないこと
    from config import screenshots_dir  # type: ignore
    files = list(screenshots_dir().glob("*.jpg"))
    assert files == [], f"strictモードで画像が保存された: {files}"


def test_pipeline_writes_jsonl_with_full_schema(isolated_env, monkeypatch):
    """保存された JSONL に必須フィールドと PII マスク済みデータが揃っているか."""
    from ocr import OCRBox  # type: ignore

    boxes: List = [
        OCRBox(text="保険者番号", bbox=(50, 50, 200, 80), confidence=0.95),
        OCRBox(text="12345678", bbox=(220, 50, 400, 80), confidence=0.95),
    ]
    collector_mod, c = _make_collector_with_stub_ocr(boxes)
    monkeypatch.setattr(collector_mod, "capture_active", lambda _info=None: _white_image())

    info_a = _info(collector_mod, hwnd=1, proc="A.exe", title="A")
    info_b = _info(collector_mod, hwnd=2, proc="Receipt.exe", title="保険者番号 12345678 を入力")
    c.process(info_a)
    c._focus.focus_since -= 1.0
    c.process(info_b)
    c._events.close()

    # 直近イベントを読む
    from config import events_dir  # type: ignore
    files = list(events_dir().glob("*.jsonl"))
    assert files, "JSONL が書き出されていない"
    line = [ln for ln in files[0].read_text(encoding="utf-8").splitlines() if ln][-1]
    obj = json.loads(line)

    # スキーマ必須キー
    for key in ("session_id", "event_seq", "ts", "event_type", "app", "window", "screenshot"):
        assert key in obj, f"missing key: {key}"
    assert obj["window"]["title_mask_categories"], "タイトルカテゴリが取れていない"

    # ウィンドウタイトル内の保険者番号がマスク済み
    assert "12345678" not in obj["window"]["title"]
    assert "[MASKED:" in obj["window"]["title"]

    # スクショの mask_categories に insurance 系が入っている
    cats = obj["screenshot"]["mask_categories"]
    assert any("insurance" in c for c in cats), f"保険系カテゴリ無し: {cats}"


def test_no_raw_image_saved_when_masker_raises(isolated_env, monkeypatch):
    """masker.mask_image が例外を投げた場合、生スクショを絶対に保存しない."""
    import masker as masker_mod  # type: ignore
    from ocr import OCRBox  # type: ignore

    boxes: List = [OCRBox(text="鈴木太郎 様", bbox=(50, 50, 300, 90), confidence=0.95)]
    collector_mod, c = _make_collector_with_stub_ocr(
        boxes, drop_image_if_unmaskable=True, mask_strict_mode=True,
    )
    monkeypatch.setattr(collector_mod, "capture_active", lambda _info=None: _white_image())

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated masker failure")

    monkeypatch.setattr(collector_mod, "mask_image", _boom)
    # masker._HAS_MASKER は collector でしか参照しないので影響なし

    info_a = _info(collector_mod, hwnd=1, proc="A.exe", title="A")
    info_b = _info(collector_mod, hwnd=2, proc="Receipt.exe", title="患者")
    c.process(info_a)
    c._focus.focus_since -= 1.0
    result = c.process(info_b)

    assert result is not None
    assert result["screenshot"] is None

    from config import screenshots_dir  # type: ignore
    assert list(screenshots_dir().glob("*.jpg")) == []


def test_no_raw_image_saved_when_masker_unavailable(isolated_env, monkeypatch):
    """masker モジュールが import 失敗していたら、画像保存しない（防御的）."""
    from ocr import OCRBox  # type: ignore

    boxes: List = [OCRBox(text="鈴木太郎 様", bbox=(50, 50, 300, 90), confidence=0.95)]
    collector_mod, c = _make_collector_with_stub_ocr(boxes)
    monkeypatch.setattr(collector_mod, "capture_active", lambda _info=None: _white_image())
    monkeypatch.setattr(collector_mod, "_HAS_MASKER", False)

    info_a = _info(collector_mod, hwnd=1, proc="A.exe", title="A")
    info_b = _info(collector_mod, hwnd=2, proc="Receipt.exe", title="患者")
    c.process(info_a)
    c._focus.focus_since -= 1.0
    result = c.process(info_b)

    assert result is not None
    assert result["screenshot"] is None

    from config import screenshots_dir  # type: ignore
    assert list(screenshots_dir().glob("*.jpg")) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
