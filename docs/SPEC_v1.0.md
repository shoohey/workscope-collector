# WorkScope Collector v1.0 仕様書

**位置づけ**: 株式会社TRIBE「業務まるごと可視化AI」サービスのクライアント側ツール。
**v0.1.0からの差分**: 薬局特化 → 全業界対応の汎用業務観察プラットフォーム。

---

## 1. 設計原則

| # | 原則 | 理由 |
|---|---|---|
| 1 | 顧客別カスタムEXEを私たち側で事前ビルド | 顧客に判断させない＝現場で確実に使われる |
| 2 | 顧客側UXは「ダブルクリック→同意ボタン→開始」のみ | 業界選択・送信先入力・会社名入力すべて撤去 |
| 3 | 業界プロファイル(`profiles/*.json`)でマスキング切替 | extends継承でルール再利用、業界追加時はJSON1ファイル |
| 4 | アプリ自動分類でSaaS/ERP/業界アプリ判定 | 業務分析・RPA出口振り分けの基準 |
| 5 | RPA出口4種を業務種別で自動振り分け | pywinauto/PAD/Selenium/Computer Use |
| 6 | 既存v0.1.0テスト31本を壊さない移行 | パイロット店舗(薬局)を無停止アップデート可能に |

---

## 2. 業界プロファイル機構

### 2.1 ファイル構成

```
profiles/
├── base.json         # 共通PII (氏名/電話/メール/住所/郵便/マイナンバー/クレカ)
├── pharmacy.json     # 薬局 (患者ID/保険者番号/薬剤名ホワイトリスト)
├── accounting.json   # 会計事務所 (取引先名/口座番号)
├── legal.json        # 法律事務所 (契約相手/案件番号/判例番号)
├── sales.json        # 営業 (顧客名/商談額/SFAフィールド)
├── hr.json           # HR (従業員ID/給与額/評価)
└── generic.json      # 普通の会社 (baseのみ)
```

### 2.2 JSONスキーマ

```json
{
  "name": "pharmacy",
  "extends": "base",
  "version": "1.0",
  "description": "薬局・調剤薬局向けマスキングプロファイル",
  "rules": [
    {
      "name": "patient_id_digits",
      "pattern": "(?<!\\d)\\d{4,}(?!\\d)",
      "category": "patient_id",
      "context_keywords": ["患者ID", "カルテNo", "受付番号"],
      "priority": 100
    }
  ],
  "whitelist": {
    "drug_names": ["アムロジピン", "ロキソニン"]
  }
}
```

**フィールド意味**:
- `extends`: 親プロファイル名（再帰的に継承解決）
- `rules[].pattern`: 正規表現（JSON文字列、Python `re` 互換）
- `rules[].category`: マスクラベル種別（`[MASKED:<category>]` で表示）
- `rules[].context_keywords`: 近傍boxにあれば発火する文脈キーワード（薬の用量など誤マスク防止）
- `rules[].priority`: 評価順序（小さい順、base=0、業界固有=100）
- `whitelist.<key>`: マスク除外する語リスト（業界別に拡張可能）

### 2.3 継承解決アルゴリズム

```
load("pharmacy")
  ↓ extends="base"
1. profiles/base.json を読み込み rules = base.rules
2. profiles/pharmacy.json を読み込み rules += pharmacy.rules
3. priority昇順でソート
4. whitelist は dict マージ
5. ProfileオブジェクトとしてMaskRuleリストを返す
```

### 2.4 既存薬局ルールの移植

`src/masker.py` の `DEFAULT_RULES`（13ルール）を以下に振り分け:

| ルール | 移行先 | 理由 |
|---|---|---|
| email | base.json | 全業界共通 |
| my_number | base.json | 全業界共通 |
| insurance_card_no | pharmacy.json | 薬局固有 |
| name_kanji_honorific / name_kana_honorific / name_kana_long | base.json | 全業界共通 |
| insurance_8digits | pharmacy.json | 薬局固有 |
| patient_id_digits | pharmacy.json | 薬局固有 |
| date_wareki / date_kanji / date_slash | base.json | 全業界共通 |
| phone | base.json | 全業界共通 |
| postal | base.json | 全業界共通 |
| address | base.json | 全業界共通 |

`COMMON_DRUG_NAMES` → `pharmacy.json` の `whitelist.drug_names`

---

## 3. 顧客側UX（C案: ワンクリック）

```
[1回目]
顧客: DLリンクから WorkScope_<customer>_<date>.exe 取得
  ↓ ダブルクリック
[同意書HTML 1枚表示] (docs/consent_form.html)
  ↓ [同意して開始] ボタン
裏で自動実行:
  - スタートアップ登録 (HKCU\Run)
  - %APPDATA%\WorkScope\consent_signed.json 生成 (同意日時+顧客名)
  - トレイ常駐開始 (緑●)

[2回目以降]
ダブルクリック → 同意画面スキップ → 即トレイ常駐開始
```

