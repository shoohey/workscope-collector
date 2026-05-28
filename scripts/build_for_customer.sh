#!/usr/bin/env bash
# 顧客別カスタムEXE生成スクリプト (Codex Critical#2 対応版).
#
# 使い方:
#   ./scripts/build_for_customer.sh \
#     --customer "村上薬局" \
#     --industry pharmacy \
#     --endpoint "https://upload.tribe-saas.com/customers/murakami/" \
#     --api-key "<bearer_api_key>" \
#     [--output dist/WorkScope_村上薬局_20260505.exe]
#
# 動作:
#   1. リポジトリ全体を一時ディレクトリへ rsync (作業ツリー汚染ゼロ)
#   2. 一時ディレクトリ内で profiles/ を該当業界のみに絞る
#   3. 一時ディレクトリ内で _build_constants.py を生成（src/config.py には書き込まない）
#   4. 一時ディレクトリ内で PyInstaller を実行
#   5. 生成EXEを dist/ にコピー
#   6. 一時ディレクトリは trap で必ず削除（成功・失敗・割込のいずれでも）
#
# 設計判断 (Codex Critical#2 対応):
#   - リポジトリ作業ツリーを一切変更しない（profiles 退避や config.py書換を廃止）
#   - 一時ディレクトリは trap EXIT/ERR/INT で必ず cleanup
#   - 失敗時もリポジトリ汚染なし、APIキーが作業ツリーに残らない

set -euo pipefail

CUSTOMER=""
INDUSTRY=""
ENDPOINT=""
API_KEY=""
OUTPUT=""
WORK_DIR=""
RAW_CAPTURE="false"

# v1.1-lite: 新規オプション
MODE="full"                   # "full" (v1.0 薬局向け) | "lite" (v1.1 汎用)
CUSTOMER_ID=""                # "tribe-001" 等
GDRIVE_FOLDER_ID=""           # 共有ドライブ内顧客フォルダのID
SERVICE_ACCOUNT_KEY=""        # SAキーJSONファイルパス

usage() {
    cat <<EOF
Usage: $0 --customer NAME --industry INDUSTRY [--endpoint URL] [--api-key KEY] [--output PATH] [--raw-capture]
       $0 --mode lite --customer NAME --customer-id ID --gdrive-folder-id ID --service-account-key PATH [--output PATH]

Common options:
  --customer NAME            顧客名 (例: "村上薬局" / "テスト顧客")
  --output PATH              出力EXEパス (省略時: dist/WorkScope_<customer>_<date>.exe)
  -h, --help                 このヘルプを表示

Full mode options (v1.0 薬局向け: スクショ+OCR+マスキング+Supabase):
  --industry KIND            業界プロファイル (pharmacy|accounting|legal|sales|hr|generic|...)
  --endpoint URL             データ送信先URL (USB回収のみなら空でOK)
  --api-key KEY              Bearer認証用APIキー (空でアップロード無効化)
  --raw-capture              OCR/マスク失敗時にも生スクショを保存する USB回収専用モード。

Lite mode options (v1.1 汎用: JSONLのみ+Google Drive直送+リモート制御):
  --mode lite                Lite版（汎用、医療系以外向け）でビルド
  --customer-id ID           顧客ID（例: tribe-001）。control.json/GDriveパス分離に使用
  --gdrive-folder-id ID      Google共有ドライブ内の顧客フォルダID（URLの /folders/{ID}）
  --service-account-key PATH サービスアカウントJSONキーのファイルパス（base64で焼き付け）

Examples:
  # Full（薬局向け既存）
  $0 --customer "村上薬局" --industry pharmacy --endpoint https://upload.tribe-saas.com/...

  # Lite（一般企業向け新規）
  $0 --mode lite --customer "テスト顧客" --customer-id tribe-001 \\
     --gdrive-folder-id 1AbCdEfGhIjKlMnOp --service-account-key ./sa-tribe-001.json
EOF
    exit 0
}

