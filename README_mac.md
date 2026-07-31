# 每日報告 · macOS 桌機準時版

在自己的 Mac 上以 **launchd** 準時執行——不像 GitHub 排程會延遲數十分鐘到數小時，launchd 會在你指定的 16:10 準時觸發。AI 引擎與 GitHub Actions 版相同（Anthropic API），共用同一組金鑰。

## 桌機版 vs GitHub 版，怎麼選？

| | GitHub Actions 版 | macOS 桌機版 |
|---|---|---|
| 準時性 | 常延遲 30 分鐘~數小時 | **準時**（誤差幾秒） |
| 電腦要開機嗎 | 不用 | **要**（16:10 當下需開機、有網路、非睡眠） |
| 維護 | 全自動 | 需顧著電腦開機 |
| 適合 | 不在意早晚、只要每天有 | 一定要準時送達 |

**建議：兩個都留著當備援。** 桌機準時送，GitHub 版當你電腦沒開時的保底（我已把 GitHub 版守門判斷放寬，遲到也會照跑）。兩邊寄送內容一樣，訂閱者頂多同一天收到兩封，測試階段無妨；日後要避免重複，關掉其中一個的排程即可。

---

## 安裝步驟

### 前置：確認有 Python 3

打開「終端機」(Terminal)，貼上：

```bash
python3 --version
```

顯示 `Python 3.9` 以上就 OK。若說找不到，先裝 [Homebrew](https://brew.sh)，再 `brew install python`。

### 步驟 1：放置專案並執行安裝腳本

把整個 `portfolio-report-free` 資料夾放到你想要的位置（例如 `~/Documents/`），然後：

```bash
cd ~/Documents/portfolio-report-free      # 改成你放的實際路徑
bash setup_mac.sh
```

這會自動：建立獨立的 Python 環境、安裝套件、檢查中文字型、設定好 16:10 的排程。約 1–2 分鐘。

### 步驟 2：取得 Anthropic API 金鑰（給 AI 用）

1. 到 [console.anthropic.com](https://console.anthropic.com) 登入
2. **Settings** → **API keys** → **Create Key**
3. 名稱隨意（如 `daily-brief-mac`）
4. 複製那串 `sk-ant-...`（只會顯示一次，關掉就看不到了）

如果 GitHub Actions 版已經設過，直接用同一組即可，不必另外產生。

> ⚠️ 這是**付費** API，依用量計費。用預設的 Haiku 模型、單人使用約每月 US$2；
> 訂閱者到 30 人約 US$13。可在 Console 的 **Limits** 設每月上限當保險。

### 步驟 3：建立環境變數檔

```bash
cp .env.sh.example .env.sh
open -e .env.sh
```

在打開的編輯器裡填入四項，存檔：

- `GMAIL_ADDRESS`：你的寄件 Gmail
- `GMAIL_APP_PASSWORD`：Gmail 應用程式密碼 16 碼（Google 帳戶 → 安全性 → 兩步驟驗證 → 應用程式密碼）
- `ANTHROPIC_API_KEY`：剛複製的 `sk-ant-...`
- `SUBSCRIBERS_CSV_URL`：你的 Google 表單 CSV 網址（用表單收訂閱就填，否則留空用 subscribers.yaml）

這個檔含機密，已被 `.gitignore` 排除、不會上傳 GitHub。

### 步驟 4：自我檢查

```bash
.venv/bin/python check_env.py
```

會逐項顯示 ✅ / ❌。全部 ✅ 才往下走；有 ❌ 照提示修正（最常見是字型或環境變數）。

### 步驟 5：立即測試一次

```bash
bash run_now.sh
```

這會用測試模式（休市日也跑）實際產生 PDF 並寄信。約 5–10 分鐘。收到信、PDF 中文正常，就成功了。

### 步驟 6：完成

排程已在跑，之後每個週一~五 16:10 自動執行。**唯一要記得的事：那個時間點電腦要開機、連著網路、不能在睡眠。**

---

## 讓電腦準時醒來（選配但建議）

若 16:10 你的 Mac 常在睡眠，可設定它自動醒來：

系統設定 → 一般 → 這台 Mac 有「排程」的話用內建；或用終端機：

```bash
sudo pmset repeat wake MTWRF 16:08:00
```

這會讓 Mac 每個工作日 16:08 自動醒來（提前 2 分鐘），排程 16:10 就能準時跑。筆電需接著電源才保證生效。

---

## 日常操作

| 想做的事 | 指令 |
|---|---|
| 立即手動跑一次 | `bash run_now.sh` |
| 看最近執行紀錄 | `cat run.log` |
| 暫停排程 | `launchctl unload ~/Library/LaunchAgents/com.howard.dailybrief.plist` |
| 恢復排程 | `launchctl load ~/Library/LaunchAgents/com.howard.dailybrief.plist` |
| 確認排程在跑 | `launchctl list \| grep dailybrief` |

新增訂閱者跟線上版一樣——有人填表單就自動納入，或編輯 `subscribers.yaml`。

---

## 疑難排解

**PDF 中文變方框** → `brew install --cask font-noto-sans-cjk-tc` 後再跑一次。

**排程時間到了沒動靜** → 檢查三件事：(1) 電腦當時是否開機且非睡眠；(2) `launchctl list | grep dailybrief` 有沒有列出來；(3) `cat run.log` 看有無錯誤。launchd 最常見的坑是「觸發當下電腦在睡眠」——睡眠中不會執行，醒來後才補跑。

**run_now.sh 說找不到 .env.sh** → 你還沒做步驟 3，或檔名打錯（要是 `.env.sh` 不是 `.env.sh.example`）。

**AI 回應「API key 無效」** → 金鑰複製時漏字或已被撤銷，回步驟 2 重新產生一組。

**AI 回應 HTTP 402** → Anthropic 帳戶餘額用完，到 Console → Billing 儲值。
