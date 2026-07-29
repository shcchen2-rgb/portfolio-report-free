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
        "title": "投資組合觀察清單報告",
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
        "fmt_driver": "**主要原因**：…\n**產業鏈觀察**：…\n**與大盤/類股的關係**：…\n**後續變數**：…",
    },
    "en": {
        "title": "Portfolio Watchlist Report",
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
        "fmt_driver": "**Key driver**: …\n**Industry-chain view**: …\n**Versus market & sector**: …\n**Upcoming items**: …",
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


def rank_news(items, ticker, top_n):
    """標題有提到代號的排前面，其次依日期新到舊"""
    base = ticker.split(".")[0].upper()

    def score(it):
        return (1 if base in it["title"].upper() else 0, it.get("date", ""))

    return sorted(items, key=score, reverse=True)[:top_n]


def news_for_ticker(t, days=3, top_n=5):
    if is_tw(t):
        code = t.split(".")[0]
        items = google_news(f"{code} 股價", lang="zh", days=days)
    else:
        items = google_news(f"{t} stock", lang="en", days=days)
    return rank_news(items, t, top_n)


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
    "zh": ("你是一位資深總經與美股策略分析師，讀者是有基本財務知識的散戶。"
           "你沒有上網能力，只能使用提供的收盤數據與已編號的新聞標題。\n"
           "要求：1) 論述用 [n] 標註新聞依據，至少引用 2 則；"
           "2) 每個判斷都要附上實際數字（指數漲跌幅、殖利率水準、VIX 變化）；"
           "3) 點名具體事件（哪個數據、哪位官員、哪家公司財報），不要寫「市場觀望」「情緒謹慎」"
           "這類換成任何一天都成立的句子；4) 資料裡沒有的因果一律不要斷言，"
           "找不到明確驅動因素就說今天缺乏單一主導題材。\n"
           "【用語紅線，違反即不合格】不得預測未來方向（如『有望反彈』『仍有下行風險』）；"
           "不得使用『建議』『應』『訊號』『布局』等指示或操作字眼；只描述已發生的事實及其新聞依據。"),
    "en": ("You are a senior macro and US equity strategist writing for financially literate retail readers. "
           "You have NO web access; use only the closing data and numbered headlines provided.\n"
           "Requirements: 1) cite headlines as [n], at least 2 citations; 2) attach real figures to every "
           "judgement (index moves, yield levels, VIX change); 3) name specific events — which data release, "
           "which official, whose earnings — never filler like 'markets were cautious' that would fit any day; "
           "4) assert no causality absent from the material; if there is no clear driver, say the session "
           "lacked a single dominant theme.\n"
           "[Language red lines — violation = unacceptable] Never forecast future direction (e.g. 'poised to "
           "rebound', 'downside risk remains'); never use 'recommend', 'should', 'signal', or 'position/buy' "
           "wording; describe only what has already happened and its news basis."),
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
           "6. 用語紅線（違反即不合格，最重要）：\n"
           "   - 禁止規範性用語：不得出現『建議』『應』『須』『宜』『值得布局』等指示讀者行動的字眼。\n"
           "   - 禁止方向性預測：不得預測未來股價方向，如『有望上漲』『估值修復可期』『仍有下行風險』"
           "『可能反彈』。只陳述已發生的事實與其新聞依據，不對未來漲跌表態。\n"
           "   - 禁止交易/操作語彙：不得使用『訊號』『買點』『逢低布局』『資金流入/流出』等操作暗示詞。\n"
           "   - 轉述但不延伸：新聞中的分析師目標價、評級可如實轉述（屬新聞事實），"
           "但不得加上自己的延伸推論，例如不可寫『顯示長線看好基本面』。\n\n"
           "輸出：繁體中文 markdown，220–360 字，用下列四個粗體小標分段。"),
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
           "call transcripts or analyst actions.\n"
           "6. Language red lines (violation = unacceptable, most important):\n"
           "   - No prescriptive language: never use 'recommend', 'should', 'ought to', 'worth buying/"
           "accumulating', or any wording that tells the reader to act.\n"
           "   - No directional forecasts: never predict future price direction (e.g. 'poised to rise', "
           "'valuation recovery likely', 'downside risk remains', 'may rebound'). State only what has already "
           "happened and its news basis; take no stance on future moves.\n"
           "   - No trading/operational vocabulary: never use 'signal', 'entry point', 'buy the dip', "
           "'fund inflow/outflow', or similar operational hints.\n"
           "   - Transcribe, don't extend: analyst target prices and ratings from the news may be transcribed "
           "as-is (they are news facts), but never add your own extrapolation such as 'showing long-term "
           "confidence in fundamentals'.\n\n"
           "Output: English markdown, 180–300 words, using the four bold sub-headings below."),
}

