"""PaddleOCR 薄ラッパー.

オンデバイスで日本語+英語OCRを実行し、行単位の bbox とテキストを返す。
モデル未取得時は初回呼び出し時に自動DLされる（PaddleOCR 既定動作）。
PaddleOCR 自体は重いので、import は遅延（クラス内）で行い、Mac開発でも
ImportError でモジュールロードが失敗しないようにする。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


@dataclass
class OCRBox:
    """OCR が検出した1行分のテキスト+矩形."""

    text: str
    bbox: tuple[int, int, int, int]  # (x1, y1, x2, y2) 元解像度ベース
    confidence: float


class OCREngine:
    """PaddleOCR の薄いラッパー.

    - 入力画像を `max_image_side` に合わせて縮小してから推論
    - 結果の bbox は元画像の解像度に戻す
    - PaddleOCR 初期化に失敗しても import エラーで死なない設計
    """

    def __init__(
        self,
        languages: list[str] | None = None,
        max_image_side: int = 1920,
    ) -> None:
        self.languages = languages or ["japan", "en"]
        self.max_image_side = max_image_side
        self._ocr: Any | None = None
        self._init_failed: bool = False

        # PaddleOCR は lang を1つしか取らないので、日本語優先で1個選ぶ
        self._lang = "japan"
        for cand in self.languages:
            if cand.lower() in ("japan", "japanese", "ja", "jp"):
                self._lang = "japan"
                break
            if cand.lower() in ("en", "english"):
                self._lang = "en"

        self._lazy_init()

    def _lazy_init(self) -> None:
        """PaddleOCR を遅延 import + 初期化."""
        if self._ocr is not None or self._init_failed:
            return
        try:
            from paddleocr import PaddleOCR  # type: ignore[import-not-found]

            self._ocr = PaddleOCR(
                use_angle_cls=True,
                lang=self._lang,
                show_log=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("PaddleOCR の初期化に失敗: %s", exc)
            self._ocr = None
            self._init_failed = True

    def _to_ndarray(self, image: Image.Image | np.ndarray) -> np.ndarray:
        if isinstance(image, np.ndarray):
            return image
        if image.mode != "RGB":
            image = image.convert("RGB")
        return np.array(image)

    def _resize_for_ocr(self, arr: np.ndarray) -> tuple[np.ndarray, float]:
        h, w = arr.shape[:2]
        longest = max(h, w)
        if longest <= self.max_image_side:
            return arr, 1.0
        scale = self.max_image_side / float(longest)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        try:
            import cv2  # type: ignore[import-not-found]

            resized = cv2.resize(arr, (new_w, new_h), interpolation=cv2.INTER_AREA)
        except Exception:  # noqa: BLE001
            pil = Image.fromarray(arr).resize((new_w, new_h), Image.BILINEAR)
            resized = np.array(pil)
        return resized, scale

    def extract(self, image: Image.Image | np.ndarray) -> list[OCRBox]:
        """画像から OCRBox のリストを返す. 失敗時は空リスト."""
        self._lazy_init()
        if self._ocr is None:
            return []

        try:
            arr = self._to_ndarray(image)
            resized, scale = self._resize_for_ocr(arr)
            inv_scale = 1.0 / scale if scale else 1.0

            raw = self._ocr.ocr(resized, cls=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR 推論に失敗: %s", exc)
            return []

        # PaddleOCR の戻り値は version によって [page] のラップ有無が変わる
        if not raw:
            return []
        page = raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], list) else raw
        if page is None:
            return []

        boxes: list[OCRBox] = []
        for line in page:
            try:
                quad, (text, conf) = line[0], line[1]
            except (TypeError, ValueError, IndexError):
                continue
            if not text:
                continue
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            x1 = int(round(min(xs) * inv_scale))
            y1 = int(round(min(ys) * inv_scale))
            x2 = int(round(max(xs) * inv_scale))
            y2 = int(round(max(ys) * inv_scale))
            boxes.append(
                OCRBox(
                    text=str(text),
                    bbox=(x1, y1, x2, y2),
                    confidence=float(conf),
                )
            )
        return boxes
