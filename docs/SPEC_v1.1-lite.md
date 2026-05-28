# WorkScope Collector v1.1 "Lite" 要件定義書

- バージョン: v1.1-lite
- 作成日: 2026-05-28
- 派生元: v1.0（薬局向け、スクショ+OCR+PIIマスキング+Supabase Storage）
- 対象顧客: TRIBE「業務まるごと可視化AI」契約顧客のうち、医療系以外の一般企業
- ビルド環境: VAIO（Windows 10/11）
- 稼働環境: 顧客のWindows PC

---

## 1. 背景・目的

v1.0は薬局のレセコン業務向けに、スクショ＋OCR＋PIIマスキングまで搭載した重装備版。
一般企業（士業／建設／製造／小売／サービス業など）に配布する際には以下が過剰になる:

- スクショ取得 → 顧客の心理的抵抗が大きい（画面映り込みの懸念）
- OCRエンジン同梱 → EXEサイズ約180MBで配布が重い
- PIIマスキング → 医療業界水準は不要

v1.1-lite は **JSONLログ取得のみ・Google Drive直送・リモート停止制御** に絞った汎用ライト版とする。

---

## 2. スコープ

### 2.1 In Scope（実装する）

| 項目 | 内容 |
|---|---|
| JSONLイベント収集 | window_focus / uia_focus / key_typed / mouse_click / app_category |
| Google Drive 直送 | 1時間ごとgzip化してアップロード |
| リモート制御 | control.json ポーリングで一時停止／再開／アンインストール |
| 顧客側UX | ダブルクリック→同意ボタン→稼働、トレイ常駐 |
| Windowsスタートアップ登録 | PC起動時に自動常駐 |
| オフライン耐性 | ネット切断中はローカル保留、復旧後リトライ |
| 顧客権利 | トレイから一時停止／同意取消＋データ削除 |
| 顧客別EXEビルド | scripts/build_for_customer.sh（VAIOで実行） |

### 2.2 Out of Scope（v1.1-liteでは実装しない）

| 項目 | 理由 |
|---|---|
| スクリーンショット取得 | 顧客抵抗が大きい・本要件で不要 |
| OCRテキスト抽出 | スクショ撤廃により不要 |
| PaddleOCR エンジン同梱 | 同上、EXE軽量化 |
| PIIマスキング（masker.py） | 医療業界水準は不要 |
| raw_capture_mode（USB回収） | Google Drive直送に統一 |
| Supabase Storage アップロード | Google Drive直送に統一 |
| 管理ダッシュボード | 顧客5社超まで手作業運用 |

---

## 3. 取得データ仕様

すべてJSONL形式、1日1ファイル（`%APPDATA%\WorkScope\data\events\YYYY-MM-DD.jsonl`）。
保管期間：ローカル90日、Google Drive側は無期限（運用ルールで定期削除）。

### 3.1 イベント種別

| event_type | 用途 | フィールド例 |
|---|---|---|
| window_focus | アプリ・ウィンドウ切替 | process_name, window_title, started_at, ended_at, duration_ms |
| uia_focus | コントロールにフォーカス | automation_id, name, control_type, parent_path |
| key_typed | キー入力 | key_kind（Tab/Enter/Char/Ctrl+S 等）, char_count（文字キーのみ桁数） |
| mouse_click | クリック | x, y, button, nearest_uia |
| app_category | アプリ分類（裏で付与） | category（SaaS/ERP/Office/業界アプリ/その他） |

### 3.2 取得しない情報（v1.1-liteで明示的に削除）

- 画面ピクセル情報（スクショ）
- OCR抽出テキスト
- 文字キーの実値（桁数のみ記録）
- パスワードフィールド入力（IsPassword=True検出時はキーロギング停止）

---

## 4. Google Drive 直送

### 4.1 保存先

- ドライブ種別：
  - **v1.1-lite 当面：個人マイドライブ**（髙石: 5TB プラン、OAuth Refresh Token 方式）
  - **Workspace 契約後：共有ドライブ**へ移行予定
    - サービスアカウントとの相性が良い
    - 容量がオーナー個人に紐づかない
    - 退職等での所有権移管がない
- ルート：`/WorkScope/`
- 顧客別フォルダ：`/WorkScope/{customer_id}/`
- 日付別：`/WorkScope/{customer_id}/{YYYY-MM-DD}/events_{HHMMSS}.jsonl.gz`
- **保管期間：1年（解析後も含め1年で自動削除）**
  - 削除運用：月次バッチで `created_time < now - 365d` のファイルを削除
  - 削除前に顧客IDごとの集計サマリ（イベント数・期間）を別シートに退避