**業界選択UI・送信先入力UI・会社名入力UIは存在しない**（ビルド時埋め込み済み）。

---

## 4. ビルド運用（私たち側）

### 4.1 顧客別ビルドコマンド

```bash
scripts/build_for_customer.sh \
  --customer "村上薬局" \
  --industry pharmacy \
  --endpoint "https://upload.tribe-saas.com/customers/murakami/" \
  --output "dist/WorkScope_村上薬局_20260505.exe"
```

### 4.2 ビルド時の埋め込み内容

| 内容 | 埋め込み方法 |
|---|---|
| 業界プロファイル | `profiles/<industry>.json` のみ同梱（他は削除） |
| 顧客名 | ビルド時に `config.py` の定数として埋め込み |
| 送信先URL | 同上 |
| 同意書(顧客名入り) | `docs/consent_form.html` のテンプレート置換 |

### 4.3 GitHub Actions実行

```yaml
# .github/workflows/build-customer.yml
on:
  workflow_dispatch:
    inputs:
      customer: { type: string, required: true }
      industry: { type: choice, options: [pharmacy, accounting, legal, sales, hr, generic] }
      endpoint: { type: string }
runs-on: windows-latest
```

---

## 5. アプリ自動分類

### 5.1 カテゴリ定義

| カテゴリ | 例 | RPA出口 |
|---|---|---|
| SaaS-Web | kintone/freee/Salesforce/Notion | Selenium/Playwright |
| SaaS-Desktop | Teams/Zoom/Slack(電子) | Power Automate Desktop |
| ERP | SAP/OBIC7/弥生販売 | pywinauto |
| CRM | Salesforce(電子)/HubSpot(電子) | PAD or Selenium |
| 業界アプリ-医療 | レセコン8メーカー/電子カルテ | pywinauto |
| 業界アプリ-会計 | freee/MFクラウド/弥生 | PAD |
| Office | Excel/Word/PowerPoint/Outlook | PAD |
| Browser | Chrome/Edge/Firefox | Selenium |
| 開発 | VSCode/IntelliJ/Terminal | (RPA対象外) |
| その他 | - | Computer Use |

### 5.2 判定ロジック

`src/app_classifier.py`:
```python
def classify(process_name: str, process_path: str, window_title: str) -> AppCategory:
    # 1. プロセス名完全一致 (kintone-desktop.exe → SaaS-Desktop)
    # 2. プロセスパスのbasename部分一致 (\Microsoft\Office\ → Office)
    # 3. window_titleのドメイン抽出 (Chrome + "kintone.cybozu.com" → SaaS-Web)
    # 4. デフォルト: その他
```

ルールDBは `src/app_rules.json` で外部化（コミュニティ拡張可能）。

---

## 6. 入力イベント収集（v0.2機能）

### 6.1 イベント種別 (schema_version=2)

| event_type | 取得タイミング | 主要フィールド |
|---|---|---|
| `window_focus` | フォアグラウンド変化 | app, window, dwell_ms_prev, screenshot |
| `uia_focus` | コントロールフォーカス変化 | focused_control(automation_id, name, control_type, parent_path) |
| `key_typed` | キーボード入力 | key_name(Tab/Enter/Fkey等) or text_keys_count(N桁) |
| `mouse_click` | クリック | coords, target_text(直近OCRボックスから推定) |
| `value_changed` | フォーム値変更 | field_name, value_masked(マスカー通過後) |

### 6.2 PII保護ポリシー（v1.0で固定）

- 文字キーの**値は記録せず桁数のみ**（例: `text_keys_count: 8`）
- IsPassword=True のフィールド入力中は**キーロギング自体を停止**（トレイ赤）
- UI Automation で取得した `Value` は**必ずマスカー通過後**に記録
- マスカー失敗時は記録ごとスキップ

---

## 7. 解析（業界横断）

### 7.1 反復パターン検出

`analyzer/detector.py`:
1. JSONLから `(window_focus → uia_focus → key_typed*) → window_focus` のシーケンス抽出
2. 同一アプリ内の連続操作を業務単位として N-gram 化
3. 階層クラスタリングで反復パターン抽出
4. 頻度・分散・所要時間で自動化候補スコア算出

### 7.2 業務マップHTMLレポート

`analyzer/report_generator.py` → 顧客納品物:
- 業務一覧表 (業務名/頻度/月間時間/担当者数/自動化候補度)
- 業務フロー図 (Mermaid.js)
- アプリ別時間配分 (円グラフ)
- RPA化提案リスト (上位10業務+推定削減時間+ROI)
- 業界ベンチマーク (同業他社平均との比較)

