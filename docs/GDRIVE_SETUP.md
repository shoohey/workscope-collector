# Google Drive セットアップ手順書（WorkScope v1.1-lite）

- 対象: 弊社（株式会社TRIBE）側の運用担当者（髙石ほか）
- 目的: 新規顧客向けに Google Drive 側の受け皿を準備し、ビルド時に渡す値を揃える
- 想定所要時間: 初回40分／2社目以降10分（OAuth Client は初回のみ作成）
- 関連: `docs/SPEC_v1.1-lite.md` の §4「Google Drive 直送」と §5「リモート制御」

> このドキュメントは Google Drive 側の準備だけを扱う。
> VAIO 上でのビルド・実機テストは `docs/VAIO_BUILD_GUIDE.md` を参照すること。

> **【重要】2026-05-28 認証方式変更**
> 当初 SA (サービスアカウント) 方式で設計したが、Google の仕様で SA は
> マイドライブに書き込めず、共有ドライブが必須。共有ドライブは Workspace
> (Business Standard 以上) でのみ作成可能。
> 現状の自社契約は個人 Gmail (Google One) のため、SA 方式は採用不可。
> → **OAuth Refresh Token 方式に変更し、髙石さんのマイドライブ (5TB 空き) を使う。**
> Workspace 契約後は共有ドライブ + SA 方式に戻す予定。

---

## 1. 概要

### 1.1 なぜ Google Drive 直送か

v1.0（薬局向け）では Supabase Storage + USB 物理回収だったが、v1.1-lite では一般企業向けに以下の理由から Google Drive 直送に統一した。

- 顧客側のセットアップ不要（顧客に Google アカウントを作らせない）
- 自社の Google Workspace（or Google One）契約をそのまま再利用できる
- OAuth は弊社側で1回完結、顧客 PC で同意フローが走らない
- 月次の容量管理・削除運用を Drive 1 か所に集約できる

### 1.2 v1.1-lite 当面の運用：マイドライブ + OAuth Refresh Token

| 項目 | 当面（v1.1-lite） | Workspace 契約後 |
|---|---|---|
| 認証方式 | **OAuth Refresh Token**（髙石アカウントで1回認証） | サービスアカウント |
| ドライブ種別 | **個人マイドライブ**（髙石: 5TB 空き） | 共有ドライブ |
| 顧客フォルダ親 | マイドライブ配下 `WorkScope/tribe-001/` | 共有ドライブ配下 `WorkScope/tribe-001/` |
| 顧客 UX | 変化なし（EXE ダブルクリックで完結） | 同左 |
| OAuth Client | 1個を全顧客で共用（drive.file scope） | 不要（SA を顧客毎発行） |
| 移行容易性 | コード側は SA 形式 Credentials も受けられる設計を維持 | — |

**なぜ SA を当面使わないのか:**
- Google の仕様で SA には storage quota が無く、マイドライブには書き込めない
- 共有ドライブが必須だが、共有ドライブは Workspace (Business Standard 以上) でしか作れない
- 現状の自社契約は個人 Gmail (Google One プラン) のため共有ドライブを作れない

---

## 2. 前提条件

| 項目 | 要件 |
|---|---|
| Google アカウント | **個人 Gmail で可**（髙石: pondering1083@gmail.com を使用） |
| Google One 等のストレージ | **2TB 以上推奨**（顧客 5 社 × 1 年で数十 GB 消費） |
| ブラウザ | Chrome 推奨（Google サービスの最適化のため） |
| Google Cloud Console | 同じ Google アカウントでログインできること |
| Python 環境 | OAuth Refresh Token 取得用に `google-auth-oauthlib` が動く環境 |

> Workspace に移行できれば共有ドライブ + SA 方式に切り替え可能。詳細は SPEC_v1.1-lite.md §12.2。

---

## 3. 手順1: マイドライブに WorkScope ルートフォルダを作成（初回のみ・約2分）

1. https://drive.google.com にアクセス（髙石アカウント）
2. 左メニューの **「マイドライブ」** を選択
3. 右クリック → **「新しいフォルダ」** → 名前 **`WorkScope`** で作成
4. 中央ペインに `WorkScope` フォルダが表示されることを確認