### 4.2 認証

- 方式：**OAuth Refresh Token方式（v1.1-lite 当面）**
  - 当初は SA (サービスアカウント) で設計したが、Google の仕様で SA には
    storage quota が無く、マイドライブに書き込めない。
  - 共有ドライブが必須だが、共有ドライブは Google Workspace
    (Business Standard 以上) でしか作成できない。
  - 現状の自社契約は個人 Gmail (Google One プラン)。そのため SA 方式は採用不可。
  - 対策として、髙石さん (pondering1083@gmail.com) のマイドライブ (5TB 空き) を
    使い、事前に OAuth で発行した Refresh Token を EXE に埋め込む方式に変更。
- キー：弊社側で1回だけ OAuth 認証 → Refresh Token を取得 → EXE に埋め込み
  （顧客側で Google ログインは不要、顧客 UX は変わらない）
  - 取得手順: `scripts/issue_refresh_token.py path/to/client_secret.json`
  - ビルド時: `build_for_customer.sh --oauth-credentials path/to/oauth.json`
- スコープ：`https://www.googleapis.com/auth/drive.file`（自アプリ作成ファイルのみ）
  - `drive` (full) は Google の restricted scope でブロック中、`drive.file` が最大権限
- **将来移行**: Workspace 契約後は共有ドライブ + SA 方式に戻す予定
  （`gdrive_uploader.py` / `remote_control.py` は SA 形式の Credentials も
  受けられる構造を維持しておく）

### 4.3 アップロード仕様

| 項目 | 値 |
|---|---|
| 送信頻度 | 60分ごと（control.jsonで変更可） |
| 形式 | gzip圧縮JSONL |
| リトライ | 失敗時 指数バックオフ 最大5回 |
| 重複排除 | `%APPDATA%\WorkScope\data\sent_marker.json` |
| 通信 | HTTPS only、TLS1.2以上 |

---

## 5. リモート制御

### 5.1 制御ファイル

Google Drive上の `/WorkScope/{customer_id}/control.json` をCollector側がポーリング。

```json
{
  "status": "active",
  "poll_interval_minutes": 5,
  "upload_interval_minutes": 60,
  "action": null,
  "updated_at": "2026-05-28T15:00:00Z",
  "updated_by": "operator@tribe.example"
}
```

### 5.2 制御コマンド一覧

| status | action | 動作 |
|---|---|---|
| active | null | 通常収集＋アップロード |
| paused | null | 収集停止、トレイ黄●、アップロードは停止前データのみ |
| active | "uninstall" | 自動アンインストーラ起動、データ全削除 |
| active | "force_upload" | 即座に未送信分をアップロード（デバッグ用） |

### 5.3 反映タイミング

- ポーリング間隔：**デフォルト5分（確定）**（control.jsonで変更可、最短1分／最長60分）
- 制御変更から最大 ポーリング間隔ぶん遅延

### 5.4 安全装置

- control.json が読めない場合 → 直前の状態を維持（フェイルセーフ）
- control.json の updated_at が現在時刻より未来 or 30日以上古い → 警告ログ、直前状態維持
- uninstall は10分の取消猶予あり（トレイに警告表示、顧客が「キャンセル」可）

---

## 6. 顧客側UX

### 6.1 初回起動フロー

```
1. メールでDLリンク受領（こちらから送付）
2. WorkScope_{顧客名}_{YYYYMMDD}.exe をダウンロード
3. ダブルクリック
4. SmartScreen警告 → 「詳細情報」→「実行」（手順書で案内）
5. 同意書HTML 1枚表示
6. [同意して開始] ボタン
7. 裏処理:
   - Windowsスタートアップにレジストリ登録
   - トレイ常駐開始（緑●アイコン）
   - 収集スレッド起動
```

### 6.2 2回目以降

- PC起動時に自動常駐（同意画面スキップ）
- 顧客の操作は不要

### 6.3 トレイメニュー

```
WorkScope（緑●｜黄●｜赤×）
  ├ 状態確認
  │   └ 「収集中／停止中／最終アップロード時刻／本日のイベント数」
  ├ 一時停止 / 再開（顧客側でも可）
  ├ アップロード状況
  │   └ 「最終成功:YYYY-MM-DD HH:MM／未送信:NMB」
  ├ ヘルプ
  │   └ 操作手順PDFを開く
  └ 同意取消＋データ削除
      └ 「本当に取消しますか？収集停止＋ローカルデータ全削除」
```

