#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日股票觀察報告【多人訂閱版】
- 每個訂閱者：自己的 email、語言（zh / en / both）、觀察清單（上限 15 檔）
- AI 引擎：Anthropic API；同一檔股票同一語言只分析一次，所有訂閱者共用（省成本）
- 每人收到個人化 PDF（中文＝紅漲綠跌、英文＝綠漲紅跌）

環境變數：ANTHROPIC_API_KEY、GMAIL_ADDRESS、GMAIL_APP_PASSWORD
測試：DRY_RUN=1（跳過 AI 與寄信）、FORCE=1（休市日強制執行）
"""

import os
import re
import io
import csv
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
import anthropic
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
        "subtitle": "資料來源：Yahoo Finance（市場數據）、Google News（新聞來源）",
        "disclosure_title": "方法論與重要聲明",
        "tz_note": "（洛杉磯時間）",
        "disclaimer": "本報告以量化市場數據（Yahoo Finance）與公開新聞來源自動彙整產生，分析內容經演算法生成、未經人工覆核，資料可能延遲或有誤。所有結論之依據皆已標註來源編號，敬請自行查證原文。本報告為一般性資訊，不構成投資建議、亦非任何證券之買賣邀約，不考量個別讀者之財務狀況或投資目標。投資決策請自行判斷並諮詢合格專業人士。",
        "email_intro": "您的每日股票觀察報告已產生（詳細分析請見附件 PDF）：",
        "email_unsub": "本報告為一般性資訊，不構成投資建議；完整聲明請見附件末頁。如需取消訂閱，直接回覆此信告知即可。",
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
        "subtitle": "Sources: Yahoo Finance (market data), Google News (headlines)",
        "disclosure_title": "Methodology & Disclosures",
        "tz_note": " (Los Angeles time)",
        "disclaimer": "This report is compiled automatically from quantitative market data (Yahoo Finance) and public news sources; the analysis is algorithmically generated and not reviewed by a human. Data may be delayed or inaccurate. Every conclusion is tagged to a numbered source — readers are encouraged to verify the originals. This is general information only, not investment advice or an offer to buy or sell any security, and does not consider any individual's financial situation or objectives. Please make your own decisions and consult a qualified professional.",
        "email_intro": "Your daily stock watchlist brief is ready (see the attached PDF for full analysis):",
        "email_unsub": "General information only — not investment advice. Full disclosures appear on the final page of the attached report. Reply to this email anytime to unsubscribe.",
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
    """把表單的自由文字答案轉成 ['zh'] / ['en'] / ['zh','en']
    可接受：'English 英文版'、'Mandarin Chinese 中文版'、'zh'、'both'、'中英文都要' 等"""
    s = str(v or "").strip().lower()
    if not s:
        return ["zh"]
    if s in ("zh", "en"):
        return [s]
    if any(k in s for k in ("both", "全部", "兩者", "都要", "兩份", "中英", "zh+en", "all")):
        return ["zh", "en"]
    has_zh = any(k in s for k in ("中文", "繁", "chinese", "mandarin", "中"))
    has_en = any(k in s for k in ("english", "英文", "英"))
    if has_zh and has_en:
        return ["zh", "en"]
    if has_en:
        return ["en"]
    if has_zh:
        return ["zh"]
    return ["zh"]  # 預設中文


def norm_tickers(v):
    """接受清單或字串；容錯全形逗號、頓號、$ 前綴、多餘空白"""
    if isinstance(v, str):
        parts = re.split(r"[,\s、，；;/|]+", v)
    else:
        parts = list(v or [])
    out = []
    for p in parts:
        p = str(p).strip().upper().lstrip("$").strip(".")
        if p and p not in out:
            out.append(p)
    return out[:MAX_TICKERS_PER_SUB]


def valid_email(e):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", str(e or "").strip()))


# ------------------------------------------------------------
# 訂閱者來源：Google 表單回應試算表（CSV）→ 失敗時退回 subscribers.yaml
# ------------------------------------------------------------
FIELD_KEYS = [
    ("email", ("email", "e-mail", "信箱", "郵件", "郵箱", "電子郵件")),
    ("language", ("language", "語言", "lang", "版本")),
    ("tickers", ("ticker", "stock", "symbol", "股票", "代號", "清單", "觀察", "watchlist")),
    ("name", ("name", "姓名", "稱呼", "名字", "call you")),
]


def to_csv_url(url):
    """把各種 Google 試算表網址轉成可直接下載的 CSV 連結"""
    url = (url or "").strip()
    if not url or "output=csv" in url or "format=csv" in url:
        return url
    m = re.search(r"/spreadsheets/d/e/([^/]+)/pub", url)
    if m:  # 已發布到網路的網址
        base = url.split("?")[0].replace("/pubhtml", "/pub")
        gid = re.search(r"gid=(\d+)", url)
        q = "output=csv" + (f"&gid={gid.group(1)}&single=true" if gid else "")
        return f"{base}?{q}"
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if m:  # 一般試算表網址（需設為「知道連結的人可檢視」）
        gid = re.search(r"gid=(\d+)", url)
        g = gid.group(1) if gid else "0"
        return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}"
                f"/export?format=csv&gid={g}")
    return url


def map_columns(headers):
    """把表單的問題標題對應到欄位；同一欄只會被指派一次"""
    mapping = {}
    used = set()
    for field, keys in FIELD_KEYS:
        for h in headers:
            if h in used or not h:
                continue
            hl = re.sub(r"\s+", "", str(h)).lower()
            if any(k.replace(" ", "") in hl for k in keys):
                mapping.setdefault(field, []).append(h)
                used.add(h)
    return mapping


def parse_subscribers_csv(text):
    """解析表單回應 CSV；同一 email 重複填寫時以最後一次為準"""
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    cols = map_columns(headers)
    if "email" not in cols or "tickers" not in cols:
        raise ValueError(f"表單欄位對應失敗，讀到的標題為：{headers}")

    def pick(row, field):
        for h in cols.get(field, []):
            v = (row.get(h) or "").strip()
            if v:
                return v
        return ""

    by_email = {}
    for row in reader:  # 表單回應依時間排序，後填的會覆蓋先填的
        email = pick(row, "email")
        if not valid_email(email):
            if email:
                print(f"  [略過] email 格式不正確：{email}")
            continue
        tickers = norm_tickers(pick(row, "tickers"))
        if not tickers:
            print(f"  [略過] {email} 未填股票代號")
            continue
        name = pick(row, "name") or email.split("@")[0]
        by_email[email.lower()] = {
            "name": name,
            "email": email,
            "language": pick(row, "language"),
            "tickers": tickers,
        }
    return list(by_email.values())


def load_subscribers():
    """優先讀 Google 表單（環境變數 SUBSCRIBERS_CSV_URL），失敗則退回 subscribers.yaml"""
    url = os.environ.get("SUBSCRIBERS_CSV_URL", "").strip()
    if url:
        try:
            r = requests.get(to_csv_url(url), timeout=60, allow_redirects=True)
            r.raise_for_status()
            text = r.content.decode("utf-8-sig", errors="replace")
            if "<html" in text[:400].lower():
                raise ValueError("下載到的是網頁而非 CSV，請確認試算表已「發布到網路」且格式選 CSV")
            subs = parse_subscribers_csv(text)
            print(f"訂閱來源：Google 表單（讀到 {len(subs)} 位有效訂閱者）")
            return subs
        except Exception as e:
            print(f"[警告] 表單讀取失敗（{e}）")
            print("       改用 subscribers.yaml 作為備援名單")
    try:
        subs = load_yaml("subscribers.yaml")["subscribers"]
        print(f"訂閱來源：subscribers.yaml（{len(subs)} 筆）")
        return subs
    except Exception as e:
        print(f"[錯誤] 備援名單也讀取失敗：{e}")
        return []


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


def _finite(v):
    """NaN 與 None 都算沒有資料。

    不能用 `if not v` 判斷 —— NaN 在 Python 是 truthy，`not NaN` 會得到
    False，這正是舊版讓 nan 溜進報告的原因。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None      # f != f 只有 NaN 成立


