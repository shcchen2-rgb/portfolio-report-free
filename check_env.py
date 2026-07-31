#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本機環境自我檢查——執行後告訴你每一項是否就緒，方便排查問題。
用法：bash run_now.sh 之前先跑這個確認環境
  .venv/bin/python check_env.py
"""
import os
import sys
import shutil
import subprocess

ok = "✅"
no = "❌"
warn = "⚠️ "
problems = []


def check(label, condition, detail_ok="", detail_fail="", fatal=True):
    if condition:
        print(f"{ok} {label}" + (f"：{detail_ok}" if detail_ok else ""))
    else:
        print(f"{no if fatal else warn}{label}" + (f"：{detail_fail}" if detail_fail else ""))
        if fatal:
            problems.append(label)


print("=" * 50)
print("每日報告 本機環境自我檢查")
print("=" * 50)

# 1) Python 版本
v = sys.version_info
check("Python 版本", v >= (3, 9),
      detail_ok=f"{v.major}.{v.minor}.{v.micro}",
      detail_fail=f"{v.major}.{v.minor}（需要 3.9 以上）")

# 2) 必要套件
for pkg in ["yfinance", "requests", "feedparser", "yaml", "markdown", "weasyprint"]:
    try:
        __import__(pkg)
        check(f"套件 {pkg}", True)
    except Exception as e:
        check(f"套件 {pkg}", False, detail_fail=str(e))

# 3) 中文字型（PDF 顯示中文的關鍵）
has_font = False
try:
    out = subprocess.run(["fc-list", ":lang=zh"], capture_output=True, text=True)
    has_font = bool(out.stdout.strip())
except Exception:
    pass
check("中文字型", has_font, fatal=False,
      detail_ok="系統有中文字型",
      detail_fail="未偵測到；PDF 中文可能變方框，執行 brew install --cask font-noto-sans-cjk-tc")

# 3b) PDF 引擎（WeasyPrint 需要 Homebrew 的 pango；缺了會在產 PDF 那步才爆）
try:
    from weasyprint import HTML  # noqa
    check("PDF 引擎", True, detail_ok="WeasyPrint 可正常載入")
except Exception as e:
    check("PDF 引擎", False,
          detail_fail=f"WeasyPrint 載入失敗（{str(e)[:60]}）— 執行 brew install pango，"
                      "並確認用 run_now.sh 執行而不是直接呼叫 python")

# 4) 環境變數
for var, hint in [("GMAIL_ADDRESS", "寄件信箱"),
                  ("GMAIL_APP_PASSWORD", "應用程式密碼"),
                  ("ANTHROPIC_API_KEY", "Anthropic API 金鑰")]:
    val = os.environ.get(var, "")
    filled = bool(val) and "xxxx" not in val and "your_email" not in val
    check(f"環境變數 {var}（{hint}）", filled,
          detail_fail="未設定或仍是範本預設值 — 檢查 .env.sh")

# 5) 訂閱來源檔案
csv_url = os.environ.get("SUBSCRIBERS_CSV_URL", "")
if csv_url:
    check("訂閱來源", True, detail_ok="使用 Google 表單 CSV")
else:
    check("訂閱來源", os.path.exists("subscribers.yaml"),
          detail_ok="使用 subscribers.yaml",
          detail_fail="未設表單網址，也找不到 subscribers.yaml")

# 6) AI 連線測試（實際打一次 Anthropic API，成本約 US$0.00001）
api_key = os.environ.get("ANTHROPIC_API_KEY", "")
if api_key and "xxxx" not in api_key:
    try:
        import anthropic
        import yaml as _yaml
        with open("config_multi.yaml", encoding="utf-8") as f:
            model = _yaml.safe_load(f)["ai"]["model"]
        anthropic.Anthropic().messages.create(
            model=model,
            max_tokens=5,
            messages=[{"role": "user", "content": "reply with OK"}],
        )
        check("AI 連線測試", True, detail_ok=f"{model} 回應正常")
    except anthropic.AuthenticationError:
        check("AI 連線測試", False, detail_fail="API key 無效或已撤銷")
    except anthropic.NotFoundError:
        check("AI 連線測試", False,
              detail_fail="找不到該模型，檢查 config_multi.yaml 的 model 名稱")
    except Exception as e:
        check("AI 連線測試", False, fatal=False, detail_fail=str(e)[:150])
else:
    check("AI 連線測試", False, detail_fail="ANTHROPIC_API_KEY 未設定，跳過")

print("=" * 50)
if problems:
    print(f"{no} 有 {len(problems)} 項需要處理：{', '.join(problems)}")
    print("   修正後再跑一次本檢查。")
    sys.exit(1)
else:
    print(f"{ok} 全部就緒！可以執行 bash run_now.sh 測試一次完整流程。")