---

## 7. 我々側の運用フロー

v1.1-lite 当面 (OAuth Refresh Token 方式):

```
[顧客契約成立]
    ↓
[customer_id 発番（例: tribe-001）]
[マイドライブ /WorkScope/tribe-001/ 作成]
[OAuth Client (Desktop App) は初回のみ作成、以後は使い回し]
[scripts/issue_refresh_token.py で顧客別 Refresh Token 発行]
    → oauth-tribe-001.json として保管
[control.json 初期版を /WorkScope/tribe-001/ に配置]
    ↓
[VAIOでビルド]
    bash scripts/build_for_customer.sh \
      --mode lite \
      --customer "顧客名" \
      --customer-id "tribe-001" \
      --gdrive-folder-id "xxx" \
      --oauth-credentials "path/to/oauth-tribe-001.json"
    ↓ dist/WorkScope_顧客名_20260528.exe
    ↓
[Google Driveに配布用フォルダを別途作成 → EXE置く → 公開リンク発行]
[顧客にメール送付：DLリンク＋手順書PDF]
    ↓
[顧客が導入]
    ↓
[1ヶ月収集] ← この間、control.json で停止／設定変更可
    ↓
[Google Driveから JSONL を analyzer/ に流して業務マップHTML生成]
    ↓
[改善提案書納品（TRIBE¥30万）]
    ↓
[必要なら control.json で uninstall 指示 → 顧客環境クリーンアップ]
```

Workspace 契約後 (SA + 共有ドライブ方式へ移行) は、以下のみ差分:
- `--oauth-credentials` → `--service-account-key`
- マイドライブ → 共有ドライブ
- 顧客毎に SA 発行 → 共有ドライブの編集権限付与

---

## 8. 技術構成

### 8.1 言語・ライブラリ

| 領域 | 採用 |
|---|---|
| 言語 | Python 3.11 |
| パッケージング | PyInstaller --onefile |
| OS | Windows 10/11（VAIOでビルド） |
| キャプチャ | uiautomation, pywinauto, keyboard, mouse |
| トレイ | pystray, Pillow |
| GDriveクライアント | google-api-python-client, google-auth, google-auth-oauthlib |
| OAuth ビルド前作業 | google-auth-oauthlib（`scripts/issue_refresh_token.py`） |
| ログ | structlog |
| テスト | pytest, pytest-mock |

### 8.2 ディレクトリ構成（v1.0からの差分）

```
workscope-collector/
├── src/
│   ├── collector.py           ← OCR/masker依存を削除
│   ├── uia_capture.py         ← 流用
│   ├── input_events.py        ← 流用
│   ├── app_classifier.py      ← 流用
│   ├── consent.py             ← v1.1-lite向け文言に差し替え
│   ├── tray.py                ← トレイメニュー更新
│   ├── storage.py             ← 流用（ローカル保存のみ）
│   ├── gdrive_uploader.py     ← 【新規】Google Drive直送
│   ├── remote_control.py      ← 【新規】control.json ポーリング
│   ├── uninstaller.py         ← 【新規】自動アンインストール
│   └── main.py                ← OCR/masker初期化を削除、新規モジュール統合
├── scripts/
│   ├── build_for_customer.sh  ← OAuth資格情報のbase64埋め込み手順を追加
│   ├── issue_refresh_token.py ← 【新規】OAuth Refresh Token 取得
│   └── ...
├── docs/
│   ├── SPEC_v1.1-lite.md      ← 本ドキュメント
│   ├── consent_form_lite.html ← 【新規】スクショ記述削除版
│   └── ...
└── tests/
    ├── test_gdrive_uploader.py ← 【新規】
    ├── test_remote_control.py  ← 【新規】
    └── （OCR/masker関連テストは削除）
```

### 8.3 削除するモジュール

- `src/ocr.py`
- `src/masker.py`
- `src/uploader.py`（Supabase用、gdrive_uploader.pyに置換）
- `tests/test_ocr*.py` `tests/test_masker*.py` `tests/test_pii_leak*.py`

---

## 9. セキュリティ・プライバシー

