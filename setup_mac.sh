#!/bin/bash
# ============================================================
# macOS 安裝腳本：把每日報告設定成本機準時排程（launchd）
# 用法：把整個專案資料夾放好後，在資料夾內執行
#   bash setup_mac.sh
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_LABEL="com.howard.dailybrief"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
PY="$PROJECT_DIR/.venv/bin/python"

echo "=== 每日報告 macOS 安裝 ==="
echo "專案目錄：$PROJECT_DIR"
echo ""

# 1) 檢查 python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "[錯誤] 找不到 python3。請先安裝："
  echo "  方法一（推薦）：先裝 Homebrew（https://brew.sh），再執行 brew install python"
  echo "  方法二：到 python.org 下載 macOS 安裝檔"
  exit 1
fi
echo "[1/5] python3：$(python3 --version)"

# 2) 建立虛擬環境並裝套件
echo "[2/5] 建立虛擬環境並安裝套件（約 1-2 分鐘）…"
python3 -m venv "$PROJECT_DIR/.venv"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$PROJECT_DIR/requirements_free.txt"
echo "      完成"

# 3) PDF 引擎與中文字型檢查
# WeasyPrint 需要 pango/cairo 這些 C 函式庫，pip 不會幫你裝，要用 Homebrew。
# 且 macOS 動態載入器預設不看 Homebrew 的 lib 目錄，所以要設 DYLD_FALLBACK_LIBRARY_PATH
# （run_now.sh / run_scheduled.sh 已內建這行，這裡只是為了讓檢查跑得起來）
for d in /opt/homebrew/lib /usr/local/lib; do
  [ -d "$d" ] && export DYLD_FALLBACK_LIBRARY_PATH="$d:$DYLD_FALLBACK_LIBRARY_PATH"
done

echo "[3/5] 檢查 PDF 引擎…"
if "$PY" -c "from weasyprint import HTML" 2>/dev/null; then
  echo "      PDF 引擎正常"
else
  echo "      [錯誤] WeasyPrint 缺少系統函式庫，PDF 會產生失敗。請執行："
  echo "        brew install pango"
  echo "      裝完後重新執行 bash setup_mac.sh"
fi

echo "      檢查中文字型…"
if "$PY" - <<'PYEOF'
import sys
try:
    from weasyprint.text.fonts import FontConfiguration  # noqa
except Exception:
    pass
# macOS 內建蘋方（PingFang TC）與宋體，多數情況直接可用
import subprocess
out = subprocess.run(["fc-list", ":lang=zh"], capture_output=True, text=True)
sys.exit(0 if out.stdout.strip() else 1)
PYEOF
then
  echo "      系統已有中文字型，PDF 中文可正常顯示"
else
  echo "      [注意] 未偵測到中文字型。若 PDF 中文變成方框，執行："
  echo "      brew install --cask font-noto-sans-cjk-tc"
fi

# 4) 產生 launchd 設定檔（每天 16:10 準時觸發，只在週一~五）
echo "[4/5] 設定排程（每天 16:10，週一~五）…"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>${PROJECT_DIR}/run_scheduled.sh</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${PROJECT_DIR}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>

  <!-- 週一~五 16:10 觸發；Weekday 1-5 = 週一到週五 -->
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>10</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>10</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>10</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>10</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>10</integer></dict>
  </array>

  <!-- 若觸發時電腦在睡眠，醒來後會補跑一次（macOS 特性） -->
  <key>RunAtLoad</key>
  <false/>

  <key>StandardOutPath</key>
  <string>${PROJECT_DIR}/run.log</string>
  <key>StandardErrorPath</key>
  <string>${PROJECT_DIR}/run.log</string>
</dict>
</plist>
PLIST
echo "      已建立：$PLIST_PATH"

# 5) 載入排程
echo "[5/5] 載入排程…"
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "      已載入"
echo ""
echo "=== 安裝完成 ==="
echo ""
echo "還差最後一步：建立環境變數檔 .env.sh（填入你的 Gmail 等資訊）"
echo "請看 README_mac.md 的「設定環境變數」段落。"
echo ""
echo "常用指令："
echo "  立即測試一次：    bash $PROJECT_DIR/run_now.sh"
echo "  查看執行紀錄：    cat $PROJECT_DIR/run.log"
echo "  停用排程：        launchctl unload $PLIST_PATH"
echo "  重新啟用：        launchctl load $PLIST_PATH"
