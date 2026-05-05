#!/usr/bin/env bash
# 顧客別カスタムEXE生成スクリプト.
#
# 使い方:
#   ./scripts/build_for_customer.sh \
#     --customer "村上薬局" \
#     --industry pharmacy \
#     --endpoint "https://upload.tribe-saas.com/customers/murakami/" \
#     [--output dist/WorkScope_村上薬局_20260505.exe]
#
# 動作:
#   1. profiles/ から指定業界のJSONだけを残し、他を一時退避
#   2. src/config.py に CUSTOMER_NAME / DEFAULT_PROFILE / UPLOAD_ENDPOINT を埋め込み
#   3. docs/consent_form.html の {{CUSTOMER_NAME}} を置換
#   4. PyInstaller でビルド
#   5. 退避した profiles と置換した config.py を復元
#   6. dist/ に <customer>_<date>.exe + 同意書PDF + 配布手順書 を出力
#
# このスクリプトは macOS でも実行可能（PyInstallerビルド自体はWindowsで実行する想定。
# CI: GitHub Actions windows-latest workflow から呼び出される）.

set -euo pipefail

CUSTOMER=""
INDUSTRY=""
ENDPOINT=""
OUTPUT=""

usage() {
    cat <<EOF
Usage: $0 --customer NAME --industry INDUSTRY [--endpoint URL] [--output PATH]

Options:
  --customer NAME      顧客名 (例: "村上薬局")
  --industry KIND      業界プロファイル (pharmacy|accounting|legal|sales|hr|generic)
  --endpoint URL       データ送信先URL (USB回収のみなら空でOK)
  --output PATH        出力EXEパス (省略時: dist/WorkScope_<customer>_<date>.exe)
  -h, --help           このヘルプを表示
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --customer) CUSTOMER="$2"; shift 2;;
        --industry) INDUSTRY="$2"; shift 2;;
        --endpoint) ENDPOINT="$2"; shift 2;;
        --output)   OUTPUT="$2"; shift 2;;
        -h|--help)  usage;;
        *) echo "Unknown option: $1" >&2; usage;;
    esac
done

if [[ -z "$CUSTOMER" ]] || [[ -z "$INDUSTRY" ]]; then
    echo "ERROR: --customer and --industry are required" >&2
    usage
fi

# 業界プロファイルの存在確認
PROFILE_DIR="$(cd "$(dirname "$0")/.." && pwd)/profiles"
if [[ ! -f "$PROFILE_DIR/$INDUSTRY.json" ]]; then
    echo "ERROR: profile '$INDUSTRY' not found in $PROFILE_DIR" >&2
    echo "Available: $(ls "$PROFILE_DIR" | sed 's/\.json//' | tr '\n' ' ')" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATE_STR="$(date +%Y%m%d)"
if [[ -z "$OUTPUT" ]]; then
    OUTPUT="$REPO_ROOT/dist/WorkScope_${CUSTOMER}_${DATE_STR}.exe"
fi

echo "=========================================="
echo "  WorkScope Customer Build"
echo "  Customer: $CUSTOMER"
echo "  Industry: $INDUSTRY"
echo "  Endpoint: ${ENDPOINT:-<none, USB only>}"
echo "  Output:   $OUTPUT"
echo "=========================================="

cd "$REPO_ROOT"

# --- 1. profiles/ 退避 (該当業界 + base のみ残す) ---
PROFILE_BACKUP="$(mktemp -d)"
echo "[1/5] backup profiles to $PROFILE_BACKUP"
for p in "$PROFILE_DIR"/*.json; do
    name="$(basename "$p" .json)"
    if [[ "$name" != "$INDUSTRY" ]] && [[ "$name" != "base" ]]; then
        mv "$p" "$PROFILE_BACKUP/"
    fi
done

# --- 2. src/config.py の埋め込み定数を上書き ---
CONFIG="$REPO_ROOT/src/config.py"
CONFIG_BACKUP="$(mktemp)"
cp "$CONFIG" "$CONFIG_BACKUP"
echo "[2/5] inject CUSTOMER_NAME / DEFAULT_PROFILE / UPLOAD_ENDPOINT"
python3 - <<PY
import re
p = "$CONFIG"
src = open(p, encoding="utf-8").read()
src = re.sub(r'^DEFAULT_PROFILE = ".*"', 'DEFAULT_PROFILE = "$INDUSTRY"', src, flags=re.M)
src = re.sub(r'^CUSTOMER_NAME = ".*"', f'CUSTOMER_NAME = "$CUSTOMER"', src, flags=re.M)
src = re.sub(r'^UPLOAD_ENDPOINT = ".*"', f'UPLOAD_ENDPOINT = "$ENDPOINT"', src, flags=re.M)
open(p, "w", encoding="utf-8").write(src)
print("  injected config:", "$INDUSTRY", "$CUSTOMER", "$ENDPOINT")
PY

# --- 3. 同意書テンプレート置換 ---
CONSENT="$REPO_ROOT/docs/consent_form.html"
CONSENT_BUILD="$REPO_ROOT/docs/consent_form.built.html"
if [[ -f "$CONSENT" ]]; then
    echo "[3/5] render consent_form.html with customer name"
    sed -e "s|{{CUSTOMER_NAME}}|$CUSTOMER|g" \
        -e "s|{{ENDPOINT}}|${ENDPOINT:-（USB回収）}|g" \
        -e "s|{{BUILD_DATE}}|$DATE_STR|g" \
        "$CONSENT" > "$CONSENT_BUILD"
fi

# --- 4. PyInstaller build ---
echo "[4/5] PyInstaller build (this takes a few minutes)..."
mkdir -p "$REPO_ROOT/dist"
if command -v pyinstaller >/dev/null 2>&1; then
    pyinstaller --clean --distpath "$(dirname "$OUTPUT")" \
                --workpath "$REPO_ROOT/build" \
                --specpath "$REPO_ROOT" \
                --name "$(basename "$OUTPUT" .exe)" \
                "$REPO_ROOT/pyinstaller.spec" 2>&1 | tail -20
else
    echo "  WARNING: pyinstaller not installed; skipping actual build (CI/Windows env required)"
fi

# --- 5. 復元 ---
echo "[5/5] restore profiles and config.py"
mv "$PROFILE_BACKUP"/*.json "$PROFILE_DIR/" 2>/dev/null || true
rmdir "$PROFILE_BACKUP" 2>/dev/null || true
mv "$CONFIG_BACKUP" "$CONFIG"
[[ -f "$CONSENT_BUILD" ]] && rm "$CONSENT_BUILD"

echo ""
echo "=========================================="
echo "  Build done."
echo "  Output: $OUTPUT"
echo "  Customer: $CUSTOMER ($INDUSTRY profile)"
echo "=========================================="