cleanup() {
    local exit_code=$?
    if [[ -n "$WORK_DIR" ]] && [[ -d "$WORK_DIR" ]]; then
        echo "[cleanup] removing temp build dir: $WORK_DIR"
        rm -rf "$WORK_DIR" 2>/dev/null || true
    fi
    if [[ $exit_code -ne 0 ]]; then
        echo "[cleanup] build FAILED (exit=$exit_code). Repository working tree untouched." >&2
    fi
    exit $exit_code
}
# trap は引数解析より前に登録。シグナル経由の中断でも cleanup が走る
trap cleanup EXIT INT TERM

while [[ $# -gt 0 ]]; do
    case "$1" in
        --customer)              CUSTOMER="$2"; shift 2;;
        --industry)              INDUSTRY="$2"; shift 2;;
        --endpoint)              ENDPOINT="$2"; shift 2;;
        --api-key)               API_KEY="$2"; shift 2;;
        --output)                OUTPUT="$2"; shift 2;;
        --raw-capture)           RAW_CAPTURE="true"; shift;;
        --mode)                  MODE="$2"; shift 2;;
        --customer-id)           CUSTOMER_ID="$2"; shift 2;;
        --gdrive-folder-id)      GDRIVE_FOLDER_ID="$2"; shift 2;;
        --service-account-key)   SERVICE_ACCOUNT_KEY="$2"; shift 2;;
        -h|--help)               usage;;
        *) echo "Unknown option: $1" >&2; usage;;
    esac
done

# モード判定
if [[ "$MODE" != "full" && "$MODE" != "lite" ]]; then
    echo "ERROR: --mode must be 'full' or 'lite', got '$MODE'" >&2
    exit 1
fi

# 共通必須チェック
if [[ -z "$CUSTOMER" ]]; then
    echo "ERROR: --customer is required" >&2
    usage
fi

if [[ "$MODE" == "full" ]]; then
    # Full モード（v1.0 薬局向け）の必須チェック
    if [[ -z "$INDUSTRY" ]]; then
        echo "ERROR: --industry is required in full mode" >&2
        usage
    fi
    # 安全ガード: クラウドアップロード有効と --raw-capture は併用禁止
    if [[ "$RAW_CAPTURE" == "true" ]] && [[ -n "$ENDPOINT" ]]; then
        echo "ERROR: --raw-capture cannot be combined with --endpoint." >&2
        echo "  raw-capture is for USB-only deployments where unmasked screenshots" >&2
        echo "  are protected by physical custody. Cloud upload + raw capture would" >&2
        echo "  leak unmasked PII to the upload endpoint." >&2
        exit 1
    fi
else
    # Lite モード（v1.1 汎用）の必須チェック
    if [[ -z "$CUSTOMER_ID" ]]; then
        echo "ERROR: --customer-id is required in lite mode (例: tribe-001)" >&2
        usage
    fi
    if [[ -z "$GDRIVE_FOLDER_ID" ]]; then
        echo "ERROR: --gdrive-folder-id is required in lite mode" >&2
        usage
    fi
    if [[ -z "$SERVICE_ACCOUNT_KEY" ]]; then
        echo "ERROR: --service-account-key is required in lite mode" >&2
        usage
    fi
    if [[ ! -f "$SERVICE_ACCOUNT_KEY" ]]; then
        echo "ERROR: service account key file not found: $SERVICE_ACCOUNT_KEY" >&2
        exit 1
    fi
    # Lite モードは業界マスキング不要なので generic 固定
    if [[ -z "$INDUSTRY" ]]; then
        INDUSTRY="generic"
    fi
    # Lite モードでは Supabase 系オプションを使わない
    if [[ -n "$ENDPOINT" ]] || [[ -n "$API_KEY" ]] || [[ "$RAW_CAPTURE" == "true" ]]; then
        echo "WARN: --endpoint/--api-key/--raw-capture are ignored in lite mode" >&2
    fi
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 業界プロファイルの存在確認 (リポジトリ側)
if [[ ! -f "$REPO_ROOT/profiles/$INDUSTRY.json" ]]; then
    echo "ERROR: profile '$INDUSTRY' not found in $REPO_ROOT/profiles" >&2
    echo "Available: $(ls "$REPO_ROOT/profiles" | sed 's/\.json//' | tr '\n' ' ')" >&2
    exit 1