期待する状態:
- マイドライブ直下に `WorkScope/` フォルダがある

> 共有ドライブを作る必要は **無い**（当面はマイドライブ運用）。

---

## 4. 手順2: Google Cloud Project + OAuth Client 作成（初回のみ・約15分）

### 4.1 プロジェクト作成

1. https://console.cloud.google.com にアクセス（髙石アカウント）
2. 上部のプロジェクト選択ドロップダウン → **「新しいプロジェクト」**
3. プロジェクト名: **`workscope-collector`**
4. 組織: 「組織なし」でOK（個人 Gmail 用）
5. 「作成」をクリック

期待する状態:
- ヘッダーに「workscope-collector」が表示される
- 「ようこそ」ダッシュボードが開く

### 4.2 Drive API の有効化

1. 左メニュー → **「APIとサービス」** → **「ライブラリ」**
2. 検索バーに **`Google Drive API`** と入力
3. 「Google Drive API」をクリック → **「有効にする」** ボタン
4. 数秒で「APIが有効化されました」と表示される

期待する状態:
- ライブラリの Drive API 詳細画面に「APIを管理」「無効にする」のボタンが表示される（＝有効化済）

### 4.3 OAuth 同意画面の構成

1. 左メニュー → **「APIとサービス」** → **「OAuth 同意画面」**
2. User Type: **「外部」** を選択 → 作成
3. アプリ情報:
   - アプリ名: `WorkScope Collector`
   - ユーザーサポートメール: 髙石アドレス
   - デベロッパー連絡先: 髙石アドレス
4. スコープ → **「スコープを追加または削除」**:
   - `https://www.googleapis.com/auth/drive.file` を追加
   - （drive、drive.metadata 等の上位スコープは追加しない）
5. テストユーザー → **「ADD USERS」**:
   - **`pondering1083@gmail.com`**（または認証するアカウント）を追加
6. 概要 → 保存

> 「公開」ステータスは **テスト** のままで OK（弊社内部利用のため）。
> 公開モードに変えると Google の審査が必要になるが、それは不要。

### 4.4 OAuth Client ID (Desktop App) の作成

1. 左メニュー → **「APIとサービス」** → **「認証情報」**
2. 上部 **「+ 認証情報を作成」** → **「OAuth クライアント ID」**
3. アプリケーションの種類: **「デスクトップアプリ」**
4. 名前: **`workscope-collector-cli`**
5. 「作成」
6. ダイアログに client_id / client_secret が表示される → **「JSON をダウンロード」**
7. ダウンロードした `client_secret_xxx.json` を `~/keys/` 等に保管

> このファイルは **全顧客で 1 個を共用**。顧客ごとに作る必要はない。
> 顧客ごとに発行するのは「Refresh Token」のほう（手順5）。

---

## 5. 手順3: OAuth Refresh Token の発行（顧客ごとに毎回・約3分）

> 「Refresh Token」とは Google アカウントの「あらかじめ許可済みのアクセス権」を
> 後から access token に交換するための鍵。EXE に埋め込むことで、顧客 PC は
> Google ログインなしでアップロードできる。

### 5.1 取得スクリプトの実行

リポジトリのルートで以下を実行:

```bash
python scripts/issue_refresh_token.py ~/keys/client_secret_xxx.json
```

実行内容:
1. ブラウザが自動で立ち上がる
2. Google ログイン画面 → **`pondering1083@gmail.com`** でログイン
3. 「このアプリは Google で確認されていません」警告 → **「詳細」** → **「(unsafe) workscope-collector-cli に移動」**
   - これはテストモードの OAuth Client なので想定通り
4. 「WorkScope Collector に次のアクセスを許可」 → **「許可」**
5. ブラウザに「The authentication flow has completed.」と表示されたら閉じる
6. ターミナルに JSON 出力と base64 確認用文字列が出る

### 5.2 取得した JSON の保管

スクリプトの標準出力をファイルにリダイレクトして保存:

```bash
python scripts/issue_refresh_token.py ~/keys/client_secret_xxx.json \
    > ~/keys/oauth-tribe-001.json
```