def snapshot(ticker):
    hist = fetch_history(ticker)
    if hist is None:
        return None

    # Yahoo 有時會先把當日那一列建出來（Volume 已有值）但 Close 還沒補上。
    # 直接取 iloc[-1] 就會拿到 NaN，一路算成 nan% 印進報告，
    # AI 還會為了一檔沒有價格的股票白花一次呼叫。
    # 只保留 Close 有效的列，等於自動退回最近一個「資料完整」的交易日，
    # 而 date 欄會誠實反映那一天是哪天。
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 2:
        return None

    last, prev = hist.iloc[-1], hist.iloc[-2]
    close, prev_close = _finite(last["Close"]), _finite(prev["Close"])
    if close is None or not prev_close:
        return None

    change_pct = (close - prev_close) / prev_close * 100

    vol_ratio = None
    vol = _finite(last.get("Volume"))
    if vol is not None and len(hist) > 6:
        avg_vol = _finite(hist["Volume"].iloc[-21:-1].mean())
        if avg_vol:
            vol_ratio = vol / avg_vol

    return {
        "ticker": ticker,
        "date": hist.index[-1].date().isoformat(),
        "close": close,
        "prev_close": prev_close,
        "change_pct": change_pct,
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
def google_news(query, lang="en", limit=12, days=3):
    """抓取近 N 天新聞，回傳結構化項目（含來源與連結）"""
    q = f"{query} when:{days}d"
    if lang == "zh":
        url = (f"https://news.google.com/rss/search?q={quote(q)}"
               f"&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    else:
        url = (f"https://news.google.com/rss/search?q={quote(q)}"
               f"&hl=en-US&gl=US&ceid=US:en")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    items = []
    try:
        feed = feedparser.parse(url)
        for e in feed.entries:
            if len(items) >= limit:
                break
            pub = None
            if getattr(e, "published_parsed", None):
                pub = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
                if pub < cutoff:      # 硬性過濾，確保只用近三天的消息
                    continue
            src = ""
            try:
                src = e.source.title
            except Exception:
                pass
            items.append({
                "title": (e.get("title") or "").strip(),
                "source": src or "—",
                "date": pub.date().isoformat() if pub else "",
                "link": e.get("link", ""),
            })
    except Exception as ex:
        print(f"  [警告] 新聞抓取失敗 {query}：{ex}")
    return items


# 這份報告只涵蓋美股與台股。代號在各國交易所會重複（NEM 在美國是金礦商
# Newmont，在德國 XETRA 是軟體商 Nemetschek），標題若把代號標成外國交易所，
# 就一定不是我們要的那家公司。
FOREIGN_EXCHANGES = (
    "XTRA|ETR|FRA|BER|MUN|STU|HAM|SWX|VTX|EPA|AMS|BIT|BME|LIS|"
    "STO|CPH|HEL|OSL|WSE|PRA|IST|LON|LSE|TSE|TYO|HKG|SHA|SHE|"
    "KRX|KOSDAQ|NSE|BSE|JSE|BVMF|TSX|TSXV|CVE|ASX|NZE|SGX|IDX|BKK|KLSE"
)


def is_relevant(title, ticker):
    """標題是否真的在講這檔美股／台股。

    1. 代號被標成外國交易所（如 'Nemetschek (XTRA:NEM)'）→ 排除。
    2. 代號用字界比對，避免 'NEM' 命中 'NEMETSCHEK'。

    訂閱者只提供代號、沒有公司名，所以這裡只能靠代號判斷；
    個人版有 portfolio.yaml 的 name 與 aliases，判斷會更準。
    """
    code = ticker.split(".")[0]
    if re.search(rf"(?:{FOREIGN_EXCHANGES})\s*:\s*{re.escape(code)}\b", title, re.I):
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])",
                          title, re.I))