fi

DATE_STR="$(date +%Y%m%d)"
if [[ -z "$OUTPUT" ]]; then
    OUTPUT="$REPO_ROOT/dist/WorkScope_${CUSTOMER}_${DATE_STR}.exe"
fi

echo "=========================================="
echo "  WorkScope Customer Build (isolated)"
echo "  Mode:         $MODE"
echo "  Customer:     $CUSTOMER"
if [[ "$MODE" == "lite" ]]; then
    echo "  CustomerID:   $CUSTOMER_ID"
    echo "  GDrive folder:$GDRIVE_FOLDER_ID"
    echo "  SA key:       ***embedded (base64) from $(basename "$SERVICE_ACCOUNT_KEY")***"
else
    echo "  Industry:     $INDUSTRY"
    echo "  Endpoint:     ${ENDPOINT:-<none, USB only>}"
    echo "  API key:      $([ -n "$API_KEY" ] && echo "***set***" || echo "<none>")"
    echo "  Raw capture:  $RAW_CAPTURE"
fi
echo "  Output:       $OUTPUT"
echo "=========================================="

# --- 1. 一時ビルドディレクトリへリポジトリをコピー ---
WORK_DIR="$(mktemp -d -t workscope_build_XXXXXX)"
echo "[1/5] copy repo → $WORK_DIR"
rsync -a --quiet \
    --exclude '.git' \
    --exclude 'node_modules' \
    --exclude 'build' \
    --exclude 'dist' \
    --exclude 'build-artifacts' \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude '*.pyc' \
    "$REPO_ROOT/" "$WORK_DIR/"

# --- 2. profiles を該当業界 + base のみに絞る (一時ディレクトリ内) ---
echo "[2/5] reduce profiles to {base, $INDUSTRY}"
for p in "$WORK_DIR/profiles/"*.json; do
    name="$(basename "$p" .json)"
    if [[ "$name" != "$INDUSTRY" ]] && [[ "$name" != "base" ]]; then
        rm -f "$p"
    fi
done

# --- 3. _build_constants.py を生成 (config.py は書き換えない) ---
echo "[3/5] generate src/_build_constants.py"
# shell の true/false を Python の True/False に変換 (NameError 防止)
if [[ "$RAW_CAPTURE" == "true" ]]; then
    RAW_CAPTURE_PY="True"
else
    RAW_CAPTURE_PY="False"
fi

# Lite mode 用の追加定数を準備
COLLECTION_MODE_PY="full"
UPLOAD_BACKEND_PY="supabase"
REMOTE_CONTROL_ENABLED_PY="False"
SA_KEY_B64=""

if [[ "$MODE" == "lite" ]]; then
    COLLECTION_MODE_PY="lite"
    UPLOAD_BACKEND_PY="gdrive"
    REMOTE_CONTROL_ENABLED_PY="True"
    # SAキーJSONを base64 エンコード
    if command -v base64 >/dev/null 2>&1; then
        # macOS / BSD: -i ファイル
        # GNU coreutils: -w 0 で改行抑制
        if base64 --help 2>&1 | grep -q "GNU coreutils"; then
            SA_KEY_B64="$(base64 -w 0 < "$SERVICE_ACCOUNT_KEY")"
        else
            SA_KEY_B64="$(base64 < "$SERVICE_ACCOUNT_KEY" | tr -d '\n')"
        fi
    else
        echo "ERROR: base64 command not found" >&2
        exit 1
    fi
    if [[ -z "$SA_KEY_B64" ]]; then
        echo "ERROR: failed to encode service account key" >&2
        exit 1
    fi
fi

