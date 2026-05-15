# 村上さん 顧客プロファイル

| 項目 | 値 |
|---|---|
| slug | `murakami` |
| 正式名 | **村上さん**（呼称） |
| 業界 | **中古車業界** |
| 業務内容 | **情報入力業務の自動化**（観察対象） |
| 業界プロファイル | **なし**（generic 相当の共通PIIマスクのみで運用） |
| 同意書 | **本リポジトリでは管理しない**（必要時のみ別途作成） |
| 担当 | takaishouhei |
| 状態 | **ビルド前**（業務・業界確定、配布EXE未生成） |

---

## 物理環境（パイロット端末）

- **ノートPC内蔵 + 外部モニター1枚（合計2画面）**
- 本顧客向けには **v1.0.1（マルチモニター対応版）以降のビルドを使う**こと
  - 大森薬局向けの初期投入で「フォーカス側1モニターしか撮れない」事象が確認され、
    `capture_all_monitors()` で全物理モニターを個別キャプチャする方式に変更済
  - JSONLでは `event["screenshot"]` にフォーカス側、`event["additional_screenshots"][]`
    に他モニターを格納
  - スクショファイル名は `_mon{N}` サフィックス（N は mss の 1始まり物理モニター番号）

参考: `src/collector.py::capture_all_monitors`, `tests/test_multi_monitor_capture.py`

---

## マスキング方針

業界固有プロファイルは適用しない。`profiles/base.json`（共通PII: 氏名／電話／メール／
住所／郵便／マイナンバー／クレカ）のみを通す `generic` 相当で運用する。

中古車情報入力の観察文脈で守るべき項目はおおむね共通PIIで賄える想定:
- 顧客氏名／連絡先（電話・メール・住所）
- 車検証上の所有者・使用者の氏名・住所

これ以外に固有マスク（自動車登録番号や車台番号を機械的に黒塗りする等）が必要に
なった時点で、`profiles/automotive_used.json` を新設する判断に切り替える。
（現状は不要、業務観察で漏れが見つかったら追加）

---

## ビルド設定

```bash
# プロジェクトルートから（業界プロファイルは generic を指定）
bash scripts/build_for_customer.sh \
  --customer "村上さん" \
  --industry "generic" \
  --endpoint "<クラウドアップロード先 or 'usb'（USB回収モード）>" \
  --api-key "<dashboard で発行した APIキー>"

# 成果物: dist/WorkScope_村上さん_<YYYYMMDD>.exe
```

AppVeyor CI でビルドする場合は環境変数で:
- `CUSTOMER_NAME=村上さん`
- `INDUSTRY=generic`
- `ENDPOINT=<確定後>`
- `API_KEY=<確定後>`

---

## 同意書

本リポジトリでは管理しない（現時点で運用上不要との判断）。
業務拡大や法務要件で必要になった時点で `docs/consent_form.html` をベースに作成する。

---

## 運用状況

- (TODO) ビルド完了後追記
- (TODO) パイロット投入日・端末情報・OS バージョン
- (TODO) 既知の障害／対応履歴

---

## 関連情報

- 全体スペック: `docs/SPEC_v1.0.md`
- インストールチェックリスト: `docs/PILOT_INSTALL_CHECKLIST.md`
- データ取扱い方針: `docs/data_handling_policy.html`
- マルチモニター対応のテスト: `tests/test_multi_monitor_capture.py`
