#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日股票觀察報告【多人訂閱版】
- 每個訂閱者：自己的 email、語言（zh / en / both）、觀察清單（上限 15 檔）
- AI 引擎：GitHub Models（免費）；同一檔股票同一語言只分析一次，所有訂閱者共用（省額度）
- 每人收到個人化 PDF（中文＝紅漲綠跌、英文＝綠漲紅跌）

環境變數：GITHUB_TOKEN（Actions 自動）、GMAIL_ADDRESS、GMAIL_APP_PASSWORD
測試：DRY_RUN=1（跳過 AI 與寄信）、FORCE=1（休市日強制執行）
"""

import os
import re
import sys
import time
import smtplib
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.utils import formataddr
from urllib.parse import quote
from zoneinfo import ZoneInfo

import yaml
import requests
import feedparser
import yfinance as yf
import markdown as md_lib
from weasyprint import HTML

# ------------------------------------------------------------
# 基本設定
# ------------------------------------------------------------
LA = ZoneInfo("America/Los_Angeles")
NOW_LA = dt.datetime.now(LA)
TODAY = NOW_LA.date()
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FORCE = os.environ.get("FORCE") == "1"

GH_MODELS_URL = "https://models.github.ai/inference/chat/completions"
MAX_TICKERS_PER_SUB = 15

INDEXES = [
    ("^GSPC", "S&P 500", "S&P 500"),
    ("^IXIC", "那斯達克", "Nasdaq"),
    ("^DJI", "道瓊工業", "Dow Jones"),
    ("^SOX", "費城半導體", "PHLX Semiconductor"),
    ("^VIX", "VIX 恐慌指數", "VIX"),
    ("^TNX", "美債10年殖利率(%)", "US 10Y Treasury Yield (%)"),
]

SECTORS = [
    ("XLK", "科技", "Technology"), ("SMH", "半導體", "Semiconductors"),
    ("XLC", "通訊服務", "Communication Svcs"), ("XLY", "非必需消費", "Consumer Discretionary"),
    ("XLP", "必需消費", "Consumer Staples"), ("XLF", "金融", "Financials"),
    ("XLV", "醫療保健", "Health Care"), ("XLI", "工業", "Industrials"),
    ("XLE", "能源", "Energy"), ("XLB", "原物料", "Materials"),
    ("XLU", "公用事業", "Utilities"), ("XLRE", "房地產", "Real Estate"),
]

# ------------------------------------------------------------
# 介面文字（i18n）
# ------------------------------------------------------------
L = {
    "zh": {
        "title": "每日股票觀察報告",
        "prepared_for": "專屬報告：",
        "sec_market": "一、大盤與總經",
        "sec_watchlist": "二、觀察清單總覽",
        "sec_stocks": "三、個股漲跌原因分析",
        "sec_synth": "四、綜合觀察",
        "th_index": "指數", "th_close": "收盤", "th_chg": "漲跌幅",
        "th_etf": "ETF", "th_sector": "類股",
        "th_ticker": "代號", "th_date": "資料日期", "th_vol": "量能",
        "close_label": "收盤",
        "failed_ticker": "無法取得此代號的價格資料，請確認代號（美股直接打代號、台股加 .TW / 上櫃 .TWO）。",
        "subtitle": "資料來源：Yahoo Finance、Google News、GitHub Models AI（未經網路查證）",
        "tz_note": "（洛杉磯時間）",
        "disclaimer": "本報告由自動化系統以 AI 產生（僅依據新聞標題與價量數據推論，未經人工審核與網路查證），僅供一般資訊參考，不構成投資建議或任何證券之買賣邀約。資料可能延遲或有誤，投資決策請自行判斷並以官方來源為準。",
        "email_intro": "您的每日股票觀察報告已產生（詳細分析請見附件 PDF）：",
        "email_unsub": "本服務為免費測試版，僅供參考、不構成投資建議。若不想再收到，直接回覆此信告知即可取消訂閱。",
        "fmt_driver": "**主要原因**：…\n**產業鏈觀察**：…\n**與大盤/類股的關係**：…\n**後續觀察**：…",
    },
    "en": {
        "title": "Daily Stock Watchlist Brief",
        "prepared_for": "Prepared for: ",
        "sec_market": "1. Market & Macro",
        "sec_watchlist": "2. Watchlist Overview",
        "sec_stocks": "3. Why Each Stock Moved",
        "sec_synth": "4. Cross-Stock Themes",
        "th_index": "Index", "th_close": "Close", "th_chg": "Change",
        "th_etf": "ETF", "th_sector": "Sector",
        "th_ticker": "Ticker", "th_date": "Data date", "th_vol": "Volume",
        "close_label": "Close",
        "failed_ticker": "Price data unavailable — please double-check the ticker (US tickers as-is; Taiwan listed = .TW, OTC = .TWO).",
        "subtitle": "Sources: Yahoo Finance, Google News, GitHub Models AI (not web-verified)",
        "tz_note": " (Los Angeles time)",
        "disclaimer": "This report is generated automatically by AI, based solely on news headlines and price/volume data, without human review or web verification. It is provided for general informational purposes only and does not constitute investment advice or a solicitation to buy or sell any security. Data may be delayed or inaccurate; please make your own decisions and verify with official sources.",
        "email_intro": "Your daily stock watchlist brief is ready (see the attached PDF for full analysis):",
        "email_unsub": "This is a free beta service, for information only — not investment advice. Reply to this email anytime to unsubscribe.",
        "fmt_driver": "**Key driver**: …\n**Industry-chain view**: …\n**Versus market & sector**: …\n**What to watch**: …",
    },
}


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def is_tw(ticker):
    t = ticker.upper()
    return t.endswith(".TW") or t.endswith(".TWO")


def norm_lang(v):
    v = str(v or "").strip().lower()
    if v in ("zh", "中文", "chinese", "繁體中文", "tc"):
        return ["zh"]
    if v in ("en", "english", "英文"):
        return ["en"]
    if v in ("both", "全部", "兩者", "都要", "all", "zh+en"):
        return ["zh", "en"]
    return ["zh"]  # 預設中文


def norm_tickers(v):
    if isinstance(v, str):
        parts = re.split(r"[,\s、]+", v)
    else:
        parts = list(v or [])
    out = []
    for p in parts:
        p = str(p).strip().upper()
        if p and p not in out:
            out.append(p)
    return out[:MAX_TICKERS_PER_SUB]


# ------------------------------------------------------------
# 市場資料（yfinance，含重試）
# ------------------------------------------------------------
def fetch_history(ticker, period="1mo", retries=4):
    for i in range(retries):
        try:
            hist = yf.Ticker(ticker).history(period=period)
            if hist is not None and len(hist) >= 2:
                return hist
            print(f"  [警告] {ticker} 回傳資料不足（第 {i + 1} 次）")
        except Exception as e:
            print(f"  [警告] {ticker} 抓取失敗（第 {i + 1} 次）：{e}")
        time.sleep(4 * (i + 1))
    return None


def snapshot(ticker):
    hist = fetch_history(ticker)
    if hist is None:
        return None
    last, prev = hist.iloc[-1], hist.iloc[-2]
    if not prev["Close"]:
        return None
    change_pct = (last["Close"] - prev["Close"]) / prev["Close"] * 100
    vol = float(last.get("Volume", 0) or 0)
    vol_ratio = None
    if len(hist) > 6:
        avg_vol = float(hist["Volume"].iloc[-21:-1].mean())
        if avg_vol > 0:
            vol_ratio = vol / avg_vol
    return {
        "ticker": ticker,
        "date": hist.index[-1].date().isoformat(),
        "close": float(last["Close"]),
        "prev_close": float(prev["Close"]),
        "change_pct": float(change_pct),
        "vol_ratio": vol_ratio,
    }


def market_was_open_today():
    spy = fetch_history("SPY", period="5d")
    if spy is None:
        return True
    return spy.index[-1].date() == TODAY


# ------------------------------------------------------------
# 新聞（Google News RSS，依股票只抓一次、所有訂閱者共用）
# ------------------------------------------------------------
def google_news(query, lang="en", limit=12):
    if lang == "zh":
        url = (f"https://news.google.com/rss/search?q={quote(query)}"
               f"&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    else:
        url = (f"https://news.google.com/rss/search?q={quote(query)}"
               f"&hl=en-US&gl=US&ceid=US:en")
    try:
        feed = feedparser.parse(url)
        items = []
        for e in feed.entries[:limit]:
            title = e.get("title", "").strip()
            published = e.get("published", "")
            src = ""
            try:
                src = e.source.title
            except Exception:
                pass
            items.append(f"- {title} ({src}, {published})")
        return items
    except Exception as ex:
        print(f"  [警告] 新聞抓取失敗 {query}：{ex}")
        return []


def news_for_ticker(t):
    if is_tw(t):
        code = t.split(".")[0]
        items = google_news(f"{code} 股價 when:1d", lang="zh")
        if len(items) < 4:
            items = google_news(f"{code} when:2d", lang="zh")
    else:
        items = google_news(f"{t} stock when:1d", lang="en")
        if len(items) < 4:
            items = google_news(f"{t} when:2d", lang="en")
    return items


# ------------------------------------------------------------
# AI（GitHub Models，全域額度預算）
# ------------------------------------------------------------
_quota_exhausted = False
_ai_calls_used = 0
QUOTA_MSG = {
    "zh": "（今日 AI 免費額度已用完，此段分析略過）",
    "en": "(Daily free AI quota exhausted; this section was skipped.)",
}


def call_ai(cfg, system, user, lang, max_tokens=1200):
    global _quota_exhausted, _ai_calls_used
    budget = int(cfg["ai"].get("max_total_ai_calls", 140))
    if _quota_exhausted or _ai_calls_used >= budget:
        return QUOTA_MSG[lang]

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return "(Missing GITHUB_TOKEN — check workflow permissions: models: read)"

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "model": cfg["ai"]["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": float(cfg["ai"].get("temperature", 0.4)),
    }

    for attempt in range(4):
        try:
            r = requests.post(GH_MODELS_URL, headers=headers, json=payload, timeout=180)
            if r.status_code == 200:
                _ai_calls_used += 1
                return r.json()["choices"][0]["message"]["content"].strip()
            if r.status_code == 429:
                try:
                    wait = int(r.headers.get("Retry-After", "30"))
                except ValueError:
                    wait = 30
                if wait > 300:
                    print(f"  [限流] 當日免費額度用完（需等 {wait} 秒）")
                    _quota_exhausted = True
                    return QUOTA_MSG[lang]
                print(f"  [限流] 429，等待 {wait} 秒重試（第 {attempt + 1} 次）")
                time.sleep(wait + 2)
                continue
            if r.status_code in (401, 403):
                print(f"  [錯誤] 權限不足 HTTP {r.status_code}：{r.text[:300]}")
                return "(GitHub Models permission error — check permissions: models: read)"
            print(f"  [警告] AI 呼叫失敗 HTTP {r.status_code}：{r.text[:300]}")
            time.sleep(10 * (attempt + 1))
        except Exception as e:
            print(f"  [警告] AI 呼叫錯誤：{e}")
            time.sleep(10 * (attempt + 1))
    return "(AI analysis failed — see Actions log.)"


MARKET_SYSTEM = {
    "zh": ("你是一位資深的總體經濟與美股市場分析師，撰寫繁體中文日報。"
           "你沒有上網查證的能力，只能依據提供的收盤數據與新聞標題分析。"
           "要具體引用提供的資料；資料中沒有的事情不要斷言，寧可保守。"),
    "en": ("You are a senior macro and US equity market analyst writing a daily brief in English. "
           "You have NO web access; rely only on the closing data and news headlines provided. "
           "Be specific and ground every claim in the provided material; when uncertain, stay conservative."),
}

STOCK_SYSTEM = {
    "zh": ("你是一位資深買方產業分析師，任務是解讀「這檔股票今天為什麼漲或跌」。"
           "你沒有上網查證的能力，只能依據提供的價量數據、新聞標題與大盤背景推論。要求：\n"
           "1. 具體：從提供的新聞標題中找出實際催化劑（財報、升降評、訂單、產品消息等）。\n"
           "2. 產業鏈視角：依該公司在產業結構中的位置分析（例如半導體區分 IC 設計、代工、設備、"
           "記憶體、EDA，並考慮上下游與主要客戶連動；軟體看訂閱與 AI 商業化；金融看利率環境）。\n"
           "3. 區分 alpha 與 beta：比較個股與大盤及所屬類股 ETF 的表現，判斷是自身消息、"
           "跟隨類股/大盤、還是被其他權值股帶動。\n"
           "4. 誠實原則（最重要）：提供的標題中若無明確個股催化劑，直接說「今日波動主要反映"
           "大盤/類股走勢，提供的新聞中無重大個股消息」，絕不編造。\n"
           "5. 繁體中文 markdown，200–400 字。"),
    "en": ("You are a senior buy-side industry analyst. Task: explain why this stock moved today. "
           "You have NO web access; rely only on the price/volume data, headlines, and market context provided. Rules:\n"
           "1. Be specific: identify actual catalysts from the provided headlines (earnings, guidance, "
           "analyst rating or price-target changes, orders, product or policy news).\n"
           "2. Industry-structure perspective: analyze according to the company's position in its industry "
           "(for semiconductors distinguish fabless design, foundry, equipment, memory, EDA, plus supply-chain "
           "and key-customer linkages; for software, subscription/cloud metrics and AI monetization; for banks, rates).\n"
           "3. Separate alpha from beta: compare the stock's move with the broad market and its sector ETF to judge "
           "whether it was company-specific news, sector/market drift, or spillover from other mega-caps.\n"
           "4. Honesty rule (most important): if the headlines contain no clear company-specific catalyst, say plainly "
           "that today's move mainly tracked the market/sector and no major company news was found. Never fabricate.\n"
           "5. English markdown, 150–300 words."),
}

SYNTH_SYSTEM = {
    "zh": ("你是一位資深投資策略分析師，為讀者的觀察清單做每日總結。你沒有上網能力，"
           "只能依據提供的資料歸納。重點是跨個股的共同主題（同一供應鏈連動、同一總經因素、"
           "資金輪動）。提到後續關注時只能依據提供資料中出現的資訊或一般性週期（財報季、"
           "FOMC 例會），不要虛構具體日期。繁體中文 markdown。"),
    "en": ("You are a senior investment strategist summarizing the reader's watchlist for the day. "
           "No web access; use only the provided material. Focus on cross-stock themes (shared supply chains, "
           "common macro drivers, rotation). For forward-looking items, mention only what appears in the provided "
           "material or generic cycles (earnings season, FOMC schedule); never invent specific dates. English markdown."),
}


def analyze_market(cfg, lang, index_lines, sector_lines, market_news):
    if lang == "zh":
        user = f"""今天是 {TODAY}（洛杉磯時間），以下是今日收盤數據。