中身の例:
```json
{
  "refresh_token": "1//0abc...XYZ",
  "client_id": "xxx.apps.googleusercontent.com",
  "client_secret": "GOCSPX-..."
}
```

保管場所の運用ルール:
- 1Password / Bitwarden 等のパスワードマネージャに添付
- もしくは社内の暗号化ストレージ（BitLocker To Go USB、暗号化共有フォルダ等）
- Git リポジトリには **絶対にコミットしない**（`.gitignore` で `*.json` を除外しているが、念のため確認）
- 顧客への配布は不要（EXE に埋め込まれる）

### 5.3 顧客ごとに新規発行する理由

- 漏洩時の被害局所化（1 顧客の EXE が解析されても他顧客の Refresh Token は無事）
- 顧客契約終了時に当該 Refresh Token のみ Revoke すれば、他顧客の動作に影響しない
- Refresh Token の Revoke は Google アカウント設定 →
  「サードパーティアプリの管理」→ `WorkScope Collector` → アクセス権削除
  （ただし、これをすると **全顧客分** が一斉に止まるので、顧客毎の細かい Revoke はできない点に注意）

### 5.4 Refresh Token の有効期限

- **無期限** が原則だが、以下のケースで無効化される:
  - 6 ヶ月間 access token への交換が一度も行われなかった
  - Google アカウントのパスワードを変更した
  - 2FA の設定変更
  - OAuth 同意画面で「公開」→「テスト」モードに戻した場合
- 顧客配布前に必ず動作確認を実施すること

---

## 6. 手順4: マイドライブの WorkScope フォルダへの権限（自分専用なので不要）

OAuth Refresh Token 方式では、Refresh Token を発行した本人（髙石）のマイドライブを使うため、共有設定は不要。

> 共有ドライブ + SA 方式に移行する際にはこの手順で「サービスアカウントを共有ドライブの編集者として追加」する必要がある。Workspace 移行時に本ドキュメントを更新する。

---

## 7. 手順5: 顧客フォルダ作成（顧客ごとに毎回）

### 7.1 フォルダ作成

1. マイドライブ → `WorkScope` フォルダを開く
2. 右クリック → **「新しいフォルダ」**
3. フォルダ名: **`tribe-{連番3桁}`**
   - 例: `tribe-001`、`tribe-002`、`tribe-003`
4. 作成

> 顧客 ID の命名規則は `tribe-001` 〜 `tribe-999` の3桁固定。SPEC v1.1-lite §12.2 で確定。

### 7.2 フォルダ ID の取得

1. 作成した `tribe-001` フォルダをダブルクリックで開く
2. ブラウザのアドレスバーの URL を見る:
   ```
   https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz12345
                                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                          この部分がフォルダID
   ```
3. `/folders/` の後ろの英数字（25〜33文字）を控える → これが **GDRIVE_FOLDER_ID**

> このIDをビルド時に `--gdrive-folder-id` で渡す。SPEC §7 の運用フロー参照。

---

## 8. 手順6: control.json 初期版を配置（顧客ごとに毎回）

### 8.1 control.json の作成

ローカルの任意の場所（VAIO のデスクトップ等）で `control.json` を新規作成し、以下の内容を書く。

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

注意点:
- `updated_at` は **現在時刻の ISO 8601 形式（UTC）** に書き換える
  - Mac の場合: `date -u +"%Y-%m-%dT%H:%M:%SZ"` で取得可能
  - Windows の場合: PowerShell で `(Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")`
- `updated_by` は実際の運用者メール（自社ドメイン推奨）に書き換える
- 文字コードは **UTF-8（BOMなし）** で保存
- 改行コードは LF / CRLF どちらでもOK

### 8.2 control.json のアップロード

1. `tribe-001` フォルダを開いた状態で、`control.json` をドラッグ＆ドロップ
2. アップロード完了を確認
3. ファイル一覧に `control.json` が表示されることを確認

期待するフォルダ状態:
```
WorkScope/
└── tribe-001/
    └── control.json    ← この時点ではこれ1つだけ
```

> 顧客 PC が稼働開始すると、自動的に日付フォルダと events_*.jsonl.gz が追加されていく。

