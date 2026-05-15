# customers/ — 顧客別配布物・運用メモ

WorkScope Collector の顧客別配布物（同意書／ビルド設定／実機状態メモ）をまとめる場所。
TRIBE「業務まるごと可視化AI」案件で顧客数が増えても、混線せず追跡できることを目的とする。

---

## なぜ分けるか

- 顧客ごとにビルド引数（`customer_name` / `industry` / `endpoint` / `api_key`）が違うため、
  どの設定でEXEをビルドしたかを履歴として残したい
- 同意書は顧客名・対象業務・マスキング対象が会社ごとに変わる
- パイロット投入後の実機構成（モニター枚数／OS／レセコン製品など）と既知の障害を
  会社単位で記録したい
- `docs/` 配下に `consent_form_<会社名>.html` を直置きしていく従来パターンは、
  顧客が増えるとファイルが散らかるので、ディレクトリ単位でまとめる

---

## レイアウト

```
customers/
├── README.md             ← このファイル
└── <customer_slug>/
    ├── README.md         ← 会社情報・業界・ビルド設定・状態管理（必須）
    ├── consent_form.html ← 顧客名差し込み済み同意書（業界プロファイル確定後）
    └── notes.md          ← 実機固有の備忘（任意）
```

- `customer_slug` は **ASCII safe**（小文字英数 + ハイフン）にする
  - 理由: AppVeyor / PyInstaller spec / Windows BAT 経由でビルドする際、
    UTF-8 マルチバイトパスで過去に文字化け事故あり（コミット `3b1a149`, `9b76943` 参照）
  - 日本語の正式名は README 内に書く

---

## 新規顧客追加手順

1. ASCII safe な slug を決めて `customers/<slug>/` を作成
2. `customers/<slug>/README.md` に下記を記入（最低限）
   - 正式名（日本語可）
   - 業界プロファイル（`profiles/*.json` から選定 or 新規作成）
   - 物理環境（モニター枚数／OS／レセコン or 業務システム名）
   - ビルド引数（`scripts/build_for_customer.sh` に渡す値）
   - 状態（設計中／ビルド済／パイロット投入済／運用中）
3. 業界プロファイルが既存にあれば流用、無ければ `profiles/<new>.json` を追加
4. `docs/consent_form.html` をベースに、`customers/<slug>/consent_form.html` として
   会社名・対象業務・マスキング対象を差し込んだ同意書を作成
5. `scripts/build_for_customer.sh` を README に書かれた引数で実行
   → `dist/WorkScope_<customer>_<date>.exe` を生成
6. パイロット投入後の実機状況を `customers/<slug>/README.md` 末尾に追記

---

## 既存顧客一覧

| slug | 正式名 | 業界 | 業界プロファイル | 状態 |
|---|---|---|---|---|
| `murakami` | 村上さん | 中古車（情報入力業務） | なし（generic 相当） | ビルド前 |

### 大森薬局について（移行待ち）

`docs/consent_form_大森薬局.html` が既存。今後、本ディレクトリ配下
`customers/oomori-pharmacy/`（仮）へ移行整理する候補。
（直近のパイロット運用が片付いてから機械的に移す。今すぐ動かすと配布パッケージへの
影響が出る可能性があるので保留中）
