#!/bin/bash
# ============================================================
# 立即手動跑一次（測試用）
# 會載入 .env.sh 的環境變數，並以測試模式強制執行（休市日也跑）
# ============================================================
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [ ! -f "$PROJECT_DIR/.env.sh" ]; then
  echo "[錯誤] 找不到 .env.sh，請先依 README_mac.md 建立環境變數檔"
  exit 1
fi

# 載入 Gmail、表單網址等機密設定
set -a
source "$PROJECT_DIR/.env.sh"
set +a

# WeasyPrint 需要 Homebrew 的 pango/cairo，但 macOS 的動態載入器預設不看
# Homebrew 的 lib 目錄（Apple Silicon 在 /opt/homebrew，Intel 在 /usr/local）。
# 沒有這行 PDF 會產生失敗，錯誤訊息是 "cannot load library libpango-1.0-0"。
for d in /opt/homebrew/lib /usr/local/lib; do
  [ -d "$d" ] && export DYLD_FALLBACK_LIBRARY_PATH="$d:$DYLD_FALLBACK_LIBRARY_PATH"
done

# FORCE=1：休市日也強制執行，方便測試
FORCE=1 "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/daily_brief_multi.py"
