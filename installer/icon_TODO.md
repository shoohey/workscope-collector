# WorkScope アイコン (workscope.ico) 作成 TODO

このディレクトリに `workscope.ico` を配置すると、
PyInstaller ビルド時に EXE のアイコンとして埋め込まれます
（`pyinstaller.spec` で `os.path.exists` チェック済み）。

## 必要仕様

- 形式: ICO（Windows 標準アイコン）
- 推奨サイズ: 16x16 / 32x32 / 48x48 / 256x256 のマルチサイズ
- 色深度: 32bit（透過対応）

## 作成手順（例）

### 方法 A: PNG から変換（無料ツール）
1. 256x256 の透過PNGをデザイン（Figma / Illustrator など）
2. https://convertio.co/ja/png-ico/ などのオンライン変換ツールで ICO 化
3. このディレクトリに `workscope.ico` として保存

### 方法 B: ImageMagick
```bash
magick workscope_256.png -define icon:auto-resize=16,32,48,256 workscope.ico
```

### 方法 C: Pillow（Python）
```python
from PIL import Image
img = Image.open("workscope_256.png")
img.save("workscope.ico", sizes=[(16,16),(32,32),(48,48),(256,256)])
```

## デザインガイド（社内トンマナ）

- 背景色: #1e3a5f（濃紺）または透過
- アクセント: 白
- モチーフ: 目（観察）+ 折れ線グラフ（業務フロー）など
- シンプル・視認性重視（小サイズでも識別できること）

ICO が無くても EXE はビルドできます（デフォルトの PyInstaller アイコンになるだけ）。