【主要指數】
{index_lines}

【類股 ETF】
{sector_lines}

【市場焦點新聞標題（近 24 小時）】
{market_news}

請寫「今日大盤與總經摘要」（約 250–400 字）：1) 整體表現與主要驅動因素（依據上面標題，要具體）
2) 類股輪動與可能原因 3) 殖利率與 VIX 反映的市場情緒。直接輸出 markdown 內文，不要大標題。"""
    else:
        user = f"""Today is {TODAY} (Los Angeles time). Closing data below.

[Major indexes]
{index_lines}

[Sector ETFs]
{sector_lines}

[Market headlines, past 24h]
{market_news}

Write a "Market & Macro" daily summary (180–300 words): 1) overall performance and key drivers
(be specific, grounded in the headlines above) 2) sector rotation and likely reasons
3) what yields and the VIX say about sentiment. Output markdown body only, no big title."""
    return call_ai(cfg, MARKET_SYSTEM[lang], user, lang, max_tokens=1200)


def analyze_stock(cfg, lang, snap, market_overview, news_items):
    news_text = "\n".join(news_items) if news_items else (
        "（近兩日 RSS 未抓到相關標題）" if lang == "zh" else "(No recent headlines found via RSS.)")
    vol_txt = f"{snap['vol_ratio']:.2f}x" if snap.get("vol_ratio") else "n/a"
    if lang == "zh":
        user = f"""股票：{snap['ticker']}