---

## 9. 手順7: ビルド時に渡す値の確認

VAIO でビルドする前に、以下3点が揃っていることを確認する。

| 項目 | 値の取得元 | 例 |
|---|---|---|
| `GDRIVE_FOLDER_ID` | 手順7.2 で控えた値 | `1AbCdEfGhIjKlMnOpQrStUvWxYz12345` |
| OAuth 資格情報 JSON のパス | 手順5.2 で保管した場所 | `C:\keys\oauth-tribe-001.json` |
| `CUSTOMER_ID` | 手順7.1 で決めた値 | `tribe-001` |

これら3点をビルドコマンドに渡す。詳細は `docs/VAIO_BUILD_GUIDE.md` を参照。

---

## 10. 運用: 顧客の停止／再開

顧客側の収集を一時停止・再開するときは `control.json` を直接編集する。

### 10.1 一時停止

1. `WorkScope/tribe-001/control.json` をダブルクリック → ブラウザで開く
2. 右上「︙」→ **「アプリで開く」** → 「Google ドキュメント」または「テキストエディタ」
3. `"status": "active"` を `"status": "paused"` に変更
4. `"updated_at"` を現在時刻（UTC ISO8601）に更新
5. 上書き保存

> 顧客 PC は最大 `poll_interval_minutes` 分（デフォルト5分）で変更を検知し、トレイアイコンが緑●→黄●に切り替わる。

### 10.2 再開

`"status": "paused"` → `"status": "active"` に戻し、`updated_at` を更新するだけ。

### 10.3 ポーリング間隔の変更

例：1分間隔にしたい場合
```json
"poll_interval_minutes": 1,
```
（最短1分、最長60分）

### 10.4 アップロード間隔の変更

例：30分間隔にしたい場合
```json
"upload_interval_minutes": 30,
```

---

## 11. 運用: アンインストール指示

顧客との契約終了時、顧客 PC からの自動アンインストールを指示する。

### 11.1 アンインストールの実行

1. `control.json` を編集
2. 以下のように `action` を変更:
   ```json
   {
     "status": "active",
     "poll_interval_minutes": 5,
     "upload_interval_minutes": 60,
     "action": "uninstall",
     "updated_at": "（現在のUTC時刻）",
     "updated_by": "operator@tribe.example"
   }
   ```
3. 保存

### 11.2 顧客 PC の動作