def rank_news(items, ticker, top_n):
    """先濾掉不是在講這檔股票的標題，再依日期新到舊排序。

    寧可佐證則數不足，也不要塞進錯誤的新聞——錯的佐證比沒有佐證更糟。
    """
    relevant = [it for it in items if is_relevant(it["title"], ticker)]
    return sorted(relevant, key=lambda it: it.get("date", ""), reverse=True)[:top_n]


def news_for_ticker(t, days=3, top_n=5):
    """先抓近 N 日；若相關的則數不足，再放寬到近七日補足（寧可舊，不可錯）"""
    code = t.split(".")[0]

    def fetch(d):
        if is_tw(t):
            return google_news(f"{code} 股價", lang="zh", days=d)
        return google_news(f"{t} stock", lang="en", days=d)

    picked = rank_news(fetch(days), t, top_n)
    if len(picked) < top_n and days < 7:
        wider = rank_news(fetch(7), t, top_n)
        if len(wider) > len(picked):
            picked = wider
    return picked


def news_for_prompt(items, lang):
    """給 AI 看的編號清單，編號與報告中的佐證清單一致"""
    if not items:
        return "（近三日未取得相關新聞）" if lang == "zh" else "(No headlines found in the past 3 days.)"
    return "\n".join(f"[{i}] {it['title']}（{it['source']}, {it['date']}）"
                     for i, it in enumerate(items, 1))


