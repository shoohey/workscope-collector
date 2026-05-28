# VAIO ビルド・実機テスト手順書（WorkScope v1.1-lite）

- 対象: 弊社（株式会社TRIBE）側のビルド担当者（髙石ほか）
- 目的: VAIO（Windows）で顧客別 EXE をビルドし、実機テストして配布まで一気通貫で行う
- 想定所要時間: 初回60分／2社目以降30〜40分
- 関連: `docs/SPEC_v1.1-lite.md`、`docs/GDRIVE_SETUP.md`

> Google Drive 側の準備（マイドライブにフォルダ作成・OAuth Refresh Token 発行・control.json 配置）は本ドキュメントでは扱わない。
> 先に `docs/GDRIVE_SETUP.md` を完走して **GDRIVE_FOLDER_ID / OAuth 資格情報 JSON / CUSTOMER_ID** の3点を揃えてから本手順に入ること。
>
> **【重要】2026-05-28 認証方式変更**: SA (サービスアカウント) → OAuth Refresh Token に変更。
> ビルドコマンドの引数も `--service-account-key` → `--oauth-credentials` に変更。詳細は §6 を参照。

---

## 1. VAIO 初回セットアップ確認

VAIO は既に Python 3.11 / Git / VSCode が整備済の前提だが、念のためバージョン確認をする。

```cmd
python --version
git --version
code --version
```

期待する出力:
```
Python 3.11.x
git version 2.x.x.windows.x
1.xx.x
```

> Python が 3.11 以外、もしくは未インストールの場合は https://www.python.org/downloads/ から Python 3.11.x をインストール。
> 「Add python.exe to PATH」のチェックを必ず入れること。

### 1.1 補助ツール確認

bash / rsync / sed を使う場面があるため、**Git Bash** または **WSL** が利用できることを確認する。

```cmd
bash --version
```

→ 「'bash' は、内部コマンドまたは外部コマンド...として認識されていません」と出た場合は Git for Windows を再インストールし、インストール時に「Git Bash Here」を有効にする。

---

## 2. リポジトリ clone

```cmd
cd C:\workspace
git clone https://github.com/shoohey/workscope-collector
cd workscope-collector
```

> `C:\workspace` フォルダが無い場合は事前に作成: `mkdir C:\workspace`

期待する状態:
- `C:\workspace\workscope-collector\` が作成される
- 内部に `src\`、`scripts\`、`docs\`、`profiles\` 等が見える

### 2.1 最新化（2社目以降のとき）

既に clone 済みの場合は最新化のみ:

```cmd
cd C:\workspace\workscope-collector
git checkout main
git pull origin main
```

---

## 3. 依存インストール

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-lite.txt
```

> `requirements-lite.txt` は v1.1-lite 専用の依存定義（OCR / PaddleOCR を除外し、`google-api-python-client` / `google-auth` / `google-auth-oauthlib` を追加）。
> `google-auth-oauthlib` は OAuth Refresh Token 取得スクリプト (`scripts/issue_refresh_token.py`) で使用。

期待する状態:
- プロンプトの先頭に `(.venv)` が付く
- `pip list` で `pyinstaller`、`google-api-python-client`、`pywin32`、`pystray` 等が表示される

### 3.1 pywin32 のポストインストール（必要に応じて）

`pywin32` は import 時にエラーが出る場合があるため、ポストインストールを実行:

```cmd
python .venv\Scripts\pywin32_postinstall.py -install
```

---

## 4. テスト実行（任意・推奨）

ビルド前に単体テストを通しておく。

```cmd
pytest tests/
```

期待する出力:
```
=========================== 349 passed in 1.99s ============================
```

> 失敗テストが出た場合は、新規追加モジュール（`gdrive_uploader.py` / `remote_control.py` / `uninstaller.py`）の実装漏れ・モック不足の可能性。
> 失敗を無視してビルドに進むと、顧客環境で動かないリスクがあるため、必ず原因切り分けしてから次工程へ。

---

## 5. GDRIVE_SETUP.md の完了確認

以下3点が手元に揃っていることを確認する（揃っていなければ `docs/GDRIVE_SETUP.md` に戻る）。

| 項目 | 例 | 取得元 |
|---|---|---|
| `GDRIVE_FOLDER_ID` | `1AbCdEfGhIjKlMnOpQrStUvWxYz12345` | GDRIVE_SETUP.md §7.2 |
| OAuth 資格情報 JSON のパス | `C:\keys\oauth-tribe-001.json` | GDRIVE_SETUP.md §5.2 |
| `CUSTOMER_ID` | `tribe-001` | GDRIVE_SETUP.md §7.1 |

### 5.1 OAuth 資格情報の配置