cat > "$WORK_DIR/src/_build_constants.py" <<PYEOF
# Auto-generated by build_for_customer.sh — DO NOT COMMIT
# This file is .gitignore'd and only exists in customer-specific builds.

CUSTOMER_NAME = "$CUSTOMER"
DEFAULT_PROFILE = "$INDUSTRY"
UPLOAD_ENDPOINT = "$ENDPOINT"
UPLOAD_API_KEY = "$API_KEY"
BUILD_DATE = "$DATE_STR"
RAW_CAPTURE_MODE_DEFAULT = $RAW_CAPTURE_PY

# v1.1-lite: 収集モード / アップロードバックエンド / リモート制御
COLLECTION_MODE = "$COLLECTION_MODE_PY"
UPLOAD_BACKEND = "$UPLOAD_BACKEND_PY"
REMOTE_CONTROL_ENABLED = $REMOTE_CONTROL_ENABLED_PY
CUSTOMER_ID = "$CUSTOMER_ID"
GDRIVE_FOLDER_ID = "$GDRIVE_FOLDER_ID"
GDRIVE_SERVICE_ACCOUNT_KEY_B64 = "$SA_KEY_B64"
PYEOF

# 既存 config.py に「ビルド時定数があれば優先する」追記
cat >> "$WORK_DIR/src/config.py" <<'PYEOF'

# v1.0: ビルド時定数の上書き (build_for_customer.sh が生成した
# _build_constants.py があればそちらの値を優先する)
try:
    from _build_constants import (  # type: ignore[import-not-found]
        CUSTOMER_NAME as _BUILD_CUSTOMER_NAME,
        DEFAULT_PROFILE as _BUILD_DEFAULT_PROFILE,
        UPLOAD_ENDPOINT as _BUILD_UPLOAD_ENDPOINT,
        UPLOAD_API_KEY as _BUILD_UPLOAD_API_KEY,
    )
    if _BUILD_CUSTOMER_NAME:
        CUSTOMER_NAME = _BUILD_CUSTOMER_NAME
    if _BUILD_DEFAULT_PROFILE:
        DEFAULT_PROFILE = _BUILD_DEFAULT_PROFILE
    if _BUILD_UPLOAD_ENDPOINT:
        UPLOAD_ENDPOINT = _BUILD_UPLOAD_ENDPOINT
    if _BUILD_UPLOAD_API_KEY:
        UPLOAD_API_KEY = _BUILD_UPLOAD_API_KEY
    # v1.1: USB回収顧客向けに raw_capture_mode の既定値も焼き付ける
    try:
        from _build_constants import (  # type: ignore[import-not-found]
            RAW_CAPTURE_MODE_DEFAULT as _BUILD_RAW_CAPTURE_MODE_DEFAULT,
        )
        RAW_CAPTURE_MODE_DEFAULT = bool(_BUILD_RAW_CAPTURE_MODE_DEFAULT)
    except ImportError:
        pass
    # v1.1-lite: 収集モード/GDrive直送/リモート制御の定数も焼き付ける
    try:
        from _build_constants import (  # type: ignore[import-not-found]
            COLLECTION_MODE as _BUILD_COLLECTION_MODE,
            UPLOAD_BACKEND as _BUILD_UPLOAD_BACKEND,
            REMOTE_CONTROL_ENABLED as _BUILD_REMOTE_CONTROL_ENABLED,
            CUSTOMER_ID as _BUILD_CUSTOMER_ID,
            GDRIVE_FOLDER_ID as _BUILD_GDRIVE_FOLDER_ID,
            GDRIVE_SERVICE_ACCOUNT_KEY_B64 as _BUILD_GDRIVE_SA_KEY_B64,
        )
        if _BUILD_COLLECTION_MODE:
            COLLECTION_MODE = _BUILD_COLLECTION_MODE
        if _BUILD_UPLOAD_BACKEND:
            UPLOAD_BACKEND = _BUILD_UPLOAD_BACKEND
        REMOTE_CONTROL_ENABLED = bool(_BUILD_REMOTE_CONTROL_ENABLED)
        if _BUILD_CUSTOMER_ID:
            CUSTOMER_ID = _BUILD_CUSTOMER_ID
        if _BUILD_GDRIVE_FOLDER_ID:
            GDRIVE_FOLDER_ID = _BUILD_GDRIVE_FOLDER_ID
        if _BUILD_GDRIVE_SA_KEY_B64:
            GDRIVE_SERVICE_ACCOUNT_KEY_B64 = _BUILD_GDRIVE_SA_KEY_B64
    except ImportError:
        pass