def news_html(items, lang):
    """報告中呈現的佐證清單（標題＋來源＋日期＋原文連結）"""
    if not items:
        msg = ("近三日未取得可佐證的新聞來源。" if lang == "zh"
               else "No supporting headlines found in the past 3 days.")
        return f"<div class='evidence'><div class='ev-title'>{msg}</div></div>"
    head = "新聞佐證（近三日）" if lang == "zh" else "Sources (past 3 days)"
    rows = "".join(
        f"<li>{it['title']}　<span class='ev-meta'>— {it['source']}, {it['date']}</span>"
        + (f" <a href='{it['link']}'>{'原文' if lang == 'zh' else 'link'}</a>" if it["link"] else "")
        + "</li>"
        for it in items
    )
    return (f"<div class='evidence'><div class='ev-title'>{head}</div>"
            f"<ol class='ev-list'>{rows}</ol></div>")


# ------------------------------------------------------------
# AI（Anthropic API，全域花費預算）
# ------------------------------------------------------------
_quota_exhausted = False
_ai_calls_used = 0
_ai_client = None
QUOTA_MSG = {
    "zh": "（本次 AI 呼叫已達設定的上限，此段分析略過）",
    "en": "(AI call budget for this run was reached; this section was skipped.)",
}


def call_ai(cfg, system, user, lang, max_tokens=1200):
    global _quota_exhausted, _ai_calls_used, _ai_client
    budget = int(cfg["ai"].get("max_total_ai_calls", 200))
    if _quota_exhausted or _ai_calls_used >= budget:
        _quota_exhausted = True
        return QUOTA_MSG[lang]

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "(Missing ANTHROPIC_API_KEY — set it in GitHub Secrets or .env.sh)"

    if _ai_client is None:
        _ai_client = anthropic.Anthropic()

    # SDK 內建 429／連線錯誤的指數退避重試，不需要自己寫重試迴圈
    try:
        resp = _ai_client.messages.create(
            model=cfg["ai"]["model"],
            max_tokens=max_tokens,
            temperature=float(cfg["ai"].get("temperature", 0.4)),
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        _ai_calls_used += 1
        return resp.content[0].text.strip()
    except anthropic.AuthenticationError as e:
        print(f"  [錯誤] API key 無效：{e}")
        _quota_exhausted = True   # key 錯了後面每次都會錯，直接停手不要空轉
        return "(ANTHROPIC_API_KEY is invalid or revoked.)"
    except anthropic.APIStatusError as e:
        # 402 = 帳戶額度用完需儲值；429 重試後仍失敗也會走到這裡
        print(f"  [錯誤] AI 呼叫失敗 HTTP {e.status_code}：{str(e)[:300]}")
        if e.status_code in (402, 429):
            _quota_exhausted = True
            return QUOTA_MSG[lang]
        return "(AI analysis failed — see log.)"
    except Exception as e:
        print(f"  [警告] AI 呼叫錯誤：{e}")
        return "(AI analysis failed — see log.)"


MARKET_SYSTEM = {
    "zh": ("你是一位資深總經與美股策略分析師，讀者是有基本財務知識的散戶。"
           "你沒有上網能力，只能使用提供的收盤數據與已編號的新聞標題。\n"
           "要求：1) 論述用 [n] 標註新聞依據，至少引用 2 則；"
           "2) 每個判斷都要附上實際數字（指數漲跌幅、殖利率水準、VIX 變化）；"
           "3) 點名具體事件（哪個數據、哪位官員、哪家公司財報），不要寫「市場觀望」「情緒謹慎」"
           "這類換成任何一天都成立的句子；4) 資料裡沒有的因果一律不要斷言，"
           "找不到明確驅動因素就說今天缺乏單一主導題材。"),
    "en": ("You are a senior macro and US equity strategist writing for financially literate retail readers. "
           "You have NO web access; use only the closing data and numbered headlines provided.\n"
           "Requirements: 1) cite headlines as [n], at least 2 citations; 2) attach real figures to every "
           "judgement (index moves, yield levels, VIX change); 3) name specific events — which data release, "
           "which official, whose earnings — never filler like 'markets were cautious' that would fit any day; "
           "4) assert no causality absent from the material; if there is no clear driver, say the session "
           "lacked a single dominant theme."),
}

STOCK_SYSTEM = {
    "zh": ("你是一位資深買方產業分析師。讀者是有基本財務知識的散戶——看得懂財測、本益比、"
           "beta、類股輪動等術語，不需要解釋基礎名詞，但需要你把「今天為什麼漲跌」講到有憑有據。\n"
           "你沒有上網能力，只能使用提供的價量數據與已編號的新聞標題。\n\n"
           "【硬性要求，違反即為不合格】\n"
           "1. 引用佐證：論述時用 [1][2][3] 標註依據的新聞編號，全文至少引用 3 則。"
           "若提供的新聞不足 3 則、或內容與該股無關，明確寫出「可佐證的新聞不足」並只引用有效的，"
           "不要為了湊數而牽強連結。\n"
           "2. 具體優先：每個論點都要有可查證的事實——數字、百分比、日期、公司名、機構名、產品或"
           "製程代號。嚴禁只寫「市場情緒轉弱」「投資人保持觀望」「受到大盤影響」這種沒有主詞、"
           "沒有數據、換成任何一檔股票都成立的句子。\n"
           "3. 量化比較：明確寫出個股漲跌幅與所屬類股 ETF、大盤指數的差距並下判斷。"
           "格式示範（僅示範寫法，數字要用實際資料）：「XYZ -2.2%，同期 SMH -2.1%、S&P 500 -1.0%，"
           "與類股同步但落後大盤 1.2 個百分點，顯示賣壓來自類股層級而非個股利空」。\n"
           "4. 次產業精準度：不要只說「半導體」，要指出是 IC 設計、晶圓代工、先進封裝、HBM 記憶體、"
           "設備或 EDA 哪一塊；不要只說「科技股」，要區分雲端、數位廣告、SaaS 或硬體；"
           "礦業要區分貴金屬（避險/利率邏輯）與工業金屬（景氣循環邏輯）。\n"
           "5. 誠實條款（最重要）：新聞中若找不到個股層級的催化劑，就直接寫「今日走勢主要由類股／"
           "大盤驅動，近三日新聞中無重大個股消息」。無法從提供資料推導的因果關係一律不要寫，"
           "絕不編造財報數字、法說內容或分析師動作。\n"
           "6. 用字規範：這是觀察報告不是操作指示。不要使用「建議」二字，要表達後續方向時"
           "請用「可待關注」「值得留意」；也不要出現目標價、進出場時機或買賣指示。\n\n"
           "輸出：繁體中文 markdown，250–400 字，用下列四個粗體小標分段。"),
    "en": ("You are a senior buy-side industry analyst. Your readers are retail investors with basic "
           "financial literacy — they understand guidance, multiples, beta and sector rotation, so don't "
           "explain basics; do give them a defensible answer to 'why did this move today'.\n"
           "You have NO web access. Use only the price/volume data and the numbered headlines provided.\n\n"
           "[Hard requirements — output is unacceptable if violated]\n"
           "1. Cite your evidence: mark claims with [1][2][3] referring to the numbered headlines, at least "
           "3 citations overall. If fewer than 3 headlines are relevant, say so explicitly and cite only the "
           "valid ones rather than forcing a connection.\n"
           "2. Specificity first: every claim needs a checkable fact — a number, percentage, date, company "
           "name, institution or product/process node. Never write filler like 'sentiment weakened' or "
           "'investors stayed cautious' — sentences that would be equally true of any stock are unacceptable.\n"
           "3. Quantified comparison: state the stock's move against its sector ETF and the index, then draw "
           "a conclusion. Illustrative form only (use the real figures): 'XYZ -2.2% vs SMH -2.1% and S&P 500 "
           "-1.0% — in line with the sector but 1.2pp behind the index, implying sector-level pressure "
           "rather than company-specific bad news.'\n"
           "4. Sub-industry precision: not 'semiconductors' but fabless design, foundry, advanced packaging, "
           "HBM memory, equipment or EDA; not 'tech' but cloud, digital advertising, SaaS or hardware; for "
           "miners, separate precious metals (hedge/rates logic) from industrial metals (cyclical logic).\n"
           "5. Honesty clause (most important): if no company-specific catalyst appears in the headlines, "
           "state plainly that the move mainly tracked the sector/market with no major company news in the "
           "past three days. Never infer beyond the provided material, and never invent earnings figures, "
           "call transcripts or analyst actions.\n\n"
           "Output: English markdown, 200–320 words, using the four bold sub-headings below."),
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


def analyze_stock(cfg, lang, snap, market_ctx, news_items):
    news_text = news_for_prompt(news_items, lang)
    vol_txt = f"{snap['vol_ratio']:.2f}x" if snap.get("vol_ratio") else "n/a"
    n = len(news_items)
    if lang == "zh":
        user = f"""股票：{snap['ticker']}
今日數據（資料日期 {snap['date']}）：收盤 {snap['close']:.2f}，漲跌 {snap['change_pct']:+.2f}%，
成交量為 20 日均量的 {vol_txt}

今日大盤與類股對照數據（請用來做量化比較）：
{market_ctx}

近三日相關新聞（共 {n} 則，引用時請用編號）：
{news_text}

請分析這檔股票今天漲跌的原因，嚴格依下列格式輸出（粗體小標＋內文）：
**主要原因**：（點出最可能的驅動因素，並用 [n] 標註佐證來源）
**產業鏈觀察**：（指出精確的次產業定位與上下游／客戶連動，並用 [n] 佐證）
**與大盤/類股的關係**：（寫出個股 % vs 類股 ETF % vs 大盤 % 的具體比較與判斷）
**後續觀察**：（依據新聞中已出現的線索指出後續變數，不要虛構日期或事件）"""
    else:
        user = f"""Stock: {snap['ticker']}
Today's data (as of {snap['date']}): close {snap['close']:.2f}, change {snap['change_pct']:+.2f}%,
volume at {vol_txt} of the 20-day average

Market and sector reference data (use this for the quantified comparison):
{market_ctx}

Headlines from the past 3 days ({n} total — cite them by number):
{news_text}

Explain why this stock moved today, strictly in this format (bold labels + text):
**Key driver**: (the most likely catalyst, with [n] citations)
**Industry-chain view**: (precise sub-industry positioning and supply-chain/customer linkages, cited)
**Versus market & sector**: (explicit stock % vs sector ETF % vs index % comparison and conclusion)
**What to watch**: (forward variables grounded in the headlines above — no invented dates or events)"""
    return call_ai(cfg, STOCK_SYSTEM[lang], user, lang, max_tokens=1400)


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
/* 換頁規則只綁 .section。AI 產出的 markdown 常自帶標題（會轉成 h2/h3），
   若對所有 h2 換頁，那些標題會各自擠出一頁，造成大量半空白頁。 */
h2.section { font-size: 13.5pt; color: #166534; border-left: 5px solid #166534;
  padding-left: 9px; margin: 22px 0 10px 0; page-break-after: avoid;
  page-break-before: always; }
h2.section.first { page-break-before: avoid; margin-top: 6px; }
h1, h2, h3, h4 { page-break-after: avoid; }
h1:not(.section), h2:not(.section) { font-size: 11pt; color: #166534;
  margin: 12px 0 5px 0; border: none; padding: 0; }
h3.stock-head { font-size: 11.5pt; color: #111827; margin: 16px 0 6px 0;
  padding: 5px 8px; background: #f0fdf4; border-radius: 4px; page-break-after: avoid; }
h3:not(.stock-head), h4 { font-size: 10.5pt; color: #374151; margin: 10px 0 4px 0; }
table { width: 100%; border-collapse: collapse; font-size: 9pt; margin: 8px 0 14px 0;
  page-break-inside: avoid; }
th { background: #166534; color: #fff; padding: 5px 7px; text-align: left; font-weight: 600; }
td { border-bottom: 1px solid #e5e7eb; padding: 4px 7px; }
tr:nth-child(even) td { background: #f9fafb; }
.up { color: __UP_COLOR__; font-weight: 700; }
.down { color: __DOWN_COLOR__; font-weight: 700; }
.flat { color: #6b7280; }
.stock-block { margin-bottom: 14px; }
.stock-block + .stock-block { page-break-before: always; }
.evidence { background: #f8fafc; border-left: 3px solid #94a3b8; border-radius: 3px;
  padding: 6px 10px 6px 4px; margin: 8px 0 0 0; }
.ev-title { font-size: 8.5pt; font-weight: 700; color: #475569; margin-left: 6px; }
.ev-list { margin: 4px 0 2px 0; padding-left: 22px; font-size: 8.5pt; color: #334155; }
.ev-list li { margin-bottom: 2px; line-height: 1.45; }
.ev-meta { color: #94a3b8; }
.evidence a { color: #2563eb; text-decoration: none; }
.stock-block p { margin: 5px 0; }
.disclaimer { margin-top: 24px; padding: 9px 11px; border-top: 2px solid #cbd5e1;
  background: #f8fafc; font-size: 7.8pt; color: #64748b; line-height: 1.5;
  text-align: justify; page-break-before: avoid; }
.disc-title { font-weight: 700; color: #475569; font-size: 8.2pt;
  letter-spacing: 0.4px; margin-bottom: 3px; }
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


def strip_ai_headings(text):
    """移除 AI 自行加上的 markdown 標題。

    報表已有自己的大標與個股小標，AI 再寫一次就是重複佔版面。
    prompt 已要求不要加，但模型不一定遵守，所以程式端也拆一層：
    ATX 標題降級成粗體，開頭的直接丟掉。
    """
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not m:
            out.append(line)
            continue
        if not any(x.strip() for x in out):   # 開頭的標題直接省略
            continue
        out.append(f"**{m.group(1).strip()}**")
    return "\n".join(out).strip()


def md_to_html(text):
    return md_lib.markdown(strip_ai_headings(text), extensions=["extra"])


def build_report_html(lang, sub, rows, failed, market_overview,
                      analyses, synthesis, index_snaps, sector_snaps, news_cache):
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
        ev = news_html(news_cache.get(r["ticker"], []), lang)
        stocks_html += (
            f"<div class='stock-block'>"
            f"<h3 class='stock-head'>{r['ticker']}　{pct_html(r['change_pct'])}"
            f"（{t['close_label']} {r['close']:,.2f}）</h3>"
            f"{md_to_html(a)}{ev}</div>"
        )

    generated = NOW_LA.strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html lang="{ 'zh-Hant' if lang == 'zh' else 'en' }"><head><meta charset="utf-8"><style>{css}</style></head>
<body>
<div class="header">
  <h1>{t['title']}</h1>
  <div class="subtitle">{t['prepared_for']}{sub['name']}｜{TODAY}{t['tz_note']}｜{generated} PT｜{t['subtitle']}</div>
</div>

<h2 class="section first">{t['sec_market']}</h2>
{md_to_html(market_overview)}
<table><tr><th>{t['th_index']}</th><th>{t['th_close']}</th><th>{t['th_chg']}</th></tr>{idx_rows}</table>
<table><tr><th>{t['th_etf']}</th><th>{t['th_sector']}</th><th>{t['th_chg']}</th></tr>{sec_rows}</table>

<h2 class="section">{t['sec_watchlist']}</h2>
<table>
<tr><th>{t['th_ticker']}</th><th>{t['th_date']}</th><th>{t['th_close']}</th><th>{t['th_chg']}</th><th>{t['th_vol']}</th></tr>
{wl_rows}
</table>

<h2 class="section">{t['sec_stocks']}</h2>
{stocks_html}

<h2 class="section">{t['sec_synth']}</h2>
{md_to_html(synthesis)}

<div class="disclaimer"><div class="disc-title">{t['disclosure_title']}</div>{t['disclaimer']}</div>
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
    print(f"準備寄送給 {len(deliveries)} 位訂閱者：")
    for d in deliveries:
        pdf_langs = list(d["pdfs"].keys())
        print(f"  - {d['sub']['name']} <{d['sub']['email']}>："
              f"語言 {'+'.join(d['langs'])}，附件 {len(pdf_langs)} 份")

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
    include_synth = bool(cfg["ai"].get("include_synthesis", True))
    call_gap = float(cfg["ai"].get("seconds_between_calls", 5))
    sub_cfg = cfg.get("subscribers", {}) or {}
    blocklist = {str(e).strip().lower() for e in (sub_cfg.get("blocklist") or [])}
    max_subs = int(sub_cfg.get("max_subscribers", 30))

    raw_subs = load_subscribers()

    # 整理訂閱者
    subs = []
    for srec in raw_subs:
        email = str(srec.get("email", "")).strip()
        tickers = norm_tickers(srec.get("tickers"))
        if not valid_email(email) or not tickers:
            print(f"  [警告] 訂閱者資料不完整，跳過：{srec}")
            continue
        if email.lower() in blocklist:
            print(f"  [退訂] 略過 {email}")
            continue
        subs.append({"name": srec.get("name") or email.split("@")[0],
                     "email": email,
                     "langs": norm_lang(srec.get("language")),
                     "tickers": tickers})

    if len(subs) > max_subs:
        print(f"  [警告] 訂閱者 {len(subs)} 人超過上限 {max_subs}，本次僅處理前 {max_subs} 人")
        subs = subs[:max_subs]

    langs_needed = sorted({lg for s in subs for lg in s["langs"]})
    unique_tickers = sorted({tk for s in subs for tk in s["tickers"]})
    pair_count = len({(tk, lg) for s in subs for tk in s["tickers"] for lg in s["langs"]})
    synth_count = sum(len(s["langs"]) for s in subs) if include_synth else 0
    est_calls = len(langs_needed) + pair_count + synth_count

    print(f"=== {TODAY} 每日股票觀察報告（多人訂閱版）===")
    print(f"訂閱者 {len(subs)} 人｜語言 {langs_needed}｜不重複股票 {len(unique_tickers)} 檔")
    print(f"預估 AI 呼叫數：{est_calls}（上限 {cfg['ai'].get('max_total_ai_calls', 200)} 次）")
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
    news_days = int(cfg.get("news", {}).get("days", 3))
    news_shown = int(cfg.get("news", {}).get("items_per_stock", 5))
    print(f"抓取新聞（近 {news_days} 天，每檔取 {news_shown} 則）…")
    news_cache = {tk: news_for_ticker(tk, days=news_days, top_n=news_shown)
                  for tk in snaps}
    market_news_items = google_news("stock market today", limit=8, days=news_days)
    for tk, items in news_cache.items():
        if len(items) < 3:
            print(f"  [注意] {tk} 近 {news_days} 天僅取得 {len(items)} 則新聞，"
                  f"報告會註明佐證不足")

    # 各語言的市場對照數據（供個股做量化比較）
    ctx_lines = {}
    for lg in langs_needed:
        idx = "\n".join(f"- {n[lg]}: {s['close']:,.2f} ({s['change_pct']:+.2f}%)"
                        for n, s in index_snaps)
        sec = "\n".join(f"- {s['ticker']} {n[lg]}: {s['change_pct']:+.2f}%"
                        for n, s in sector_snaps)
        ctx_lines[lg] = f"{idx}\n{sec}"

    # 4) AI：大盤摘要（每語言一次）＋ 個股分析（每檔每語言一次，全員共用）
    overview, analyses = {}, {}
    if DRY_RUN:
        for lg in langs_needed:
            overview[lg] = f"(DRY_RUN placeholder market overview / {lg})"
        for tk in snaps:
            for lg in langs_needed:
                analyses[(tk, lg)] = (
                    f"**主要原因**：DRY_RUN 佔位文字 [1][2][3]。\n\n"
                    f"**產業鏈觀察**：{tk} placeholder.\n\n"
                    f"**與大盤/類股的關係**：placeholder.\n\n**後續觀察**：placeholder.")
    else:
        for lg in langs_needed:
            print(f"AI：大盤摘要（{lg}）…")
            idx_lines = "\n".join(f"- {n[lg]}: {s['close']:,.2f} ({s['change_pct']:+.2f}%)"
                                  for n, s in index_snaps)
            sec_lines = "\n".join(f"- {s['ticker']} {n[lg]}: {s['change_pct']:+.2f}%"
                                  for n, s in sector_snaps)
            overview[lg] = analyze_market(cfg, lg, idx_lines, sec_lines,
                                          news_for_prompt(market_news_items, lg))
            time.sleep(call_gap)
        need_pairs = sorted({(tk, lg) for s in subs for tk in s["tickers"]
                             for lg in s["langs"] if tk in snaps})
        for tk, lg in need_pairs:
            print(f"AI：分析 {tk}（{lg}）…")
            analyses[(tk, lg)] = analyze_stock(cfg, lg, snaps[tk],
                                               ctx_lines[lg], news_cache.get(tk, []))
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
                                     analyses, synth, index_snaps, sector_snaps,
                                     news_cache)
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