- 最大 `poll_interval_minutes` 分（デフォルト5分）以内に検知
- **10分間の取消猶予** あり（トレイに警告表示、顧客が「キャンセル」可能）
- 10分経過後、自動的に以下を削除:
  - レジストリのスタートアップ登録
  - `%APPDATA%\WorkScope\` フォルダ全体
  - EXE 本体（自己削除）

### 11.3 取消したい場合

10分間の猶予中に取消したい場合は、`control.json` で `"action": null` に戻すこと。

### 11.4 完了確認

- 顧客側のアンインストール完了は **アップロードが止まること** で確認
- Drive 側の収集データ（events_*.jsonl.gz）は残るので、必要に応じて手動削除する

---

## 12. 運用: 月次削除バッチ

SPEC v1.1-lite §4.1 のとおり、Google Drive 側のデータは **1年保管** が運用ルール。

### 12.1 運用ルール

- 毎月初（例: 第1営業日）に `created_time < now - 365d` のファイルを削除
- 削除前に顧客 ID ごとの集計サマリ（イベント数・期間）を別シート（Google Sheets）に退避
- 削除対象は `events_*.jsonl.gz` のみ。`control.json` は削除しない

### 12.2 実装

具体的なバッチ実装は別タスク（Drive API + GAS or Python スクリプト）で対応。
本ドキュメントの版数時点では **手動運用**：毎月1日にカレンダー通知を入れ、担当者が手作業で1年前のフォルダを削除する。

---

## 13. トラブルシューティング

### 13.1 「顧客 PC からアップロードされない」

確認順序:
1. **OAuth Refresh Token の有効性確認**
   - 弊社環境で `scripts/issue_refresh_token.py` を再実行 → 同じスコープで再認証できるか
   - 失敗する場合は OAuth Client が破損／削除されている可能性。手順4を再確認
2. **Drive API が有効化されているか**
   - Cloud Console → APIとサービス → 有効なAPI → Google Drive API が一覧にあるか
3. **顧客 PC のネット接続**
   - Wi-Fi / プロキシで Google API がブロックされていないか
4. **顧客 PC のログ確認**
   - `%APPDATA%\WorkScope\logs\collector.log` の最終100行を顧客から取得
   - `401 Unauthorized` / `invalid_grant` → Refresh Token が無効化されている。手順5を再実行して新規発行 → 再ビルド
   - `403 Forbidden` → scope 不足。`drive.file` で発行できているか確認
   - `404 Not Found` → フォルダ ID が間違っている可能性。手順7.2を再確認
   - `Connection timeout` → ネット環境問題

### 13.2 「control.json の変更が反映されない」

確認順序:
1. **ファイル名の typo**
   - `control.json` が正しいか（`controll.json` / `Control.json` 等はNG、大文字小文字も厳密）
2. **JSON 構文エラー**
   - https://jsonlint.com 等でバリデート
   - カンマ抜け、ダブルクォート忘れ、末尾カンマが多い
3. **`updated_at` の未来日付**
   - SPEC §5.4 のフェイルセーフで、現在時刻より未来 or 30日以上古い場合は **直前状態維持** されて変更が無視される
   - PC の時計と UTC のズレを確認
4. **顧客 PC が稼働していない**
   - そもそも顧客 PC が起動していない／オフラインでは反映されない
5. **ポーリング間隔より早く確認している**
   - `poll_interval_minutes: 5` なら最大5分待つ

### 13.3 「Google Drive の容量が逼迫」

確認順序:
1. **アカウントのストレージプラン確認**
   - Google One Basic: 100GB
   - Google One Premium: 2TB
   - Google One AI Premium: 2TB
   - Google One 5TB: 5TB（髙石: 5TB プラン）
2. **古いファイル削除**
   - 1年経過前でも、契約終了済顧客のデータは前倒し削除
3. **顧客フォルダ別の容量確認**
   - Drive 上の `WorkScope/` 配下を右クリック → 詳細で容量内訳を見る
   - 異常に多い顧客がいないかチェック（不具合で大量アップロードしている可能性）

### 13.4 「OAuth 資格情報 (Refresh Token) を紛失した」

1. `scripts/issue_refresh_token.py` を再実行（手順5を再実行）
2. **過去発行分は自然失効** を待つか、Google アカウント設定の
   「サードパーティアプリの管理」→ `WorkScope Collector` を削除して全 Refresh Token を一括 Revoke
3. 新しい JSON で EXE を再ビルドして顧客に再配布
   - 旧 EXE は動かなくなるので、顧客に置き換え案内が必要

> 注意: Refresh Token を全 Revoke すると **全顧客分** が止まる。顧客毎の細かい Revoke は OAuth 方式では不可能。Workspace 移行後の SA 方式では顧客毎に細かく Revoke できる。

### 13.5 「Refresh Token が `invalid_grant` で無効化された」

以下のケースで Google が自動的に Refresh Token を無効化する:
- 6 ヶ月間 access token への交換が一度もない（顧客 PC が長期間オフライン）
- 認証元アカウントのパスワード変更／2FA 設定変更
- OAuth 同意画面で「公開」→「テスト」モード変更
- ユーザーが「サードパーティアプリ」から手動 Revoke

対応:
1. 手順5を再実行して新しい Refresh Token を発行
2. 新しい資格情報で全顧客分の EXE を再ビルド
3. 顧客に新 EXE を再配布

> 予防: 顧客 PC は最低でも月1回はネット接続することを契約書に明記、または弊社側で月次の動作確認を行う。

---

## 14. 関連ドキュメント

- 仕様書: `docs/SPEC_v1.1-lite.md`（§4 Google Drive 直送、§5 リモート制御）
- VAIO ビルド手順: `docs/VAIO_BUILD_GUIDE.md`
- インシデント対応: `docs/INCIDENT_RUNBOOK.md`
- データ取扱ポリシー: `docs/data_handling_policy.html`