トンマナ: 濃紺×白、A4横印刷対応（グローバルCLAUDE.md準拠）。

---

## 8. RPA/エージェント出口の自動生成

業務シーケンスから自動的にスクリプト/エージェント定義を生成。

### 8.1 振り分けロジック

| 業務のアプリ種別 | 出口 | 生成物 |
|---|---|---|
| 業界アプリ/ERP (Win32) | pywinauto | `<task>.py` (Pythonスクリプト) |
| Office/SaaS-Desktop | Power Automate Desktop | `<task>.padfile` (YAML) |
| SaaS-Web/Browser | Selenium/Playwright | `<task>.spec.ts` |
| 非定型判断業務 | Claude Computer Use | `<task>.agent.json` (prompt+tools) |

### 8.2 生成テンプレート

`analyzer/templates/` 配下にjinja2テンプレート:
- `pywinauto.py.j2`
- `pad.padfile.j2`
- `playwright.spec.ts.j2`
- `computer_use.agent.json.j2`

各生成物はドライラン検証付きで出力。

---

## 9. 互換性戦略（既存v0.1.0との後方互換）

### 9.1 既存テスト31本を壊さない方法

| 既存API | v1.0での扱い |
|---|---|
| `from masker import DEFAULT_RULES` | `pharmacy` プロファイルのルールを返すエイリアス（後方互換） |
| `from masker import CAT_PATIENT_NAME, CAT_INSURANCE_ID, ...` | 定数として維持 |
| `mask_image(img, boxes, strict=True)` | シグネチャ変更なし、内部でデフォルトプロファイル使用 |
| `mask_window_title(title)` | シグネチャ変更なし |

### 9.2 新規API

```python
from profile_loader import load_profile
from masker import mask_image_with_profile

profile = load_profile("accounting")
result = mask_image_with_profile(img, boxes, profile=profile, strict=True)
```

### 9.3 デフォルトプロファイル決定順

```
1. 環境変数 WORKSCOPE_PROFILE が設定されていればそれ
2. config.json の industry_profile フィールド
3. ビルド時埋め込み定数 (config.py の DEFAULT_PROFILE)
4. フォールバック: "pharmacy" (v0.1.0互換)
```

---

## 10. テスト戦略

### 10.1 必須テスト

| ファイル | カバレッジ |
|---|---|
| `tests/test_masker.py` (既存31本) | 既存挙動維持 |
| `tests/test_pii_safety.py` (新規3本) | masker未導入/OCR空/mask例外で生画像が残らない |
| `tests/test_profile_loader.py` (新規) | プロファイルJSON読込/extends継承/whitelist マージ |
| `tests/test_profiles_masking.py` (新規) | 業界別 property-based テスト (hypothesis) |
| `tests/e2e/test_pharmacy_workflow.py` (Phase 1) | tkinterモックレセコンE2E |
| `tests/integration/test_app_coverage.py` (Phase 1) | 主要20アプリでのカバレッジ |

### 10.2 CIゲート

`scripts/release_check.py`:
1. 全テストグリーン
2. PII漏洩テスト3本パス
3. 全プロファイルJSONが正規表現として有効
4. PyInstaller buildに成功
5. smoke_test.exe で起動→1イベント生成→終了

→ Windows clean VM (GitHub Actions windows-latest) で毎回実行、1項目でも失敗ならrelease block。

---

## 11. 実装フェーズ

| Phase | 内容 | 所要 |
|---|---|---|
| **Phase 0**(着手中) | 仕様書/PII漏洩テスト/プロファイル機構/プロファイル7種/業界別マスキングテスト | 5日 |
| Phase 1 | UI Automation/キーロガー/click resolver/イベントスキーマv2/E2E | 7日 |
| Phase 2 | ワンクリックEXE化+顧客別ビルドスクリプト+CI gate | 5日 |
| Phase 3 | 汎用解析+業務マップHTML | 5日 |
| Phase 4 | RPA出口4種自動生成 | 8日 |

**Phase 0 完了基準**: 既存テスト31本すべてグリーン + 新規PII漏洩テスト3本グリーン + 全7プロファイル読み込み成功 + 薬局案件は無停止アップデート可能。

---

## 12. 関連プロジェクト

- **TRIBE「業務まるごと可視化AI」**: 本ツールの本体サービス。¥30万契約済（2026-04）
- **村上さん薬局案件**: pharmacyプロファイルの第1号顧客。28店舗展開予定
- **saas-boilerplate**: 管理ダッシュボード(Phase外、5社超で着手)の基盤として流用予定