| 項目 | 対応 |
|---|---|
| 通信 | HTTPS only、TLS1.2以上、証明書検証必須 |
| 認証情報 | OAuth Refresh Token (+ client_id/client_secret) を base64 で EXE 埋め込み (v1.1-lite 当面)。将来 Workspace 契約後は SA JSON 暗号化埋め込みへ |
| パスワード保護 | IsPassword=Trueフィールド検出時はキーロギング停止 |
| 文字キー実値 | 記録しない（桁数のみ） |
| 顧客同意 | 初回起動時に同意書HTML表示、同意ボタン押下まで収集開始しない |
| 顧客権利 | トレイから同意取消＋データ削除が可能、その後再起動時に自動アンインストール |
| ログ最小化 | 自社ログにPII相当の情報を含めない（顧客IDとイベント数のみ） |
| アンインストール | レジストリ・スタートアップ・%APPDATA%\WorkScope を全削除 |

---

## 10. テスト方針

### 10.1 開発時（Mac）

```bash
cd workscope-collector
source .venv/bin/activate
pytest tests/ -v
```

- 単体テストのみ（Windows依存モジュールはモック）
- 既存約310テストのうち、Lite版で残すもの＋新規追加
- 新規テスト最低限:
  - `test_gdrive_uploader.py`：モックドライブへの送信／リトライ／重複排除
  - `test_remote_control.py`：control.json解釈／フェイルセーフ／反映遅延
  - `test_uninstaller.py`：レジストリ・ファイル削除の網羅

### 10.2 VAIO（Windows）でのビルド・実機テスト

#### 初回セットアップ

```cmd
:: 1. 開発環境（VAIOで未セットアップなら実施）
git clone https://github.com/shoohey/workscope-collector
cd workscope-collector
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

:: 2. テスト用Google Drive準備（v1.1-lite 当面: マイドライブ + OAuth）
::    - 髙石マイドライブ配下に WorkScope/test-tribe-001/ フォルダ作成
::    - OAuth Client (Desktop App) を GCP で作成 (初回のみ)
::    - python scripts/issue_refresh_token.py client_secret.json > oauth-test.json
:: （Workspace 契約後はサービスアカウント + 共有ドライブに切替）
```

#### ビルド

```cmd
:: 顧客専用EXEをビルド
bash scripts\build_for_customer.sh ^
  customer="テスト顧客" ^
  customer_id="test-001" ^
  gdrive_folder_id="xxxxxxxxxxxxx" ^
  service_account_key="C:\keys\test-sa.json"

:: → dist\WorkScope_テスト顧客_20260528.exe（約50MB想定）
```

### 10.3 VAIO実機テストチェックリスト

#### 機能確認（A. 起動・常駐）

- [ ] ダブルクリック → 同意書HTML表示
- [ ] [同意して開始] でトレイに緑●が出る
- [ ] タスクマネージャの起動アプリにレジストリ登録される
- [ ] PC再起動後に自動常駐
- [ ] 同意書を「同意しない」で閉じると常駐せず終了

#### 機能確認（B. 収集）

- [ ] 10分間、複数アプリを操作 → ローカルJSONLにイベント記録
- [ ] window_focus / uia_focus / key_typed / mouse_click すべて記録
- [ ] パスワード入力欄でキー記録が停止する
- [ ] 文字キーは桁数のみ、実値は記録されない

#### 機能確認（C. アップロード）

- [ ] 1時間経過 → Google Drive に gzip がアップロードされる
- [ ] パス：`/WorkScope/test-001/2026-05-28/events_HHMMSS.jsonl.gz`
- [ ] Wi-Fi切断 → 5分後復旧 → 未送信分が後追いでアップロード
- [ ] 送信済データはローカルマーカーで記録、再送されない

#### 機能確認（D. リモート制御）

- [ ] control.json を `status: paused` に変更 → 5分以内にトレイ黄●
- [ ] control.json を `status: active` に戻す → 5分以内に緑●
- [ ] control.json で `poll_interval_minutes: 1` に変更 → 1分間隔ポーリング
- [ ] control.json で `action: uninstall` → 10分の警告後にアンインストール
- [ ] アンインストール後、レジストリ・スタートアップ・%APPDATA%\WorkScope すべて削除

#### 機能確認（E. 顧客操作）

- [ ] トレイ「一時停止」→ 黄●＋収集停止
- [ ] トレイ「再開」→ 緑●＋収集再開
- [ ] トレイ「同意取消＋データ削除」→ 確認ダイアログ → 削除実行

#### 性能・サイズ

- [ ] CPU使用率 平均1%未満（タスクマネージャ確認）
- [ ] メモリ使用量 200MB未満
- [ ] EXEサイズ 100MB未満
- [ ] 24時間連続稼働でクラッシュなし

