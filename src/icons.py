"""タスクトレイ用アイコン生成.

外部画像ファイルに依存せず、PIL で動的に単色丸アイコンを生成する。
PyInstaller --onefile 配布時に追加リソースを束ねなくて済む。
"""

from __future__ import annotations

from typing import Tuple

try:
    from PIL import Image, ImageDraw  # type: ignore
except Exception:  # pragma: no cover - PIL は requirements.txt 経由で必ず入る
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore


# CLAUDE.md のトンマナに沿った配色
ICON_ACTIVE: Tuple[int, int, int] = (39, 103, 73)   # #276749 (success/録画中)
ICON_PAUSED: Tuple[int, int, int] = (148, 148, 148)  # グレー (一時停止)


def make_icon(color: Tuple[int, int, int], size: int = 64) -> "Image.Image":
    """指定色の単色丸アイコン（透過背景）を生成する.

    タスクトレイのダーク/ライト両テーマで視認できるよう、丸の外周に
    薄いダークリングを足して輪郭を立てている。
    """
    if Image is None or ImageDraw is None:
        raise RuntimeError("Pillow is not available")

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = max(2, size // 16)
    bbox = (margin, margin, size - margin, size - margin)

    # 外周ダークリング（コントラスト確保）
    ring_color = (0, 0, 0, 180)
    draw.ellipse(bbox, fill=color + (255,), outline=ring_color, width=max(1, size // 32))

    # ハイライト（左上）でアイコンを立体的に
    hl_radius = size // 4
    hl_offset = size // 6
    hl_bbox = (
        hl_offset,
        hl_offset,
        hl_offset + hl_radius,
        hl_offset + hl_radius,
    )
    draw.ellipse(hl_bbox, fill=(255, 255, 255, 70))

    return img


def active_icon(size: int = 64) -> "Image.Image":
    return make_icon(ICON_ACTIVE, size)


def paused_icon(size: int = 64) -> "Image.Image":
    return make_icon(ICON_PAUSED, size)


__all__ = ["ICON_ACTIVE", "ICON_PAUSED", "make_icon", "active_icon", "paused_icon"]