今日數據（資料日期 {snap['date']}）：收盤 {snap['close']:.2f}，漲跌 {snap['change_pct']:+.2f}%，量能 {vol_txt}（vs 20 日均量）

今日大盤背景摘要（供判斷 beta 用）：
{market_overview[:700]}

相關新聞標題：
{news_text}

請分析這檔股票今天漲跌的原因，輸出格式（粗體小標＋內文）：
{L['zh']['fmt_driver']}"""
    else:
        user = f"""Stock: {snap['ticker']}
Today's data (as of {snap['date']}): close {snap['close']:.2f}, change {snap['change_pct']:+.2f}%, volume {vol_txt} vs 20-day avg

Market backdrop (to judge beta):
{market_overview[:700]}

Related headlines:
{news_text}

Explain why this stock moved today. Output format (bold labels + text):
{L['en']['fmt_driver']}"""
    return call_ai(cfg, STOCK_SYSTEM[lang], user, lang, max_tokens=1200)


def synthesize(cfg, lang, sub_name, rows, analyses):
    parts = []
    for r in rows:
        a = analyses.get((r["ticker"], lang), "")
        parts.append(f"### {r['ticker']} ({r['change_pct']:+.2f}%)\n{a[:350]}")
    text = "\n\n".join(parts)
    if lang == "zh":
        user = f"""以下是 {sub_name} 的觀察清單今日（{TODAY}）資料與各檔分析節錄：

