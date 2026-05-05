# WorkScope Collector インシデント対応 RUNBOOK

**対象**: WorkScope Collector v1.0 を顧客に導入中の運用担当者（株式会社TRIBE 内部）
**最終更新**: 2026-05-06

---

## 想定インシデント分類

| 重大度 | 内容 | 初動目標時間 |
|---|---|---|
| **P1: 漏洩疑い** | マスキング失敗で顧客個人情報が外部送信 / クラウド保管領域で意図せず保管 | 検知から **1時間以内** に停止 |
| **P2: 動作不能** | 顧客環境でEXEが起動しない、データ収集が止まっている | 検知から **当日中** に対応開始 |
| **P3: 解析不整合** | 業務マップ生成失敗、JSONLが破損 | 検知から **3営業日以内** |
| **P4: 機能要望** | 顧客から追加機能・修正の要望 | 検討後返答 |

---

## P1: 個人情報漏洩疑い (最優先)

### 初動 (1時間以内)

#### 1. 全顧客の収集停止
**最優先**: 同様の漏洩経路が他顧客にも存在する可能性があるため、まず止める。

```bash
# Dashboard で全顧客を suspend に
psql "$DATABASE_URL" <<SQL
UPDATE workscope_customers SET status = 'paused', notes =
  COALESCE(notes, '') || E'\n[INCIDENT ' || now() || '] paused due to suspected leak';
SQL
```

これだけで顧客側EXEからのアップロードAPIが401を返すようになる（receiver側ガード）。
顧客PCの常駐EXEは即時停止しないが、それ以上のデータ送信は防げる。

#### 2. 漏洩規模の切分け
**何が、どこで、いつから** を10分以内に確定:

```bash
# 受信bucket内の全アップロードを検査 (Supabase Storage)
# 過去24時間以内に届いたファイルを全件ダウンロードしてPIIスキャン
cd ~/Claude案件/株式会社TRIBE/業務フロー構築AI/workscope-collector
python3 -m scripts.scan_received_uploads --hours 24
```

(scripts/scan_received_uploads.py は別途実装、または今は手動で workscope_uploads テーブルから 過去24時間分の storage_path を取得 → uploader.scan_jsonl_for_pii_leakage で検査)

#### 3. 影響顧客への一次連絡
**漏洩確定後、4時間以内**:
- メール: support@tribe-saas.com（仮）から顧客の管理者宛
- 件名: 「【重要】WorkScope Collector における個人情報の取扱いについて (一次報告)」
- 本文テンプレート (下記参照) を顧客名・状況に合わせて調整

#### 4. 証跡保全
- 該当時間帯の Supabase Storage バケット全ファイルを別バケット（`workscope-incident-archive`）にコピー保存
- ダッシュボードDB: workscope_uploads / workscope_customers のスナップショットを取る
- 顧客側EXEのログ収集を依頼（`%APPDATA%\WorkScope\logs\` を圧縮して送付してもらう）

---

### 初動以降 (24-72時間)

#### 5. 原因究明
- マスキング失敗パターンを特定:
  - OCR の誤認識 (低解像度・特殊フォント)
  - 業界プロファイルのルール漏れ
  - window_titles.py の Phase 3 ホワイトリストすり抜け
  - UIA の name/parent_path のマスク失敗
  - uploader.scan_pending_for_leakage の検出漏れ
- gitleaks やテストコードに新しい検出パターンを追加し、PR で修正
- Codex セカンドレビューを再実施

#### 6. 個情委への報告判断
- 個人情報保護法 第26条「報告義務」の該当性を判断
- 該当時: 速報 (3-5日以内) + 確報 (30日以内) を提出
- 個情委フォーマット: https://www.ppc.go.jp/personalinfo/legal/leakAction/

#### 7. 本人通知
- 漏洩した本人（薬局の患者）への通知が必要かを評価
- 通常は薬局経由で患者に通知（薬局が個人情報取扱事業者であるため）

#### 8. 公表判断
- 影響規模・社会的影響に応じて公表
- 公表時はステータスページとプレスリリースで対応

#### 9. 復旧
- 修正後の v1.0.X をリリース
- 全顧客に再ビルド配布
- workscope_customers.status を 'active' に戻す
- 影響顧客に確報（再開のお知らせ + 再発防止策）を送付

---

### 一次連絡メールテンプレート

```
件名: 【重要】WorkScope Collector における個人情報の取扱いについて (一次報告)

[顧客担当者] 様

平素より弊社サービスをご利用いただき誠にありがとうございます。
株式会社TRIBE [担当者名] です。

[YYYY-MM-DD HH:MM] 頃、WorkScope Collector の運用において、
以下の事象を確認いたしました。

■ 事象
[簡潔に状況を記述。例: マスキング処理を経ずに顧客様のデータが
弊社の解析サーバへ送信されていた可能性がございます]

