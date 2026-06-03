# Macだけで顧客EXEをビルドする手順（AppVeyor）

PyInstaller は**クロスビルド不可**（Mac 上で Windows 用 .exe は作れない）。
さらに、このアカウントでは **GitHub Actions がアカウントレベルで無効**化されている。
そのため Windows ビルドは **AppVeyor**（`appveyor.yml`）で行う。Windows 実機は不要で、
Mac のブラウザ／CLI から AppVeyor の「NEW BUILD」を起動すれば EXE が手に入る。

- AppVeyor プロジェクト: https://ci.appveyor.com/project/shoohey/workscope-collector
- ビルド対象は常に **main ブランチの最新コミット**（顧客別ビルドも main 上のコードを使う）

---

## A. Lite 版（v1.1-lite / 一般企業向け・Google Drive 直送）

### 手順1: OAuth 資格情報を base64 化（Mac）

その顧客の OAuth 資格情報 JSON（`scripts/issue_refresh_token.py` で発行した
`oauth-tribe-XXX.json`。`refresh_token`/`client_id`/`client_secret` を含む）を base64 にする:

```bash
base64 -i oauth-tribe-001.json | tr -d '\n' | pbcopy   # クリップボードにコピー
```

### 手順2: AppVeyor で NEW BUILD（環境変数を上書き）

AppVeyor プロジェクト → **NEW BUILD** → 「Environment variables」(Advanced) で以下を設定して Build:

| 変数 | 値 | 例 |
|---|---|---|
| `CUSTOMER_NAME` | 顧客名 | `テスト顧客` |
| `MODE` | `lite` 固定 | `lite` |
| `CUSTOMER_ID` | 顧客ID | `tribe-001` |
| `GDRIVE_FOLDER_ID` | Drive 顧客フォルダID（URLの /folders/{ID}） | `1AbCdEf...` |
| `GDRIVE_OAUTH_CREDENTIALS_B64` | 手順1の base64（**Secure variable** にする） | `eyJ...`（長い） |
| `INDUSTRY` | 任意（省略時 generic） | `generic` |

> `GDRIVE_OAUTH_CREDENTIALS_B64` は必ず **「Secure」チェック**を付ける（ログ・履歴に平文を残さない）。
> ビルドスクリプトもこの値をログに出力しない設計（ファイルへ直書き）。

### 手順3: 成果物をダウンロード

ビルド完了後、AppVeyor のビルドページ → **Artifacts** から
`WorkScope_<...>_<sha>.zip` を Mac にDL。zip 内に以下が入る:
- `WorkScope_<顧客>_<日付>.exe`（顧客に配布する本体）
- `consent_form.html`（lite 用同意書・顧客情報置換済）
- `operation_guide.html` ほか

---

## B. Full 版（v1.0 / 薬局向け・スクショ+OCR+Supabase）

NEW BUILD の環境変数:

| 変数 | 値 |
|---|---|
| `CUSTOMER_NAME` | 顧客名（例: 村上薬局） |
| `INDUSTRY` | 業界プロファイル（pharmacy 等） |
| `ENDPOINT` | アップロード先URL（USB回収なら空） |
| `API_KEY` | Bearer APIキー（USB回収なら空） |

`MODE` は設定しない（空＝full）。

---

## C. CLI から起動したい場合（任意・AppVeyor REST API）

AppVeyor の Account → API Token を取得して `APPVEYOR_TOKEN` に入れる:

```bash
curl -s -X POST https://ci.appveyor.com/api/builds \
  -H "Authorization: Bearer $APPVEYOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "accountName": "shoohey",
    "projectSlug": "workscope-collector",
    "branch": "main",
    "environmentVariables": {
      "CUSTOMER_NAME": "テスト顧客",
      "MODE": "lite",
      "CUSTOMER_ID": "tribe-001",
      "GDRIVE_FOLDER_ID": "1AbCdEf...",
      "GDRIVE_OAUTH_CREDENTIALS_B64": "'"$(base64 -i oauth-tribe-001.json | tr -d '\n')"'"
    }
  }'
```

> CLI の場合は base64 が履歴・プロセス一覧に残りうる点に注意（Web UI の Secure variable の方が安全）。

---

## 注意・既知の制約

- **実機検証は別途必要**: AppVeyor のスモークは「起動→5秒→終了」まで。
  デュアルディスプレイ＋DPI混在の Windows 実機で、key_typed/mouse_click の発火と
  CPU 負荷（目標 平均1%未満）を最終確認すること。
- **GitHub Actions は使えない**: アカウントレベルで無効（repo 設定では enabled でも
  `Actions has been disabled for this user` になる）。再有効化できた場合に備えて
  `appveyor.yml` と同等の処理を Actions 化することは可能だが、現状は AppVeyor が唯一の経路。
- 入力捕捉は `pynput` に依存（`requirements*.txt` に同梱済）。これが無いと
  key/mouse が一切収集されないため、依存を削らないこと。
