"""Mac でも回せるデモ実行スクリプト.

レセコン画面に近いダミー画像を生成し、

  1. 画像にあえて個人情報（氏名・電話・保険者番号・生年月日）を描画
  2. ダミー OCR がそれら矩形を返す
  3. masker でマスキング
  4. ScreenshotStore に保存
  5. EventStore に JSONL を書く

を実行する。実行後、生成された JPEG/JSONL の場所を表示する。
"""

from __future__ import annotations

import os
import sys
import shutil
from pathlib import Path
from tempfile import mkdtemp


def _setup_paths(workdir: Path) -> None:
    os.environ["APPDATA"] = str(workdir)
    src = Path(__file__).resolve().parents[1] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _draw_demo_screenshot(out_path: Path) -> tuple[object, list]:
    """擬似レセコン画面 + 各テキストの bbox(OCRBox) を返す."""
    from PIL import Image, ImageDraw, ImageFont
    from ocr import OCRBox  # type: ignore

    W, H = 1280, 720
    img = Image.new("RGB", (W, H), (245, 247, 250))
    draw = ImageDraw.Draw(img)

    # ヘッダ風の濃紺バー（ロゴ感）
    draw.rectangle((0, 0, W, 64), fill=(30, 58, 95))
    draw.text((24, 18), "Receipt System v3", fill=(255, 255, 255))

    boxes: list = []

    def text_with_box(text: str, xy: tuple[int, int], box_w: int = 380, box_h: int = 36) -> None:
        x, y = xy
        draw.rectangle((x, y, x + box_w, y + box_h), outline=(180, 180, 180), width=1)
        draw.text((x + 8, y + 8), text, fill=(20, 20, 30))
        boxes.append(OCRBox(text=text, bbox=(x + 8, y + 8, x + 8 + box_w - 16, y + 8 + box_h - 16),
                            confidence=0.95))

    # 患者情報パネル
    draw.text((24, 96), "■ 患者情報", fill=(30, 58, 95))
    boxes.append(OCRBox(text="■ 患者情報", bbox=(24, 96, 200, 124), confidence=0.95))

    text_with_box("患者ID  100245", (24, 140), box_w=300)
    text_with_box("氏名    鈴木太郎 様", (24, 190), box_w=420)
    text_with_box("生年月日 1985/03/15", (24, 240), box_w=320)
    text_with_box("電話    090-1234-5678", (24, 290), box_w=320)
    text_with_box("住所    東京都新宿区西新宿1-2-3", (24, 340), box_w=540)
    text_with_box("保険者番号 12345678", (24, 390), box_w=380)
    text_with_box("マイナンバー 1234 5678 9012", (24, 440), box_w=420)

    # 処方欄（薬品名は誤マスクされない想定）
    draw.text((640, 96), "■ 処方", fill=(30, 58, 95))
    boxes.append(OCRBox(text="■ 処方", bbox=(640, 96, 760, 124), confidence=0.95))
    text_with_box("Rp1  アムロジピン 5mg 朝食後", (640, 140), box_w=520)
    text_with_box("Rp2  ロキソニン 60mg 頓服", (640, 190), box_w=520)
    text_with_box("Rp3  カロナール 200mg 毎食後", (640, 240), box_w=520)

    # フッタ
    draw.text((24, 680), f"Receipt System | 操作者: フィクション 太郎", fill=(120, 120, 120))
    boxes.append(OCRBox(text="Receipt System  操作者: フィクション 太郎",
                        bbox=(24, 680, 600, 700), confidence=0.95))

    img.save(out_path, "JPEG", quality=85)
    return img, boxes