### 10.4 顧客環境パイロットテスト（1社目）

- [ ] 配布手順書だけでITに詳しくない人が導入完了
- [ ] 1週間連続稼働
- [ ] 業務時間帯（8h）の操作が漏れなくJSONLに記録
- [ ] 顧客の通常業務に体感的な影響なし
- [ ] こちら側で control.json 操作 → 即座に反映確認

---

## 11. リリース手順

```
1. main ブランチで全テストグリーン
2. tag v1.1-lite-rc1 を打つ
3. VAIOでテスト顧客向けEXEビルド
4. VAIO実機テストチェックリスト完走
5. パイロット1社で1週間稼働
6. 問題なければ tag v1.1.0 を打つ
7. 以後、顧客契約のたびに顧客専用EXEをビルド・配布
```

---

## 12. 既知の制約・決定事項

### 12.1 制約

- macOS / Linux 非対応（Windows専用）
- 顧客のGoogleアカウント連携は不要だが、自社側のGoogle Workspace契約が必須（共有ドライブ利用のため）
- SmartScreen警告は回避策なし → **顧客から事前に同意取得済のため、手順書でクリック方法を案内する運用で対応**

### 12.2 決定事項（2026-05-28 確定）

| # | 項目 | 決定値 |
|---|---|---|
| 1 | ドライブ種別 | **個人マイドライブ + OAuth Refresh Token**（当面）／**Workspace契約後に共有ドライブ + SA へ移行予定** |
| 2 | デフォルトポーリング間隔 | **5分**（control.jsonで変更可） |
| 3 | 顧客ID命名規則 | **tribe-{連番3桁}**（例: tribe-001, tribe-002...） |
| 4 | コード署名証明書購入 | **不要**（顧客に事前同意取得済、手順書で対応） |
| 5 | Google Drive側データ保管期間 | **1年**（月次バッチで自動削除、集計サマリのみ別保管） |
| 6 | VAIO開発環境 | **整備済**（Python 3.11 / Git / VSCode 利用可） |
| 7 | 認証方式（v1.1-lite 当面） | **OAuth Refresh Token（個人 Gmail のマイドライブ運用）** |

#### 影響範囲（OAuth Refresh Token 方式の当面採用）

| 観点 | 影響 |
|---|---|
| 容量 | 髙石さんのマイドライブ（5TB 空き）を消費する。顧客5社×1年で想定数十GB、当面問題なし |
| 退職・担当替え | 髙石アカウント停止時にアップロード停止 → Workspace 移行を急ぐトリガー |
| 顧客 UX | 変化なし（顧客は Google ログイン不要、EXE ダブルクリックで完結） |
| データ所有 | 髙石個人のマイドライブ内 → 社内ガバナンス上は早期に Workspace へ移行すべき |
| Refresh Token 漏洩 | scope は drive.file 限定、漏洩時は GCP コンソールで該当 OAuth Client を Revoke |
| Workspace 契約後の移行 | `--oauth-credentials` を `--service-account-key` 系オプションに戻すだけ、コード側は本構造で SA 形式 Credentials も受けられる設計を維持 |

---

## 13. 工数見積（概算）

| フェーズ | 内容 | 工数 |
|---|---|---|
| 設計 | 本要件定義の確定＋Google Drive構成設計 | 0.5日 |
| 実装 | gdrive_uploader / remote_control / uninstaller 新規＋既存OCR削除 | 3日 |
| ビルドスクリプト改修 | OAuth資格情報base64埋め込み + issue_refresh_token.py 作成 | 0.5日 |
| 同意書改訂 | スクショ記述削除版作成 | 0.5日 |
| テスト（Mac単体） | pytest更新＋新規テスト | 1日 |
| VAIOビルド・実機テスト | チェックリスト完走 | 1日 |
| パイロット | 顧客1社で1週間稼働観察 | 別途 |
| **合計（パイロット除く）** |  | **6.5日** |

---

## 14. 関連ドキュメント

- v1.0仕様：`docs/SPEC_v1.0.md`
- 同意書v1.1-lite版：`docs/consent_form_lite.html`（要作成）
- 操作ガイド：`docs/operation_guide.html`（v1.1-lite向けに改訂要）
- インシデント対応：`docs/INCIDENT_RUNBOOK.md`（流用）
- データ取扱ポリシー：`docs/data_handling_policy.html`（v1.1-lite向けに改訂要）