■ 影響範囲
- 対象顧客: [顧客名]
- 対象期間: [YYYY-MM-DD] 〜 [YYYY-MM-DD]
- 影響を受けた可能性のある情報: [氏名・保険番号・電話番号 等の項目]

■ 現時点の対応
- 全顧客のデータ収集を停止いたしました（[YYYY-MM-DD HH:MM]）。
- 影響範囲の調査を進めております。
- 弊社サーバに保管していたデータは隔離保全いたしました。

■ 今後の対応予定
- 24-48時間以内に詳細調査結果を再度ご連絡いたします。
- 必要に応じて個人情報保護委員会への報告および本人通知を実施します。
- 修正版のリリース時期は調査完了後に改めてご案内いたします。

ご心配をおかけしておりますことを深くお詫び申し上げます。
ご質問・追加情報のご要望等ございましたら、本メールへの返信または
support@tribe-saas.com までご連絡くださいませ。

株式会社TRIBE
[担当者名]
[電話番号]
```

---

## P2: 動作不能 (顧客環境でEXEが起動しない)

### 切り分け手順

1. **smoke_test.exe を実行してもらう**
   - SmokeTest結果ページ（ブラウザ表示）のスクリーンショットを送ってもらう
   - 失敗項目の名前を確認: `python_imports / windows_bindings / profile_loader / masker_works / appdata_writable / consent_form_present / uploader_config`

2. **失敗項目別の対処**

| 失敗項目 | 対処 |
|---|---|
| python_imports | EXE破損 → 再ダウンロード or 再ビルド配布 |
| windows_bindings | OS古い (Win10未満) → 顧客にOSアップグレード or 旧PC交換を案内 |
| profile_loader | 業界プロファイル同梱漏れ → ビルド設定確認、再ビルド |
| masker_works | マスカーバグ → 緊急修正 + 全顧客再配布 |
| appdata_writable | 権限不足 → 管理者権限実行 or グループポリシー調整を顧客IT管理者に依頼 |
| consent_form_present | docs/ 同梱漏れ → ビルド設定 (.spec) 確認、再ビルド |

3. **ログ収集依頼**
   - `%APPDATA%\WorkScope\logs\main.log`
   - `%APPDATA%\WorkScope\logs\crash.log` (存在すれば)
   - 顧客にzipして送付してもらう

4. **再発防止**
   - 切分け結果を release_check.py の項目に追加
   - GitHub Actions の build-customer.yml の smoke test stepで検出可能か確認

---

## P3: 解析不整合 (業務マップ生成失敗)

### 典型ケース

#### a. JSONLが破損している
```bash
# 破損行を検出
python3 -c "
from analyzer.detector import load_events
from pathlib import Path
loaded = list(load_events(Path('events/')))
print(f'loaded {len(loaded)} events')
"
```
- `analyzer/detector.py` の `load_events()` は破損行を warning でスキップする (Codex Medium 指摘)
- 破損数が大量の場合は受信側で何らかの異常 → 該当アップロードを isolate

#### b. パターン検出されない
- min_occurrences を下げる (`--min-occurrences 1`)
- 観測期間が短い → データ収集を継続してもらう

#### c. RPAスクリプトが生成されない
- 候補がゼロ → 業務反復が観測されていない
- 顧客の業務シーンが多様すぎる → 業務マップだけ納品して、人力で自動化候補を絞る

---

## P4: 機能要望

### 受け方
- support@tribe-saas.com で受信 → Notion/Issue Tracker に起票
- 優先度判定:
  - 既存契約に影響 → P3 相当で扱う
  - 新規機能 → 別途見積

### 業界プロファイル追加要望
- profiles/ に新規JSONを追加 → tests/test_profile_loader.py の parametrize で自動検証
- PR + Codex レビューを通してリリース

---

## 連絡先 (社内)

| 役割 | 担当 | 連絡先 |
|---|---|---|
| インシデント指揮者 | [代表名] | [電話番号] / [メール] |
| 技術担当 | [担当者名] | [電話番号] / [メール] |
| 法務担当 | [外部弁護士事務所名] | [電話番号] / [メール] |
| 広報担当 | (該当時のみ) | - |

## ステータスページ (公開予定)

`https://status.tribe-saas.com` (構築予定)
- P1/P2 発生時はここで状況を更新
- 顧客は RSS / メール購読で通知を受ける

---

## 演習 (推奨)

四半期ごとに机上演習を実施:
1. P1 シナリオ「マスカーが10秒間 false negative を起こした」
2. 上記初動手順をストップウォッチで計測
3. 30分以内に「全顧客停止 + 影響規模の暫定切分け」が完了することを確認

実機演習は本番と区別がつかなくなるリスクがあるため、ステージング環境で別途実施。