def main() -> int:
    workdir = Path(mkdtemp(prefix="workscope-demo-"))
    print(f"[demo] workdir: {workdir}")
    _setup_paths(workdir)

    import collector as collector_mod  # type: ignore
    from config import (  # type: ignore
        CollectorConfig,
        events_dir,
        screenshots_dir,
    )

    # 元画像を保存（マスキング前後を比較できるように）
    pre_path = workdir / "pre_mask_demo.jpg"
    img, boxes = _draw_demo_screenshot(pre_path)
    print(f"[demo] pre-mask screenshot: {pre_path}")

    # capture_active を差し替え（mss が呼ばれないように）
    collector_mod.capture_active = lambda _info=None: img  # type: ignore[assignment]

    class _StubOCR:
        def extract(self, _img):
            return list(boxes)

    cfg = CollectorConfig(
        min_dwell_seconds_for_capture=0.0,
        max_capture_per_minute=120,
        drop_image_if_unmaskable=False,
        mask_strict_mode=True,
    )
    c = collector_mod.Collector(cfg=cfg, ocr_engine=_StubOCR())

    info_a = collector_mod.WindowInfo(
        hwnd=1, title="Explorer", process_name="explorer.exe",
        process_path="C:\\Windows\\explorer.exe", pid=100, rect=(0, 0, 1280, 720), monitor=1,
    )
    info_b = collector_mod.WindowInfo(
        hwnd=2, title="患者照会 - 鈴木太郎様 - Receipt v3",
        process_name="Receipt.exe", process_path="C:\\Receipt\\Receipt.exe",
        pid=200, rect=(0, 0, 1280, 720), monitor=1,
    )

    c.process(info_a)
    c._focus.focus_since -= 1.5  # dwell 1.5s
    result = c.process(info_b)
    c._events.close()

    if result is None:
        print("[demo] [FAIL] イベントが書かれなかった")
        return 1

    ss = result["screenshot"]
    if ss is None:
        print("[demo] [FAIL] スクショが保存されなかった")
        return 2

    # 結果検証
    saved_jpg = screenshots_dir() / ss["filename"]
    print(f"[demo] masked screenshot: {saved_jpg}")
    print(f"[demo]   size           : {ss['width']}x{ss['height']}")
    print(f"[demo]   mask categories: {ss['mask_categories']}")
    print(f"[demo]   mask count     : {ss['mask_applied_count']}")
    print(f"[demo]   ocr token count: {ss['ocr_token_count']}")
    print(f"[demo]   text summary   : {ss['ocr_text_summary'][:200]}...")

    print(f"\n[demo] window title (masked): {result['window']['title']}")
    print(f"[demo]   title categories      : {result['window']['title_mask_categories']}")

    # JSONL 確認
    jsonl_files = sorted(events_dir().glob("*.jsonl"))
    if not jsonl_files:
        print("[demo] [FAIL] JSONL が無い")
        return 3
    print(f"\n[demo] event jsonl: {jsonl_files[0]}")
    last_line = [ln for ln in jsonl_files[0].read_text(encoding="utf-8").splitlines() if ln][-1]
    print(f"[demo] last event line ({len(last_line)} chars):")
    print(f"  {last_line[:400]}{'...' if len(last_line) > 400 else ''}")

    # 漏洩テキスト検査（生で残っていないか）
    leaks: list[str] = []
    for forbidden in ("鈴木太郎", "090-1234-5678", "12345678", "1985/03/15", "1234 5678 9012"):
        if forbidden in last_line:
            leaks.append(forbidden)
    if leaks:
        print(f"[demo] [FAIL] JSONL に生のPIIが残った: {leaks}")
        return 4
    print("\n[demo] [OK] JSONL に生 PII は含まれない")

    # マスク済み画像にも生の文字がそのままピクセル形で残っていないかは目視確認向け
    # ここは saved_jpg を開いて確認してもらう
    target_dir = Path(__file__).resolve().parent / "_demo_artifacts"
    target_dir.mkdir(exist_ok=True)
    out_pre = target_dir / "pre_mask.jpg"
    out_post = target_dir / "post_mask.jpg"
    out_pre.write_bytes(pre_path.read_bytes())
    out_post.write_bytes(saved_jpg.read_bytes())
    print(f"\n[demo] artifacts copied to: {target_dir}")
    print(f"  pre-mask : {out_pre}")
    print(f"  post-mask: {out_post}")
    print("\n[demo] 完了。post_mask.jpg を開いて、患者氏名・電話番号・住所・保険者番号・"
          "マイナンバーが黒塗りされていることを目視で確認してください。")

    # ワークディレクトリは残す（中身を確認しやすく）
    print(f"\n[demo] workdir (調査用に保持): {workdir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