VAIO 上に `C:\keys\` フォルダを作成し、`scripts/issue_refresh_token.py` で生成した OAuth 資格情報 JSON を配置する。

```cmd
mkdir C:\keys
copy "Macから持ち込み\oauth-tribe-001.json" C:\keys\
```

JSON の中身（参考）:
```json
{
  "refresh_token": "1//0abc...",
  "client_id": "xxx.apps.googleusercontent.com",
  "client_secret": "GOCSPX-..."
}
```

> `C:\keys\` フォルダは **エクスプローラのアクセス権を自分のみに制限** すること（プロパティ → セキュリティタブ → 編集）。
> Git リポジトリ内には絶対に置かない（誤コミット防止）。

---

## 6. 顧客向け EXE ビルド

```cmd
bash scripts\build_for_customer.sh ^
  --customer "テスト顧客" ^
  --customer-id "tribe-001" ^
  --mode lite ^
  --gdrive-folder-id "1AbCdEfGhIjKlMnOpQrStUvWxYz12345" ^
  --oauth-credentials "C:\keys\oauth-tribe-001.json"
```

> `--mode lite` は v1.1-lite ビルド用フラグ。OCR / マスカー / Supabase アップローダを除外し、Google Drive 直送モジュールを組み込む。
> `--oauth-credentials` は OAuth Refresh Token 方式の資格情報 JSON のパス。
> （旧 `--service-account-key` から名称変更。旧オプションは互換のために残してあるが警告メッセージが出る）
> `^` は Windows cmd の行継続記号（PowerShell の場合はバッククォート `` ` `` に置き換え）。
> 引数に半角スペースを含む値（顧客名やパス）は必ずダブルクォートで囲むこと。

### 6.1 ビルドの進行

`[1/5]` 〜 `[5/5]` のログが出る:
- `[1/5] copy repo → /tmp/workscope_build_XXXXXX`
- `[2/5] reduce profiles to {base, generic}`（lite モードは generic プロファイル固定）
- `[3/5] generate src/_build_constants.py`（OAuth 資格情報を base64 埋め込み）
- `[4/5] render consent_form_lite.html with customer name`
- `[5/5] PyInstaller build (this takes a few minutes)...`

所要時間: 3〜5分（VAIO のスペック次第）

### 6.2 出力ファイルの確認

```cmd
dir dist\
```

期待する出力:
```
WorkScope_テスト顧客_20260528.exe       約 50 MB
```

> サイズが100MB を超える場合は OCR / PaddleOCR が紛れ込んでいる可能性。`pyinstaller.spec` の `excludes` を確認すること。
> サイズが10MB 未満の場合は依存モジュールが取り込まれていない可能性。`--collect-all` の対象を見直す。

---

## 7. 実機テストチェックリスト

VAIO 自身を「顧客 PC のスタンドイン」として、生成 EXE を流し込んでテストする。
**全項目 ✓ を確認するまで顧客に配布しないこと。**

### A. 起動・常駐

| | 項目 |
|---|---|
| ☐ | `dist\WorkScope_テスト顧客_20260528.exe` をダブルクリック |
| ☐ | SmartScreen 警告が出る → 「詳細情報」→「実行」（手順書で顧客に案内する内容）|
| ☐ | 同意書HTML（consent_form_lite.html）が表示される |
| ☐ | [同意して開始] をクリック |
| ☐ | タスクトレイに **緑●** アイコンが表示される |
| ☐ | レジストリ確認: `reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v WorkScope` でエントリが見える |
| ☐ | PC 再起動 → 自動的にトレイ常駐（同意画面は出ない）|

### B. 収集

| | 項目 |
|---|---|
| ☐ | 10分間、複数アプリ（Chrome / Excel / Notepad 等）を切り替え操作 |
| ☐ | `%APPDATA%\WorkScope\data\events\YYYY-MM-DD.jsonl` が生成される |
| ☐ | JSONL 内に `window_focus` / `uia_focus` / `key_typed` / `mouse_click` の各イベントが記録 |
| ☐ | パスワード入力欄（例: Windows ログオン画面プレビュー、ブラウザのパスワード欄）で キー入力が停止する |
| ☐ | `key_typed` イベントに **文字キーの実値** が含まれていない（`char_count` のみ） |

### C. アップロード

| | 項目 |
|---|---|
| ☐ | 1時間経過まで待つ（または control.json で `action: force_upload` を設定して即時実行） |
| ☐ | Google Drive の `WorkScope/tribe-001/2026-05-28/` 配下に `events_HHMMSS.jsonl.gz` が出現 |
| ☐ | gzip を解凍して中身が JSONL になっていることを確認 |
| ☐ | Wi-Fi を切断 → 5分後に復旧 → 未送信分が自動的に後追いアップロードされる |
| ☐ | `%APPDATA%\WorkScope\data\sent_marker.json` に送信済 ID が記録されている |
| ☐ | 同じファイルが二重送信されない |