{text}

請寫「綜合觀察」（約 250–400 字）：1) 2–4 個跨個股的共同主題（供應鏈連動、共同總經因素、輪動方向）
2) 這份清單今日整體表現一句話定調 3) 後續值得留意的方向（僅依據上面資料，不虛構日期）。
直接輸出 markdown 內文。"""
    else:
        user = f"""Below are today's ({TODAY}) results and per-stock analysis excerpts for {sub_name}'s watchlist:

{text}

Write "Cross-Stock Themes" (180–300 words): 1) 2–4 themes cutting across these stocks
(supply-chain linkages, shared macro drivers, rotation) 2) a one-line verdict on the list's overall day
3) what to watch next (only from the material above; no invented dates). Markdown body only."""
    return call_ai(cfg, SYNTH_SYSTEM[lang], user, lang, max_tokens=1200)


# ------------------------------------------------------------
# HTML / PDF
# ------------------------------------------------------------
CSS_TEMPLATE = """
@page { size: A4; margin: 1.6cm 1.5cm;
  @bottom-center { content: counter(page) " / " counter(pages);
    font-size: 8pt; color: #999; font-family: 'Noto Sans CJK TC', sans-serif; } }
body { font-family: 'Noto Sans CJK TC', 'Noto Sans TC', 'PingFang TC', sans-serif;
  font-size: 10.5pt; line-height: 1.6; color: #1f2937; margin: 0; }
.header { border-bottom: 3px solid #166534; padding-bottom: 10px; margin-bottom: 18px; }
h1 { font-size: 19pt; color: #166534; margin: 0 0 4px 0; }
.subtitle { color: #6b7280; font-size: 9.5pt; }
h2 { font-size: 13.5pt; color: #166534; border-left: 5px solid #166534;
  padding-left: 9px; margin: 22px 0 10px 0; page-break-after: avoid; }
h3 { font-size: 11.5pt; color: #111827; margin: 16px 0 6px 0;
  padding: 5px 8px; background: #f0fdf4; border-radius: 4px; page-break-after: avoid; }
table { width: 100%; border-collapse: collapse; font-size: 9pt; margin: 8px 0 14px 0; }
th { background: #166534; color: #fff; padding: 5px 7px; text-align: left; font-weight: 600; }
td { border-bottom: 1px solid #e5e7eb; padding: 4px 7px; }
tr:nth-child(even) td { background: #f9fafb; }
.up { color: __UP_COLOR__; font-weight: 700; }
.down { color: __DOWN_COLOR__; font-weight: 700; }
.flat { color: #6b7280; }
.stock-block { page-break-inside: avoid; margin-bottom: 6px; }
.stock-block p { margin: 5px 0; }
.disclaimer { margin-top: 22px; padding-top: 8px; border-top: 1px solid #e5e7eb;
  font-size: 8pt; color: #9ca3af; }
strong { color: #111827; }
"""


def build_css(lang):
    # 中文＝紅漲綠跌（台灣慣例）；英文＝綠漲紅跌（美國慣例）
    if lang == "zh":
        up, down = "#dc2626", "#15803d"
    else:
        up, down = "#15803d", "#dc2626"
    return CSS_TEMPLATE.replace("__UP_COLOR__", up).replace("__DOWN_COLOR__", down)


def pct_html(p):
    if p is None:
        return '<span class="flat">—</span>'
    cls = "up" if p > 0 else ("down" if p < 0 else "flat")
    return f'<span class="{cls}">{p:+.2f}%</span>'


def md_to_html(text):
    return md_lib.markdown(text or "", extensions=["extra"])


def build_report_html(lang, sub, rows, failed, market_overview,
                      analyses, synthesis, index_snaps, sector_snaps):
    t = L[lang]
    css = build_css(lang)

    idx_rows = "".join(
        f"<tr><td>{n[lang]}</td><td>{s['close']:,.2f}</td><td>{pct_html(s['change_pct'])}</td></tr>"
        for n, s in index_snaps
    )
    sec_sorted = sorted(sector_snaps, key=lambda x: x[1]["change_pct"], reverse=True)
    sec_rows = "".join(
        f"<tr><td>{s['ticker']}</td><td>{n[lang]}</td><td>{pct_html(s['change_pct'])}</td></tr>"
        for n, s in sec_sorted
    )

    wl_rows = ""
    for r in rows:
        vol_txt = f"{r['vol_ratio']:.2f}x" if r.get("vol_ratio") else "—"
        wl_rows += (
            f"<tr><td><b>{r['ticker']}</b></td><td>{r['date']}</td>"
            f"<td>{r['close']:,.2f}</td><td>{pct_html(r['change_pct'])}</td>"
            f"<td>{vol_txt}</td></tr>"
        )
    for ft in failed:
        wl_rows += f"<tr><td><b>{ft}</b></td><td colspan='4'>{t['failed_ticker']}</td></tr>"

    stocks_html = ""
    for r in rows:
        a = analyses.get((r["ticker"], lang), "")
        stocks_html += (
            f"<div class='stock-block'>"
            f"<h3>{r['ticker']}　{pct_html(r['change_pct'])}"
            f"（{t['close_label']} {r['close']:,.2f}）</h3>"
            f"{md_to_html(a)}</div>"
        )

    generated = NOW_LA.strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html lang="{ 'zh-Hant' if lang == 'zh' else 'en' }"><head><meta charset="utf-8"><style>{css}</style></head>
<body>
<div class="header">
  <h1>{t['title']}</h1>
  <div class="subtitle">{t['prepared_for']}{sub['name']}｜{TODAY}{t['tz_note']}｜{generated} PT｜{t['subtitle']}</div>
</div>

<h2>{t['sec_market']}</h2>
{md_to_html(market_overview)}
<table><tr><th>{t['th_index']}</th><th>{t['th_close']}</th><th>{t['th_chg']}</th></tr>{idx_rows}</table>
<table><tr><th>{t['th_etf']}</th><th>{t['th_sector']}</th><th>{t['th_chg']}</th></tr>{sec_rows}</table>

<h2>{t['sec_watchlist']}</h2>
<table>
<tr><th>{t['th_ticker']}</th><th>{t['th_date']}</th><th>{t['th_close']}</th><th>{t['th_chg']}</th><th>{t['th_vol']}</th></tr>
{wl_rows}
</table>

<h2>{t['sec_stocks']}</h2>
{stocks_html}

<h2>{t['sec_synth']}</h2>
{md_to_html(synthesis)}

<div class="disclaimer">{t['disclaimer']}</div>
</body></html>"""
    return html


def safe_filename(name):
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", str(name)).strip("_")
    return s or "subscriber"


# ------------------------------------------------------------
# Email
# ------------------------------------------------------------
def send_all_emails(cfg, deliveries):
    """deliveries: list of dicts {sub, pdfs: {lang: path}, rows}"""
    addr = os.environ["GMAIL_ADDRESS"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    sender_name = cfg.get("email", {}).get("sender_name", "Daily Stock Brief")
    ok = fail = 0

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(addr, pwd)
        for d in deliveries:
            sub = d["sub"]
            langs = d["langs"]
            main_lang = langs[0]
            t = L[main_lang]
            if len(langs) == 2:
                subject = f"📈 每日股票觀察報告 / Daily Stock Brief {TODAY}"
            elif main_lang == "zh":
                subject = f"📈 每日股票觀察報告 {TODAY}"
            else:
                subject = f"📈 Daily Stock Watchlist Brief {TODAY}"

            wl = "".join(
                f"<tr><td>{r['ticker']}</td><td style='text-align:right'>{r['close']:,.2f}</td>"
                f"<td style='text-align:right'>{r['change_pct']:+.2f}%</td></tr>"
                for r in d["rows"]
            )
            intro = "<br>".join(L[lg]["email_intro"] for lg in langs)
            unsub = "<br>".join(L[lg]["email_unsub"] for lg in langs)
            body = f"""<div style="font-family:sans-serif;font-size:14px">
<p>{sub['name']}，</p><p>{intro}</p>
<table border="0" cellpadding="4" style="border-collapse:collapse;font-size:13px">
<tr style="background:#f0fdf4"><th align="left">Ticker</th><th>Close</th><th>Chg</th></tr>
{wl}
</table>
<p style="color:#6b7280;font-size:12px">{unsub}</p>
</div>"""

            msg = MIMEMultipart()
            msg["From"] = formataddr((sender_name, addr))
            msg["To"] = sub["email"]
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "html", "utf-8"))
            for lg in langs:
                p = d["pdfs"].get(lg)
                if p and os.path.exists(p):
                    with open(p, "rb") as f:
                        part = MIMEApplication(f.read(), _subtype="pdf")
                    part.add_header("Content-Disposition", "attachment",
                                    filename=os.path.basename(p))
                    msg.attach(part)
            try:
                s.send_message(msg)
                ok += 1
                print(f"  已寄送：{sub['name']} <{sub['email']}>（{'+'.join(langs)}）")
            except Exception as e:
                fail += 1
                print(f"  [錯誤] 寄送失敗 {sub['email']}：{e}")
            time.sleep(2)
    print(f"寄送完成：成功 {ok}、失敗 {fail}")


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
def main():
    cfg = load_yaml("config_multi.yaml")
    raw_subs = load_yaml("subscribers.yaml")["subscribers"]
    include_synth = bool(cfg["ai"].get("include_synthesis", True))
    call_gap = float(cfg["ai"].get("seconds_between_calls", 5))

    # 整理訂閱者
    subs = []
    for srec in raw_subs:
        langs = norm_lang(srec.get("language"))
        tickers = norm_tickers(srec.get("tickers"))
        if not srec.get("email") or not tickers:
            print(f"  [警告] 訂閱者資料不完整，跳過：{srec}")
            continue
        subs.append({"name": srec.get("name", srec["email"].split("@")[0]),
                     "email": srec["email"].strip(),
                     "langs": langs, "tickers": tickers})

    langs_needed = sorted({lg for s in subs for lg in s["langs"]})
    unique_tickers = sorted({tk for s in subs for tk in s["tickers"]})
    pair_count = len({(tk, lg) for s in subs for tk in s["tickers"] for lg in s["langs"]})
    synth_count = sum(len(s["langs"]) for s in subs) if include_synth else 0
    est_calls = len(langs_needed) + pair_count + synth_count

    print(f"=== {TODAY} 每日股票觀察報告（多人訂閱版）===")
    print(f"訂閱者 {len(subs)} 人｜語言 {langs_needed}｜不重複股票 {len(unique_tickers)} 檔")
    print(f"預估 AI 呼叫數：{est_calls}（免費額度約 150 次/天，全帳號共用）")
    if est_calls > int(cfg["ai"].get("max_total_ai_calls", 140)):
        print("  [警告] 預估用量超過預算上限，超出的部分會顯示「額度已用完」佔位文字")

    if not subs:
        print("沒有有效訂閱者，結束。")
        return
    if not FORCE and not market_was_open_today():
        print("今日美股休市（週末或假日），跳過執行。")
        return

    # 1) 指數與類股（共用）
    print("抓取指數與類股…")
    index_snaps, sector_snaps = [], []
    for tkr, zh, en in INDEXES:
        s = snapshot(tkr)
        if s:
            index_snaps.append(({"zh": zh, "en": en}, s))
        time.sleep(1)
    if any(is_tw(tk) for tk in unique_tickers):
        s = snapshot("^TWII")
        if s:
            index_snaps.append(({"zh": "台股加權指數", "en": "Taiwan TAIEX"}, s))
    for tkr, zh, en in SECTORS:
        s = snapshot(tkr)
        if s:
            sector_snaps.append(({"zh": zh, "en": en}, s))
        time.sleep(1)

    # 2) 個股價量（每檔只抓一次）
    print(f"抓取 {len(unique_tickers)} 檔個股…")
    snaps, failed_tickers = {}, set()
    for tk in unique_tickers:
        s = snapshot(tk)
        time.sleep(1)
        if s:
            snaps[tk] = s
        else:
            failed_tickers.add(tk)
            print(f"  [警告] {tk} 無法取得價格")

    # 3) 新聞（每檔只抓一次）
    print("抓取新聞…")
    news_cache = {tk: news_for_ticker(tk) for tk in snaps}
    market_news = "\n".join(google_news("stock market today when:1d", limit=10)) or "(none)"

    # 4) AI：大盤摘要（每語言一次）＋ 個股分析（每檔每語言一次，全員共用）
    overview, analyses = {}, {}
    if DRY_RUN:
        for lg in langs_needed:
            overview[lg] = f"(DRY_RUN placeholder market overview / {lg})"
        for tk in snaps:
            for lg in langs_needed:
                analyses[(tk, lg)] = f"**DRY_RUN** placeholder analysis for {tk} ({lg})."
    else:
        for lg in langs_needed:
            print(f"AI：大盤摘要（{lg}）…")
            idx_lines = "\n".join(f"- {n[lg]}: {s['close']:,.2f} ({s['change_pct']:+.2f}%)"
                                  for n, s in index_snaps)
            sec_lines = "\n".join(f"- {s['ticker']} {n[lg]}: {s['change_pct']:+.2f}%"
                                  for n, s in sector_snaps)
            overview[lg] = analyze_market(cfg, lg, idx_lines, sec_lines, market_news)
            time.sleep(call_gap)
        need_pairs = sorted({(tk, lg) for s in subs for tk in s["tickers"]
                             for lg in s["langs"] if tk in snaps})
        for tk, lg in need_pairs:
            print(f"AI：分析 {tk}（{lg}）…")
            analyses[(tk, lg)] = analyze_stock(cfg, lg, snaps[tk],
                                               overview[lg], news_cache.get(tk, []))
            time.sleep(call_gap)

    # 5) 每位訂閱者：組報告 → PDF →（各語言）
    os.makedirs("output", exist_ok=True)
    deliveries = []
    for sub in subs:
        rows = [snaps[tk] for tk in sub["tickers"] if tk in snaps]
        failed = [tk for tk in sub["tickers"] if tk in failed_tickers]
        pdfs = {}
        for lg in sub["langs"]:
            if DRY_RUN:
                synth = f"(DRY_RUN placeholder synthesis / {lg})"
            elif include_synth and rows:
                print(f"AI：綜合觀察 {sub['name']}（{lg}）…")
                synth = synthesize(cfg, lg, sub["name"], rows, analyses)
                time.sleep(call_gap)
            else:
                synth = ""
            html = build_report_html(lg, sub, rows, failed, overview.get(lg, ""),
                                     analyses, synth, index_snaps, sector_snaps)
            path = f"output/brief_{safe_filename(sub['name'])}_{lg}_{TODAY}.pdf"
            HTML(string=html).write_pdf(path)
            pdfs[lg] = path
            print(f"  PDF：{path}（{os.path.getsize(path) / 1024:.0f} KB）")
        deliveries.append({"sub": sub, "langs": sub["langs"],
                           "pdfs": pdfs, "rows": rows})

    print(f"AI 實際用量：{_ai_calls_used} 次")

    # 6) 寄信
    if DRY_RUN:
        print("DRY_RUN：跳過寄信。")
        return
    send_all_emails(cfg, deliveries)
    print("完成。")


if __name__ == "__main__":
    main()