except ImportError:
    # 開発環境・テスト環境では _build_constants.py は存在しない
    pass
PYEOF

# --- 4. 同意書テンプレート選択＋置換 ---
# Lite モードでは consent_form_lite.html を consent_form.html として展開する
# （PyInstaller spec はファイル名 consent_form.html を期待しているため）
CONSENT="$WORK_DIR/docs/consent_form.html"
CONSENT_LITE="$WORK_DIR/docs/consent_form_lite.html"
if [[ "$MODE" == "lite" ]] && [[ -f "$CONSENT_LITE" ]]; then
    echo "[4/5] use consent_form_lite.html for lite mode customer"
    cp "$CONSENT_LITE" "$CONSENT"
fi

if [[ -f "$CONSENT" ]]; then
    if [[ "$MODE" == "lite" ]]; then
        ENDPOINT_LABEL="Google Drive (folder: $GDRIVE_FOLDER_ID)"
    else
        ENDPOINT_LABEL="${ENDPOINT:-（USB回収）}"
    fi
    echo "[4/5] render consent_form.html with customer info"
    sed -i.bak \
        -e "s|{{CUSTOMER_NAME}}|$CUSTOMER|g" \
        -e "s|{{CUSTOMER_ID}}|${CUSTOMER_ID:-（未設定）}|g" \
        -e "s|{{ENDPOINT}}|$ENDPOINT_LABEL|g" \
        -e "s|{{BUILD_DATE}}|$DATE_STR|g" \
        -e "s|{{COMPANY_NAME}}|株式会社TRIBE|g" \
        -e "s|{{SUPPORT_EMAIL}}|support@tribe.example|g" \
        "$CONSENT"
    rm -f "$CONSENT.bak"
else
    echo "[4/5] WARN: consent_form.html not found, skipping render"
fi

# --- 5. PyInstaller build ---
echo "[5/5] PyInstaller build (this takes a few minutes)..."
mkdir -p "$REPO_ROOT/dist"
if command -v pyinstaller >/dev/null 2>&1; then
    (
        cd "$WORK_DIR"
        pyinstaller --clean --noconfirm \
                    --distpath "$WORK_DIR/dist" \
                    --workpath "$WORK_DIR/build" \
                    --name "$(basename "$OUTPUT" .exe)" \
                    "$WORK_DIR/pyinstaller.spec" 2>&1 | tail -20
    )
    # 生成されたEXEを最終出力先へコピー
    BUILT_EXE="$WORK_DIR/dist/$(basename "$OUTPUT" .exe).exe"
    if [[ ! -f "$BUILT_EXE" ]]; then
        # macOS等、拡張子無しのバイナリの場合
        BUILT_EXE="$WORK_DIR/dist/$(basename "$OUTPUT" .exe)"
    fi
    if [[ -f "$BUILT_EXE" ]]; then
        cp "$BUILT_EXE" "$OUTPUT"
        echo "[5/5] copied built EXE → $OUTPUT"
    else
        echo "[5/5] WARN: PyInstaller did not produce expected EXE in $WORK_DIR/dist/"
    fi
else
    echo "[5/5] WARN: pyinstaller not installed; skipping actual build (CI/Windows env required)"
fi

echo ""
echo "=========================================="
echo "  Build done. Repository working tree is untouched."
echo "  Output: $OUTPUT"
echo "  Customer: $CUSTOMER ($INDUSTRY profile)"
echo "=========================================="
# trap cleanup will fire on EXIT and remove $WORK_DIR