SYNTH_SYSTEM = {
    "zh": ("你是一位資深市場觀察者，為讀者的觀察清單做每日『描述性』彙整（不是投資建議）。"
           "你沒有上網能力，只能依據提供的資料歸納。任務是客觀描述『今天這份清單發生了什麼』，"
           "找出跨個股的共同主題（同一供應鏈連動、同一總經因素同時影響多檔）。\n"
           "【用語紅線，違反即不合格】不得對未來方向表態（不得寫『後續看好』『值得留意的布局方向』等）；"
           "不得使用『建議』『應』『訊號』『布局』等字；不得對個別讀者給出任何行動指引。"
           "只描述已發生的事實與其成因，所有推論須有個股分析或新聞為依據。繁體中文 markdown。"),
    "en": ("You are a senior market observer writing a DESCRIPTIVE daily wrap-up of the reader's watchlist "
           "(not investment advice). No web access; use only the provided material. Your task is to objectively "
           "describe what happened across this list today and surface cross-stock themes (shared supply chains, "
           "one macro driver affecting several names).\n"
           "[Language red lines — violation = unacceptable] Take no stance on future direction (no 'constructive "
           "going forward', no 'positioning to watch'); never use 'recommend', 'should', 'signal', or 'position'; "
           "give no action guidance to any individual reader. Describe only what has already happened and why, "
           "with every inference grounded in the per-stock analysis or headlines. English markdown."),
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
**後續變數**：（僅中性列舉新聞中已出現的待觀察事項或即將公布的事件，例如「財報預定於某日公布」；不得預測方向、不得使用建議／應／訊號等字，若新聞中無明確事件則寫「近三日新聞未提及特定後續事件」）"""
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
**Upcoming items**: (neutrally list only items or scheduled events already named in the headlines, e.g. "earnings due on X"; no direction forecast, no recommend/should/signal wording; if none appear, write "no specific upcoming events mentioned in the past 3 days")"""
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

請寫「綜合觀察」（約 250–380 字），只做客觀描述，不給任何方向建議：
1) 開頭先用一句話做機械式統計（例：本清單 N 檔中 X 檔上漲、Y 檔下跌，其中以某類股跌幅最深）
2) 2–4 個跨個股的共同主題（供應鏈連動、同一總經因素同時影響多檔），每個主題須對應到上面的個股分析
3) 若要提到後續事件，只能中性列舉新聞中已出現的既定事件（如某檔財報日），不得寫「值得留意的方向」或任何看多看空的表態。
直接輸出 markdown 內文。"""
    else:
        user = f"""Below are today's ({TODAY}) results and per-stock analysis excerpts for {sub_name}'s watchlist:

{text}

Write "Cross-Stock Themes" (180–280 words), purely descriptive with no directional advice:
1) open with a one-line mechanical tally (e.g. "of N names, X rose and Y fell, with [sector] down most")
2) 2–4 themes cutting across these stocks (supply-chain linkages, one macro driver hitting several names),
each tied back to the per-stock analysis above
3) if mentioning anything forward, only neutrally list already-scheduled events named in the news
(e.g. an earnings date); never write "what to watch" as a view or take any bullish/bearish stance.
Markdown body only."""
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
.stock-block { page-break-inside: avoid; margin-bottom: 14px; }
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
  text-align: justify; }
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


def md_to_html(text):
    return md_lib.markdown(text or "", extensions=["extra"])


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
            f"<h3>{r['ticker']}　{pct_html(r['change_pct'])}"
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
                subject = f"📈 投資組合觀察清單報告 / Portfolio Watchlist Report {TODAY}"
            elif main_lang == "zh":
                subject = f"📈 投資組合觀察清單報告 {TODAY}"
            else:
                subject = f"📈 Portfolio Watchlist Report {TODAY}"

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
                    f"**與大盤/類股的關係**：placeholder.\n\n**後續變數**：placeholder.")
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
