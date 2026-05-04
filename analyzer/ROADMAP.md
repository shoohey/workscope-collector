# Phase 2-3 解析・自動化ロードマップ

WorkScope Collector が収集した JSONL イベント + マスク済みPNG から、
業務マップ生成 → 自動化候補スコアリング → RPA/AIエージェント雛形生成 までを行う設計骨子。

このディレクトリの実装は **収集フェーズ完了後（2週間後）** に着手する。今は設計のみ。

---

## Phase 2: 解析（収集データ蓄積後・1週間）

### 2-1. 業務セッション分割（`session_segmenter.py`）
- 入力: 日次JSONLファイル群（`%APPDATA%/WorkScope/data/events/*.jsonl`）
- 処理:
  - 連続するイベントを「業務セッション」に分割（基準: 30分以上のアイドル / アプリ群の切替）
  - 各セッションに `session_label` を一旦 None で付与
- 出力: `analysis/sessions.parquet`

### 2-2. アプリ滞在時間集計（`app_usage.py`）
- アプリ別 / ウィンドウタイトル種別ごとの累計滞在時間
- 時間帯別ヒートマップ（朝・昼・夕の業務分布）
- 出力: `analysis/app_usage_report.html`（D3.jsかChart.jsで可視化）

### 2-3. 業務クラスタリング（`task_clustering.py`）
- 各イベントの `screenshot.ocr_text_summary` + `app.process_name` + `window.title` を埋め込みベクトル化
  - **ローカルLLM**: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（オフライン、日本語対応）
  - クラウド送信NG（マスク済みでも念の為ローカル処理）
- HDBSCANでクラスタリング → 業務カテゴリ自動抽出
  - 例: 「処方入力」「調剤監査」「会計」「在庫発注」「レセプト点検」
- 各クラスタの代表イベントをLLM（ローカル Ollama or クラウド）で命名
- 出力: `analysis/task_clusters.json` + `task_clusters_report.html`

### 2-4. 業務遷移マップ（`workflow_graph.py`）
- セッション内のアプリ/ウィンドウ遷移をグラフ化
- ノード: 業務カテゴリ、エッジ: 遷移頻度＋平均所要時間
- 反復パターン検出（Aho-Corasick的に頻出シーケンス抽出）
- 出力: `analysis/workflow_graph.html`（Mermaid or vis.jsで可視化）

### 2-5. 自動化候補スコアリング（`automation_candidates.py`）
スコア式:
```
score = 頻度係数 × 反復度 × 1人当たり所要時間 × (1 - 個別判断必要度)
       = (週あたり実行回数) × (操作シーケンスの定型度) × (1回あたり分) × (定型業務度)
```

- 各業務クラスタについて以下を算出:
  - **頻度係数**: 週あたり実行回数
  - **反復度**: 同一シーケンスの出現確率（高いほど自動化しやすい）
  - **所要時間**: 1回あたりの平均分
  - **定型業務度**: 個別判断（OCR画面に「特例」「相談」等のキーワードが少ない）の割合
  - **データ入出力源泉**: どのアプリから何のデータを取り、どこに入力しているか
- 出力: `analysis/automation_candidates.json`（スコア降順）+ `report.html`

### 2-6. 経営者向け業務棚卸しレポート（`management_report.py`）
- 入力: 上記2-1〜2-5の出力
- 1ファイルHTML、A4横スライド形式（CLAUDE.mdトンマナ準拠）
- セクション:
  1. サマリ: 総業務時間 / 業務カテゴリ数 / 自動化候補TOP10
  2. 業務カテゴリ別 時間配分円グラフ
  3. 時間帯別ヒートマップ
  4. 業務遷移マップ
  5. 自動化候補TOP10（スコア・推定削減時間・難易度）
  6. 推奨アクション（フェーズ3で実装する業務トップ3）
- クライアント向け説明会資料

---

## Phase 3: RPA/AIエージェント雛形生成（2週間）

自動化候補TOP10から、クライアントと相談して **着手する3〜5業務** を確定。
各業務に対し以下を選択・生成:

### 3-1. 雛形タイプ判定（`automation_type_router.py`）
判定ロジック:
| 業務特性 | 推奨ツール |
|---|---|
| 同じ画面・同じ座標で繰り返し操作 | **Power Automate Desktop** フロー |
| ウィンドウ要素操作（GUI構造あり） | **pywinauto** Pythonスクリプト |
| OCR + 判断 + 入力（柔軟性必要） | **AIエージェント**（Claude/GPT-4V + pywinauto） |
| Web操作中心 | **Playwright** スクリプト |
| ファイル・データ変換中心 | **Python + pandas** スクリプト |

### 3-2. RPA雛形生成（`rpa_generator.py`）
- 入力: 業務クラスタ（収集データから抽出した代表シーケンス）
- 処理:
  1. 操作シーケンスをステップに分解（ウィンドウ起動→クリック→入力→次画面→…）
  2. 各ステップでLLM（クラウドClaudeを使用、マスク済みデータのみ送信）にコード生成依頼
  3. **Power Automate Desktop**: `.ps1` + `.txt` でフロー定義（インポート用）
  4. **pywinauto**: 単一Pythonファイル + READMEで実行手順
  5. **AIエージェント**: Claude API + pywinauto を組み合わせた `agent.py`
- 出力: `automations/<業務名>/`
  - `flow.ps1` or `script.py`
  - `README.md`（依存・実行手順）
  - `test_data.json`（テスト入力例、マスク済み）
  - `error_handling.md`（想定エラーと対処）

### 3-3. AIエージェントテンプレート（`agent_template.py`）
判断必要な業務向けの汎用テンプレ:
```python
# 1. 起動: 対象アプリを開く
# 2. ループ:
#    a. スクショ取得
#    b. Claude/GPT-4V に「次に何をすべきか」を問う（マスク済みデータと業務指示書を渡す）
#    c. アクション実行（pywinautoで具体操作）
#    d. 結果検証
# 3. 完了条件で終了
```
- 業務指示書は markdown で人間が記述
- LLMは **Claude API** を使用（CLAUDE.mdトンマナに従い、デュアル設計でOpenAIフォールバック）

### 3-4. 検証・展開
- 自動生成した RPA をクライアントと一緒に1業務ずつ検証
- 1週間試験運用 → 不具合修正 → 本番運用
- 展開後の効果測定（同じ WorkScope Collector で再収集 → 削減時間を可視化）

---

## 後日決定事項

- 解析結果のクラウド送信ポリシー（マスク済みなら送信OKか、完全ローカルか）
- RPA実行環境（レセコン上で直接 or 別PC経由）
- 効果測定の頻度
- 追加業務の自動化サイクル

---

## ディレクトリ構成（Phase 2 着手時）
```
analyzer/
├── ROADMAP.md                       # この文書
├── requirements-analysis.txt        # 解析専用依存（sentence-transformers, hdbscan, plotly等）
├── data_loader.py                   # JSONL読込・正規化
├── session_segmenter.py             # 2-1
├── app_usage.py                     # 2-2
├── task_clustering.py               # 2-3
├── workflow_graph.py                # 2-4
├── automation_candidates.py         # 2-5
├── management_report.py             # 2-6
├── reports/                         # 出力先
└── automations/                     # Phase 3 生成物
```
