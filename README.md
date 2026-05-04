# WorkScope Collector

業務棚卸し→RPA/AIエージェント化のための、オンデバイス完結型 業務スクリーン収集ツール。

## 設計の核

- **オンデバイス完結**: スクショ取得→OCR→個人情報マスキング→保存 を全てローカルで実行。生スクショはディスクに残らない。
- **イベント駆動**: アクティブウィンドウ変化時にのみ収集（無駄なIOなし、業務フローの遷移が綺麗に取れる）。
- **棚卸し志向**: アプリ遷移シーケンス・滞在時間・反復パターンを後日の解析・自動化候補スコアリングに直結する形で記録。
- **可視性**: タスクトレイ常駐で薬剤師さんが「今録っている」「直近スクショ何だったか」を自分で確認・一時停止できる。

## ディレクトリ構成

```
workscope-collector/
├── src/                      # Python ソース（Windows EXE化される）
├── installer/                # install.bat / uninstall.bat
├── docs/                     # 同意書・運用手順・説明資料
├── analyzer/                 # フェーズ2: 解析・RPA雛形生成（後日実装）
├── .github/workflows/        # Windows EXE自動ビルド
├── pyinstaller.spec
├── requirements.txt
└── README.md
```

## ビルド

GitHub Actions の Windows runner が自動で EXE を生成します。
`main` ブランチにpushすると、`build-windows.yml` ワークフローが走り、artifactとして `workscope-collector-windows.zip` がダウンロードできます。

## インストール（薬局側）

USBに以下を入れて持ち込み：
- `WorkScope.exe`
- `install.bat`
- `uninstall.bat`
- `consent_form.html`（印刷署名）

`install.bat` を右クリック→「管理者として実行」→ 完了後タスクトレイに緑アイコン。

## データ保存先

- `%APPDATA%\WorkScope\data\screenshots\` （マスク済みPNG）
- `%APPDATA%\WorkScope\data\events\YYYY-MM-DD.jsonl` （構造化イベントログ）
- `%APPDATA%\WorkScope\config.json` （ローカル設定）

## アンインストール

`uninstall.bat` を右クリック→「管理者として実行」。スタートアップ解除・データ削除確認ダイアログが出ます。

## ロードマップ

| フェーズ | 期間 | 内容 |
|---|---|---|
| 1: 収集 | 2週間 | WorkScope Collector で業務データ蓄積 |
| 2: 解析 | 1週間 | 業務マップ生成、反復検出、自動化候補スコアリング |
| 3: RPA化 | 2週間 | 高スコア業務をPower Automate Desktop / pywinauto / AIエージェントで自動化 |
| 4: 展開 | 継続 | 現場運用・追加業務の自動化 |
