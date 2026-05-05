"""ワンクリック起動の同意画面.

設計方針:
- 初回起動時のみ同意ダイアログを表示
- 同意すると %APPDATA%/WorkScope/consent_signed.json を生成
- 2回目以降は consent_signed.json があればダイアログをスキップ
- 同意書本文は docs/consent_form.html (PyInstaller同梱、bundled_doc_path 経由)
- ダイアログは tkinter ベースで PyInstaller互換 (シンプル, 同梱が軽い)

PII保護: 同意書なしでデータ収集は絶対に開始しない（main.py のガード）.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CONSENT_FILE = "consent_signed.json"


@dataclass
class ConsentRecord:
    """同意記録."""
    customer_name: str = ""
    industry_profile: str = ""
    upload_endpoint: str = ""
    consented_at: str = ""           # ISO 8601
    schema_version: int = 1
    user_signature: str = ""         # ユーザー入力の確認名（任意）

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def consent_file_path() -> Path:
    """同意書記録ファイルのパス."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    p = Path(base) / "WorkScope"
    p.mkdir(parents=True, exist_ok=True)
    return p / CONSENT_FILE


def is_consented() -> bool:
    """同意済みか判定. consent_signed.json の存在で判定."""
    p = consent_file_path()
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return bool(data.get("consented_at"))
    except (OSError, json.JSONDecodeError):
        return False


def record_consent(
    customer_name: str = "",
    industry_profile: str = "",
    upload_endpoint: str = "",
    user_signature: str = "",
) -> ConsentRecord:
    """同意を記録. consent_signed.json を生成."""
    rec = ConsentRecord(
        customer_name=customer_name,
        industry_profile=industry_profile,
        upload_endpoint=upload_endpoint,
        consented_at=datetime.now(timezone.utc).astimezone().isoformat(),
        user_signature=user_signature,
    )
    consent_file_path().write_text(rec.to_json(), encoding="utf-8")
    logger.info("consent recorded for customer=%s industry=%s",
                customer_name, industry_profile)
    return rec


def revoke_consent() -> None:
    """同意取り消し. consent_signed.json を削除. データ収集を停止する用途."""
    p = consent_file_path()
    if p.exists():
        try:
            p.unlink()
            logger.info("consent revoked: %s deleted", p)
        except OSError:
            logger.exception("failed to delete consent file: %s", p)


def get_bundled_consent_html() -> Optional[Path]:
    """同梱された consent_form.html のパスを返す（PyInstaller 配布時 + 開発時両対応）."""
    # 1. PyInstaller frozen
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            p = Path(meipass) / "docs" / "consent_form.html"
            if p.exists():
                return p
    # 2. リポジトリ内
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "docs" / "consent_form.html",):
        if cand.exists():
            return cand
    return None


def show_consent_dialog(
    customer_name: str = "",
    industry_profile: str = "generic",
    upload_endpoint: str = "",
) -> bool:
    """同意ダイアログを表示し、ユーザーの選択を返す.

    同意 → True (consent_signed.json 生成)
    キャンセル → False (アプリは終了すべき)

    tkinter が無い環境/ヘッドレス環境では「同意なし」とみなし False を返す。
    """
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext
    except ImportError:
        logger.warning("tkinter not available; cannot show consent dialog")
        return False

    consented = {"value": False}

    def _on_agree():
        record_consent(
            customer_name=customer_name,
            industry_profile=industry_profile,
            upload_endpoint=upload_endpoint,
        )
        consented["value"] = True
        root.destroy()

    def _on_decline():
        consented["value"] = False
        root.destroy()

    def _open_full_consent_in_browser():
        html = get_bundled_consent_html()
        if html and html.exists():
            try:
                webbrowser.open(html.as_uri())
            except Exception:
                logger.exception("failed to open consent_form.html in browser")
                messagebox.showwarning("WorkScope", "同意書を開けませんでした。")

    root = tk.Tk()
    root.title("WorkScope - 利用同意")
    root.geometry("600x500")
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    # ヘッダー
    header = tk.Label(
        root,
        text="業務スクリーン記録ツール 利用同意",
        font=("Yu Gothic", 14, "bold"),
        fg="#1e3a5f",
        pady=10,
    )
    header.pack()

    label_customer = customer_name or "（顧客名未設定）"
    label_industry = industry_profile or "generic"

    # 顧客情報
    info = tk.Label(
        root,
        text=f"顧客: {label_customer}\n業界プロファイル: {label_industry}",
        font=("Yu Gothic", 10),
        fg="#4a5568",
        pady=4,
    )
    info.pack()

    # サマリーテキスト
    summary_text = (
        "本ツールは、業務改善のため以下を実施します:\n"
        "\n"
        "1. アクティブウィンドウ変化時にスクリーンショットを取得\n"
        "2. 患者氏名・保険者番号・電話番号・住所等の個人情報を\n"
        "    自動的に黒塗り（マスク）してから保存\n"
        "3. 完全にこの端末内で完結（生のスクリーンショットは破棄）\n"
        "4. キーボードのキー種別（Tab/Enter等）と入力桁数のみ記録\n"
        "    （実際の入力文字は一切保存されません）\n"
        "5. パスワード入力中は記録を完全停止\n"
        "\n"
        "詳細は「同意書全文を表示」ボタンからご確認ください。\n"
        "同意後はタスクトレイから一時停止・撤回が可能です。"
    )
    text_box = scrolledtext.ScrolledText(
        root, font=("Yu Gothic", 10), height=14, wrap=tk.WORD,
        bg="#f5f7fa", fg="#1a1a2e", padx=10, pady=10,
    )
    text_box.insert("1.0", summary_text)
    text_box.config(state="disabled")
    text_box.pack(fill="both", expand=True, padx=12, pady=8)

    # ボタン群
    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=10)

    btn_full = tk.Button(
        btn_frame, text="同意書全文を表示",
        command=_open_full_consent_in_browser,
        bg="#e2e8f0", fg="#1a1a2e", padx=14, pady=6,
    )
    btn_full.grid(row=0, column=0, padx=6)

    btn_decline = tk.Button(
        btn_frame, text="同意しない（終了）",
        command=_on_decline,
        bg="#ffffff", fg="#c53030", padx=14, pady=6,
    )
    btn_decline.grid(row=0, column=1, padx=6)

    btn_agree = tk.Button(
        btn_frame, text="同意して開始",
        command=_on_agree,
        bg="#1e3a5f", fg="#ffffff", padx=20, pady=6,
        font=("Yu Gothic", 10, "bold"),
    )
    btn_agree.grid(row=0, column=2, padx=6)

    root.mainloop()
    return consented["value"]


def ensure_consent_or_exit(
    customer_name: str = "",
    industry_profile: str = "generic",
    upload_endpoint: str = "",
) -> bool:
    """同意確認のメインエントリ. 既に同意済みなら True、未同意なら同意ダイアログ表示.

    ダイアログで同意 → True / 拒否 or キャンセル → False
    呼び出し側は False の場合、main を終了すべき。
    """
    if is_consented():
        return True
    return show_consent_dialog(
        customer_name=customer_name,
        industry_profile=industry_profile,
        upload_endpoint=upload_endpoint,
    )


__all__ = [
    "ConsentRecord",
    "consent_file_path",
    "is_consented",
    "record_consent",
    "revoke_consent",
    "show_consent_dialog",
    "ensure_consent_or_exit",
    "get_bundled_consent_html",
]
