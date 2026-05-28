"""OAuth Refresh Token 取得スクリプト (ビルド事前作業).

v1.1-lite で SA から OAuth Refresh Token 方式に変更したため、
Collector の EXE に埋め込む資格情報をビルド前に取得する必要がある。

使い方:
    python scripts/issue_refresh_token.py path/to/client_secret.json

実行内容:
    1. GCP でダウンロードした OAuth Client ID (Desktop App) の
       client_secret.json を読み込み
    2. InstalledAppFlow.run_local_server() でブラウザ認証
       (髙石さんが Google にログインして scope を許可)
    3. Refresh Token を取得
    4. Collector 埋め込み用 JSON (refresh_token / client_id / client_secret) を
       標準出力に整形表示。stderr に build_for_customer.sh の
       --oauth-credentials に渡す JSON ファイルパスの作り方をガイドする。

事前準備:
    1. https://console.cloud.google.com で OAuth 同意画面を設定
       - User Type: 「外部」(個人 Gmail でも可)
       - スコープ: https://www.googleapis.com/auth/drive.file を追加
       - テストユーザー: 認証するアカウント (例: pondering1083@gmail.com) を追加
    2. 認証情報 → OAuth クライアント ID を作成
       - アプリケーションの種類: 「デスクトップアプリ」
       - 名前: 任意 (例: workscope-collector-cli)
    3. JSON をダウンロード (client_secret_xxx.json)

注意:
    - 取得した Refresh Token は EXE に埋め込まれて顧客 PC に配布される。
      漏洩リスクを抑えるため、scope は drive.file (このアプリ作成ファイルのみ)
      に限定している。
    - Refresh Token は無期限ではない (Google の仕様で、長期間未使用 or
      パスワード変更等で無効化される可能性あり)。顧客配布前に動作確認すること。
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

# OAuth スコープ: drive.file = このアプリが作成したファイルのみアクセス可能
# gdrive_uploader.py / remote_control.py と揃える
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def main() -> int:
    if len(sys.argv) != 2:
        _err("Usage: python scripts/issue_refresh_token.py path/to/client_secret.json")
        _err("")
        _err("GCP で OAuth Client ID (Desktop App) を作成してダウンロードした JSON を渡してください")
        return 1

    client_secret_path = Path(sys.argv[1])
    if not client_secret_path.is_file():
        _err(f"ERROR: client_secret JSON not found: {client_secret_path}")
        return 2

    # 依存ライブラリは scripts 単体実行を想定して遅延 import
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError:
        _err("ERROR: google-auth-oauthlib is not installed.")
        _err("       pip install google-auth-oauthlib==1.2.0")
        return 3

    _err(f"[1/3] reading client secret: {client_secret_path}")
    try:
        with open(client_secret_path, "r", encoding="utf-8") as f:
            client = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _err(f"ERROR: failed to read client_secret JSON: {e}")
        return 4

    # GCP の Desktop App は "installed" キー、Web App は "web" キーを持つ
    client_dict = client.get("installed") or client.get("web")
    if not isinstance(client_dict, dict):
        _err("ERROR: client_secret JSON must have 'installed' or 'web' key")
        return 5
    client_id = client_dict.get("client_id", "")
    client_secret = client_dict.get("client_secret", "")
    if not (client_id and client_secret):
        _err("ERROR: client_id / client_secret missing in client_secret JSON")
        return 6

    _err(f"[2/3] launching browser for OAuth consent (scope: {SCOPES[0]})")
    _err("      → ブラウザが立ち上がります。Google にログインして許可してください。")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret_path), SCOPES
        )
        creds = flow.run_local_server(port=0, open_browser=True)
    except Exception as e:
        _err(f"ERROR: OAuth flow failed: {e}")
        return 7

    refresh_token = getattr(creds, "refresh_token", None)
    if not refresh_token:
        _err("ERROR: refresh_token was not returned by Google.")
        _err("       - OAuth 同意画面のテストユーザーに自分のアカウントが入っているか確認")
        _err("       - 以前に同じ Client ID で許可済みの場合、Google アカウント設定の")
        _err("         「Google アカウントへのアクセスがあるサードパーティ製アプリ」から")
        _err("         一度削除してから再実行すると refresh_token が返ります。")
        return 8

    _err("[3/3] writing JSON to stdout + base64 to stderr")

    out = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    out_json = json.dumps(out, ensure_ascii=False, indent=2)
    # 標準出力に JSON (リダイレクトでファイル保存しやすい形)
    print(out_json)

    # stderr に base64 (build_for_customer.sh 内部で生成される値の確認用)
    b64 = base64.b64encode(json.dumps(out).encode("utf-8")).decode("ascii")
    _err("")
    _err("=========================================================")
    _err("  ✓ OAuth Refresh Token を取得しました")
    _err("=========================================================")
    _err("")
    _err("【次の手順】")
    _err("  1. 上記の JSON を `oauth-tribe-001.json` 等として保存")
    _err("     (顧客IDを名前に入れて識別しやすくする)")
    _err("       例: python scripts/issue_refresh_token.py client_secret.json \\")
    _err("           > ~/keys/oauth-tribe-001.json")
    _err("")
    _err("  2. build_for_customer.sh に --oauth-credentials で渡す:")
    _err("       bash scripts/build_for_customer.sh \\")
    _err("         --mode lite \\")
    _err("         --customer 'テスト顧客' \\")
    _err("         --customer-id tribe-001 \\")
    _err("         --gdrive-folder-id <FOLDER_ID> \\")
    _err("         --oauth-credentials ~/keys/oauth-tribe-001.json")
    _err("")
    _err("【参考】base64 エンコード結果 (build_for_customer.sh 内部値):")
    _err(f"  {b64}")
    _err("")
    _err("⚠️  保管時の注意:")
    _err("  - oauth-tribe-001.json は機密情報 (Refresh Token を含む)")
    _err("  - パスワードマネージャ or 暗号化ストレージに保管すること")
    _err("  - Git リポジトリには絶対にコミットしない")
    return 0


if __name__ == "__main__":
    sys.exit(main())