### D. リモート制御

| | 項目 |
|---|---|
| ☐ | Drive 上の `control.json` を `"status": "paused"` に変更 → 5分以内にトレイが **黄●** に切り替わる |
| ☐ | `"status": "active"` に戻す → 5分以内に **緑●** に戻る |
| ☐ | `"poll_interval_minutes": 1` に変更 → 1分間隔でポーリングが走る（ログで確認） |
| ☐ | フェイルセーフ: control.json を不正な JSON にする → 直前状態が維持される（ログに WARN） |
| ☐ | フェイルセーフ: `updated_at` を未来日付にする → 直前状態が維持される（ログに WARN） |

### E. アンインストール

| | 項目 |
|---|---|
| ☐ | control.json で `"action": "uninstall"` を設定 |
| ☐ | 5分以内にトレイに「10分後にアンインストールします」の警告表示 |
| ☐ | 10分経過後、自動的に `%APPDATA%\WorkScope\` が削除される |
| ☐ | レジストリ確認: `reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v WorkScope` → 「見つかりません」 |
| ☐ | EXE 自身も削除される（または次回起動時に自己削除） |
| ☐ | トレイアイコンが消える |

### F. 顧客操作

| | 項目 |
|---|---|
| ☐ | トレイ右クリック → 「一時停止」→ 黄●＋収集停止 |
| ☐ | トレイ右クリック → 「再開」→ 緑●＋収集再開 |
| ☐ | トレイ右クリック → 「同意取消＋データ削除」→ 確認ダイアログ → Yes で `%APPDATA%\WorkScope\` 削除 |
| ☐ | トレイ右クリック → 「アップロード状況」→ 最終成功時刻と未送信量が表示される |
| ☐ | トレイ右クリック → 「ヘルプ」→ 操作手順 PDF が開く |

### G. 性能

| | 項目 |
|---|---|
| ☐ | タスクマネージャで CPU 使用率が **平均 1% 未満**（10分平均） |
| ☐ | タスクマネージャでメモリ使用量が **200MB 未満** |
| ☐ | `dist\WorkScope_テスト顧客_20260528.exe` のサイズが **100MB 未満** |
| ☐ | 24時間連続稼働してクラッシュなし（パイロット前の最終確認時のみ） |

> A〜G の全項目 ✓ になったらビルド完了。1つでも ✗ があれば原因切り分け → コード修正 → 再ビルド。

---

## 8. 顧客への配布

### 8.1 配布用フォルダへ EXE をアップロード

1. Google Drive で **「配布用」フォルダ**（共有ドライブ内に1つ作成しておく、顧客フォルダとは別）を開く
2. `dist\WorkScope_テスト顧客_20260528.exe` をドラッグ＆ドロップでアップロード
3. アップロード完了を待つ

> 「配布用」フォルダはサービスアカウントには共有しないこと（顧客環境からアクセスする必要なし）。

### 8.2 公開リンクの発行

1. アップロードした EXE を右クリック → **「リンクを取得」**
2. 「リンクを知っている全員」に変更 → 権限は **「閲覧者」**
3. 「リンクをコピー」

> 「リンクを知る全員」の発行は社内ポリシーで承認が必要な場合は事前確認。
> 顧客ごとに別ファイル名のため、URL を知っていても他社の EXE はダウンロードされない（ファイル名に顧客名入り）。

### 8.3 顧客へのメール送付

メールに以下を添付・記載:

| 項目 | 内容 |
|---|---|
| 件名 | 【TRIBE】WorkScope Collector のダウンロード手順について |
| 本文1 | DL リンク（手順8.2で取得した URL） |
| 添付1 | 操作手順 PDF（`docs/operation_guide.html` を PDF 変換） |
| 添付2 | 同意書 PDF（事前に署名済の控え）|
| 本文末 | SmartScreen 警告が出た場合の対処（詳細情報 → 実行）|

### 8.4 配布後の社内記録

| 項目 |
|---|
| 顧客名 / customer_id（tribe-001）|
| ビルド日 |
| EXE ハッシュ（`certutil -hashfile dist\WorkScope_*.exe SHA256`）|
| OAuth 資格情報 JSON の保管場所 |
| GDRIVE_FOLDER_ID |
| 配布日時・配布先メールアドレス |

→ 社内ナレッジ（Notion / Google Sheets 等）に残す。

---

## 9. トラブルシューティング

### 9.1 「ビルドが失敗する」

確認順序:
1. **PyInstaller の再インストール**
   ```cmd
   pip uninstall -y pyinstaller
   pip install pyinstaller==6.4.0
   ```
2. **--clean フラグ追加で再ビルド**
   - `build_for_customer.sh` は内部で `pyinstaller --clean --noconfirm` を呼んでいるが、念のため手動でも `build\` と `dist\` フォルダを削除してから再実行
   ```cmd
   rmdir /S /Q build
   rmdir /S /Q dist
   ```
3. **ログ確認**
   - PyInstaller の出力末尾20行を確認
   - `ModuleNotFoundError` → `pyinstaller.spec` の `hiddenimports` に追加
   - `Permission denied` → アンチウィルスが build フォルダをロックしている可能性、一時的に除外設定

### 9.2 「EXE が SmartScreen でブロックされる」

これは仕様（コード署名証明書を購入しない方針のため）。

顧客向け案内:
- 「Windows によって PC が保護されました」画面が出る
- **「詳細情報」** をクリック
- **「実行」** ボタンが現れるのでクリック

> SPEC v1.1-lite §12.2 で「顧客から事前同意取得済のため手順書で対応」と決定済。
> ビルド側で回避策はない（証明書購入¥10万/年が必要、現状コスト判断で見送り）。

### 9.3 「pywin32 が import できない」

実機テスト時に EXE 起動でクラッシュ、ログに `ImportError: No module named 'win32api'` が出るケース。

```cmd
pip uninstall -y pywin32
pip install pywin32==307
python .venv\Scripts\pywin32_postinstall.py -install
```

その後、再ビルド。

> ポストインストールスクリプトは pywin32 を pip でインストールしても自動実行されないため、手動実行が必須。

### 9.4 「Google Drive へのアップロードが失敗する」

確認順序:
1. **OAuth 資格情報の読み込み確認**
   - EXE 起動時のログに `failed to decode oauth credentials` / `oauth credentials missing key` が出ていないか
   - JSON が破損している場合は GDRIVE_SETUP.md §5 から再発行
2. **GDRIVE_FOLDER_ID の typo**
   - 25〜33文字の英数字。スペースや改行が混ざっていないか
3. **Refresh Token の有効性**
   - ログに `invalid_grant` / `Token has been expired or revoked` が出ていないか
   - 出ていれば GDRIVE_SETUP.md §5 を再実行 → 新 Refresh Token で再ビルド → 顧客に再配布
4. **API 制限**
   - 1日 1,000,000,000 リクエストが上限。通常は超えない
   - 短時間に大量アップロードしている場合は `upload_interval_minutes` を伸ばす

### 9.5 「テストでは動くが顧客 PC で動かない」

確認順序:
1. **VAIO と顧客 PC の Windows バージョン違い**
   - Windows 10 21H2 / Windows 11 22H2 など、サポート範囲を確認
2. **顧客 PC のセキュリティソフト**
   - ESET / McAfee / Symantec が EXE をブロック・隔離する場合あり
   - 顧客に「除外設定」を依頼
3. **顧客 PC の管理者権限**
   - スタートアップ登録には HKCU（現ユーザー）のレジストリ書込み権限が必要
   - 通常ユーザーで OK だが、ポリシー制限がある企業環境では NG

---

## 10. 次回以降の顧客向け差分

2社目以降は以下のみ変更すれば本ドキュメントの手順がそのまま再利用できる。

| 項目 | 1社目 | 2社目 | 3社目 |
|---|---|---|---|
| `CUSTOMER_ID` | `tribe-001` | `tribe-002` | `tribe-003` |
| `--customer` | `"テスト顧客"` | `"〇〇株式会社"` | `"△△商事"` |
| `GDRIVE_FOLDER_ID` | `1AbCd...` | （GDRIVE_SETUP.md §7.2 で取得した新フォルダ ID） | 同左 |
| OAuth 資格情報 JSON | `C:\keys\oauth-tribe-001.json` | `C:\keys\oauth-tribe-002.json` | `C:\keys\oauth-tribe-003.json` |

### 10.1 注意点

- **OAuth Refresh Token は顧客ごとに必ず新規発行**（前回のものは流用しない）
  - 漏洩時の被害を顧客単位に局所化するため
  - GDRIVE_SETUP.md §5 を毎回実行
- **顧客フォルダも顧客ごとに新規作成**
  - 同じフォルダに複数顧客のデータを混ぜない
- **OAuth Client (client_secret.json)・Cloud プロジェクト・配布用フォルダは全社共通で使い回し**
  - 毎回作る必要はない（OAuth Client は §4 で1回だけ作成）

---

## 11. 関連ドキュメント

- 仕様書: `docs/SPEC_v1.1-lite.md`
- Google Drive 側準備: `docs/GDRIVE_SETUP.md`
- 顧客向け操作ガイド: `docs/operation_guide.html`
- インシデント対応: `docs/INCIDENT_RUNBOOK.md`
- v1.0（薬局向け）パイロット手順: `docs/PILOT_INSTALL_CHECKLIST.md`
