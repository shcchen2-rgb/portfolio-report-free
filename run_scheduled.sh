#!/bin/zsh
# ============================================================
# launchd 排程實際執行的腳本（不要手動跑這個，測試請用 run_now.sh）
# 職責：載入環境變數 → 執行主程式（正常模式，會自動判斷休市）
# ============================================================
PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

echo "==================== $(date '+%Y-%m-%d %H:%M:%S %Z') 排程觸發 ===================="

if [ -f "$PROJECT_DIR/.env.sh" ]; then
  set -a
  source "$PROJECT_DIR/.env.sh"
  set +a
else
  echo "[錯誤] 找不到 .env.sh，無法載入 Gmail 等設定"
  exit 1
fi

# WeasyPrint 需要 Homebrew 的 pango/cairo，但 macOS 的動態載入器預設不看
# Homebrew 的 lib 目錄（Apple Silicon 在 /opt/homebrew，Intel 在 /usr/local）。
# 沒有這行 PDF 會產生失敗，錯誤訊息是 "cannot load library libpango-1.0-0"。
for d in /opt/homebrew/lib /usr/local/lib; do
  [ -d "$d" ] && export DYLD_FALLBACK_LIBRARY_PATH="$d:$DYLD_FALLBACK_LIBRARY_PATH"
done

# 正常模式：不帶 FORCE，程式會自動偵測休市日並跳過
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/daily_brief_multi.py"

echo "==================== $(date '+%Y-%m-%d %H:%M:%S %Z') 執行結束 ===================="
echo ""
