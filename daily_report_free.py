#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日投資組合分析報告【個人版】
AI 引擎：Anthropic API（Claude）
與付費版差異：AI 無法上網搜尋查證，只能依據 RSS 新聞標題與市場數據推論

流程：讀取觀察清單 → 抓價格/指數/類股/新聞 → Claude 逐檔分析 → PDF → Email

需要的環境變數：
  ANTHROPIC_API_KEY   Anthropic API 金鑰（Actions 存在 Secrets，本機存在 .env.sh）
  GMAIL_ADDRESS       寄件 Gmail 帳號
  GMAIL_APP_PASSWORD  Gmail 應用程式密碼（不是登入密碼）
  RECIPIENT_EMAIL     收件人（選填，不填就寄給自己）

本機測試：
  DRY_RUN=1 python daily_report_free.py   跳過 AI 與寄信，只測資料抓取與 PDF
  FORCE=1   python daily_report_free.py   休市日也強制執行
"""

import os
import random
import re
import sys
import time
import smtplib
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from urllib.parse import quote
from zoneinfo import ZoneInfo

import yaml
import anthropic
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

# 盤後報價的擷取時刻（洛杉磯時間）。盤後時段報價一直在動，
# 不標時間的話讀者無從判斷這個數字是幾點的快照。
AH_CAPTURED_AT = None

QUOTA_MSG = ("（本次 AI 呼叫已達設定的上限，此段分析略過。"
             "如需分析更多檔數，請調高 config_free.yaml 的 max_total_ai_calls）")

INDEXES = [
    ("^GSPC", "S&P 500"),
    ("^IXIC", "那斯達克"),
    ("^DJI", "道瓊工業"),
    ("^SOX", "費城半導體"),
    ("^VIX", "VIX 恐慌指數"),
    ("^TNX", "美債10年殖利率(%)"),
]

SECTORS = [
    ("XLK", "科技"), ("SMH", "半導體"), ("XLC", "通訊服務"),
    ("XLY", "非必需消費"), ("XLP", "必需消費"), ("XLF", "金融"),
    ("XLV", "醫療保健"), ("XLI", "工業"), ("XLE", "能源"),
    ("XLB", "原物料"), ("XLU", "公用事業"), ("XLRE", "房地產"),
]


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def is_tw(ticker):
    t = ticker.upper()
    return t.endswith(".TW") or t.endswith(".TWO")


def currency_of(ticker):
    return "TWD" if is_tw(ticker) else "USD"


# ------------------------------------------------------------
# 市場資料（yfinance，含重試以應付 Yahoo 偶發限流）
# ------------------------------------------------------------
# 「最新交易日的收盤價遲遲未補上」的標的數。累積到門檻就判定是資料源
# 全站性的缺漏（而非個別標的的暫時失敗），後續標的直接接受舊資料，
# 不再逐檔軟重試 —— 否則整批標的每檔白等十幾秒。
_stale_tail_seen = 0
_STALE_TAIL_GIVE_UP = 3


def _valid_close_rows(hist):
    """有幾筆「收盤價真的存在」的日 K。

    不能用 len(hist) 判斷資料夠不夠：Yahoo 會先把當日那列建出來、
    Volume 已有值但 Close 還是 NaN，列數看起來正常、內容卻是空的。
    """
    if hist is None or "Close" not in getattr(hist, "columns", []):
        return 0
    return len(hist.dropna(subset=["Close"]))


def fetch_history(ticker, period="1mo", retries=6, soft_retries=2):
    """抓日 K，重試到拿回可用資料為止。

    Yahoo 的失敗有三種樣態，嚴重程度不同：
      A. 連線錯誤或限流（429）→ 拋例外，完全沒資料
      B. 回傳空的或有效收盤價不足兩筆 → 等於沒資料
      C. 列數與歷史都正常，只有「最新那一列」的 Close 是 NaN
         （當日日 K 尚未補完，Volume 已有值）

    A 與 B 會用滿 retries 次。C 只重試 soft_retries 次就接受 ——
    因為 C 已經有可用的歷史資料，snapshot() 會自動退回前一個完整交易日；
    若在台股尚未收盤時執行，那一列本來就不會補上，無限重試只會讓
    23 檔標的把 job 拖到逾時。

    舊版只看 len(hist) >= 2，C 這種情況會直接接受且不重試，
    於是 nan 一路印進報告（2026-08-03 台股四檔就是這樣）。

    退避採指數 + 隨機抖動：整批標的常同時撞限流，固定間隔會讓下一輪
    又在同一秒一起打過去，等於再撞一次。
    """
    last_err = ""
    for i in range(retries):
        try:
            hist = yf.Ticker(ticker).history(period=period)
            valid = _valid_close_rows(hist)
            if valid >= 2:
                if _finite(hist.iloc[-1]["Close"]) is not None:
                    if i:
                        print(f"  [恢復] {ticker} 第 {i + 1} 次嘗試成功（{valid} 筆有效收盤價）")
                    return hist
                # 最新一列沒有收盤價。先看那是哪一天：
                # 若日期就是該市場的「今天」（或更晚），代表這場交易還沒結束，
                # 那一列本來就不會補上，重試毫無意義 —— 直接接受，
                # snapshot() 會退回前一個完整交易日。
                # 只有日期已經是過去式，才代表資料遲到，值得再試。
                tail = hist.index[-1]
                if tail.date() >= dt.datetime.now(tail.tzinfo).date():
                    return hist
                # 已經確認這是 Yahoo 全站性的資料缺漏，就別再逐檔重試。
                # 實測：Yahoo 對 2026-08-03 的 ^GSPC 等標的收盤數小時後仍是
                # Open/Close 皆 NaN、只有 Volume。這種缺漏不會因為多等幾秒而補上，
                # 47 檔各軟重試兩次會讓整個 job 多花 8 分鐘。
                global _stale_tail_seen
                if _stale_tail_seen >= _STALE_TAIL_GIVE_UP:
                    return hist
                if i >= soft_retries:
                    _stale_tail_seen += 1
                    note = ("；已連續 {} 檔如此，判定為資料源全站性缺漏，"
                            "後續標的不再重試".format(_stale_tail_seen)
                            if _stale_tail_seen >= _STALE_TAIL_GIVE_UP else "")
                    print(f"  [接受] {ticker} {tail.date()} 的收盤價遲遲未補上，"
                          f"改用前一個完整交易日（有效資料 {valid} 筆）{note}")
                    return hist
                last_err = f"{tail.date()} 的收盤價尚未補上"
            else:
                rows = 0 if hist is None else len(hist)
                last_err = f"回傳 {rows} 列、其中僅 {valid} 筆有有效收盤價"
        except Exception as e:
            last_err = str(e).replace("\n", " ")[:120]

        if i < retries - 1:
            # 3, 6, 12, 24, 30…（上限 30）再加 0~2 秒抖動
            wait = min(3 * (2 ** i), 30) + random.uniform(0, 2)
            print(f"  [重試] {ticker} 第 {i + 1}/{retries} 次（{last_err}），"
                  f"{wait:.1f} 秒後重試")
            time.sleep(wait)

    print(f"  [放棄] {ticker} 重試 {retries} 次仍無有效資料：{last_err}")
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

    # 量能 = 當日成交量 ÷ 前 20 個交易日均量（分母不含當日）。
    # 一併回傳分母實際用了幾天：iloc[-21:-1] 在資料不足時會自動縮短，
    # 舊版對此毫無標示，7 個交易日的新股也會算出一個號稱「20 日均量」
    # 的數字。報告需要據此加註，不能讓讀者誤以為都是同一個基準。
    vol_ratio, vol_days = None, 0
    vol = _finite(last.get("Volume"))
    if vol is not None:
        window = hist["Volume"].iloc[-21:-1].dropna()
        vol_days = len(window)
        if vol_days >= 2:
            avg_vol = _finite(window.mean())
            if avg_vol:
                vol_ratio = vol / avg_vol
        else:
            vol_days = 0

    return {
        "ticker": ticker,
        "date": hist.index[-1].date().isoformat(),
        "close": close,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "vol_ratio": vol_ratio,
        "vol_days": vol_days,
    }


def after_hours(ticker):
    """盤後報價。沒有盤後交易的標的（台股、指數）回 None。

    走 Ticker.info —— fast_info 沒有 postMarketPrice 欄位。
    每檔約 0.3 秒，29 檔約 10 秒，成本可接受。

    postMarketChangePercent 的單位已經是百分點（實測 AAPL 312.4861 對
    311.00 是 +0.4778%，欄位值就是 0.47785），不需要再乘 100。

    ⚠️ 盤後成交稀薄、價差大，單筆大單就能拉動報價，且盤後漲跌不代表
    隔日開盤會維持。報告上必須標示這點，不可與正常盤數字並列而不加註。
    """
    try:
        info = yf.Ticker(ticker).info
    except Exception as e:
        print(f"  [警告] {ticker} 盤後報價抓取失敗：{str(e)[:80]}")
        return None
    price = _finite(info.get("postMarketPrice"))
    if price is None:
        return None
    return {"price": price, "change_pct": _finite(info.get("postMarketChangePercent"))}


def market_was_open_today():
    spy = fetch_history("SPY", period="5d")
    if spy is None:
        return True
    return spy.index[-1].date() == TODAY


# ------------------------------------------------------------
# 新聞（Google News RSS）
# AI 沒有網路搜尋能力，新聞標題是它唯一的資訊來源。
# 回傳結構化項目（標題／來源／日期／連結），讓報告能列出可查證的佐證清單，
# 且編號與 AI 引用的 [n] 一致。
# ------------------------------------------------------------
def google_news(query, lang="en", limit=12, days=3):
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
                if pub < cutoff:      # 硬性過濾，確保只用近 N 天的消息
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


def _word_in(text, word):
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])",
                          text, re.I))


def is_relevant(title, ticker, name, aliases=()):
    """標題是否真的在講這家美股／台股公司。

    兩層判斷：
    1. 標題把代號標成外國交易所（如 'Nemetschek (XTRA:NEM)'）→ 直接排除，
       除非公司名也對得上。
    2. 代號用字界比對（避免 'NEM' 命中 'NEMETSCHEK'），或公司名／別名對得上。

    別名是為了品牌名與法人名不同的情況：GOOG 的名稱是 Alphabet，
    但新聞標題幾乎都寫 Google，沒有別名就會把相關新聞全部濾掉。
    """
    code = ticker.split(".")[0]
    words = []
    for term in [name or ""] + list(aliases or ()):
        words += re.split(r"[^A-Za-z0-9一-鿿]+", term)
    name_hit = any(len(tok) >= 4 and _word_in(title, tok) for tok in words)
    if re.search(rf"(?:{FOREIGN_EXCHANGES})\s*:\s*{re.escape(code)}\b", title, re.I):
        return name_hit
    return _word_in(title, code) or name_hit


def rank_news(items, ticker, name, top_n, aliases=()):
    """先濾掉不是在講這家公司的標題，再依日期新到舊排序。

    寧可佐證則數不足，也不要塞進錯誤的新聞——錯的佐證比沒有佐證更糟。
    """
    relevant = [it for it in items if is_relevant(it["title"], ticker, name, aliases)]
    return sorted(relevant, key=lambda it: it.get("date", ""), reverse=True)[:top_n]


def news_for_holding(h, days=3, top_n=5):
    """先抓近三日；若相關的則數不足，再放寬到近七日補足（寧可舊，不可錯）"""
    t, name = h["ticker"], h.get("name", "")
    aliases = h.get("aliases") or ()
    code = t.split(".")[0]

    def fetch(d):
        if is_tw(t):
            return google_news(f"{code} 股價", lang="zh", days=d)
        return google_news(f"{t} stock", lang="en", days=d)

    picked = rank_news(fetch(days), t, name, top_n, aliases)
    if len(picked) < top_n:
        wider = rank_news(fetch(7), t, name, top_n, aliases)
        if len(wider) > len(picked):
            picked = wider
    return picked


def news_for_prompt(items):
    """給 AI 看的編號清單，編號與報告中的佐證清單一致"""
    if not items:
        return "（未取得相關新聞）"
    return "\n".join(f"[{i}] {it['title']}（{it['source']}, {it['date']}）"
                     for i, it in enumerate(items, 1))


def news_html(items):
    """報告中呈現的佐證清單（標題＋來源＋日期＋原文連結），編號對應 AI 的 [n]"""
    if not items:
        return ("<div class='evidence'><div class='ev-title'>"
                "未取得可佐證的相關新聞（已濾除非本公司的標題）。</div></div>")
    rows = "".join(
        f"<li>{it['title']}　<span class='ev-meta'>— {it['source']}, {it['date']}</span>"
        + (f" <a href='{it['link']}'>原文</a>" if it["link"] else "")
        + "</li>"
        for it in items
    )
    return ("<div class='evidence'><div class='ev-title'>新聞佐證</div>"
            f"<ol class='ev-list'>{rows}</ol></div>")


# ------------------------------------------------------------
# AI 分析（Anthropic API）
# max_total_ai_calls 是「花費上限」而不是免費額度：超過就用佔位文字降級，
# 避免標的數暴增時帳單失控。實際用量會在執行結束時印出來。
# ------------------------------------------------------------
_quota_exhausted = False
_ai_calls_used = 0
_ai_client = None


def call_ai(cfg, system, user, max_tokens=1200):
    global _quota_exhausted, _ai_calls_used, _ai_client
    budget = int(cfg["ai"].get("max_total_ai_calls", 60))
    if _quota_exhausted or _ai_calls_used >= budget:
        _quota_exhausted = True
        return QUOTA_MSG

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ("（找不到 ANTHROPIC_API_KEY。GitHub Actions 請到 Settings → "
                "Secrets and variables → Actions 新增；本機請設在 .env.sh）")

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
        _quota_exhausted = True   # key 錯了，後面每檔都會錯，直接停手不要空轉
        return "（ANTHROPIC_API_KEY 無效或已撤銷，請重新產生一組）"
    except anthropic.APIStatusError as e:
        # 402 = 額度用完需儲值；429 重試後仍失敗也會走到這裡
        print(f"  [錯誤] AI 呼叫失敗 HTTP {e.status_code}：{str(e)[:300]}")
        if e.status_code in (402, 429):
            _quota_exhausted = True
            return QUOTA_MSG
        return "（AI 分析產生失敗，請查看執行日誌）"
    except Exception as e:
        print(f"  [警告] AI 呼叫錯誤：{e}")
        return "（AI 分析產生失敗，請查看執行日誌）"


MARKET_SYSTEM = (
    "你是一位資深的總體經濟與美股市場分析師，為一位有台股技術分析背景的投資人"
    "撰寫繁體中文日報。你沒有上網查證的能力，只能依據使用者提供的收盤數據與新聞標題分析。"
    "分析要具體引用提供的資料；資料中沒有的事情不要斷言，寧可保守。"
)

STOCK_SYSTEM = (
    "你是一位資深產業分析師，任務是解讀個股「今天為什麼漲或跌」，寫給有基本財務"
    "知識的讀者。你沒有上網查證的能力，只能依據提供的價量數據、已編號的新聞標題"
    "與大盤背景推論。要求：\n"
    "1. 引用紀律：論述必須用 [n] 標註新聞依據，至少引用 2 則。只能引用提供的編號，"
    "不可自行編造新聞或編號。從標題中找出實際催化劑（財報、升降評、訂單、產品消息等）。\n"
    "2. 產業鏈視角：依該公司在產業結構中的位置分析。例如半導體要區分 IC 設計（fabless）、"
    "晶圓代工、設備、材料、記憶體、EDA 等次產業，並考慮上下游供應鏈與主要客戶的連動；"
    "軟體股看訂閱/雲端與 AI 商業化；金融股看利率環境；以此類推。\n"
    "3. 區分 alpha 與 beta：比較個股漲跌幅與大盤及所屬類股 ETF 的表現，"
    "判斷今天的波動是「自身消息驅動」還是「跟著類股/大盤走」，或被其他權值股帶動。\n"
    "4. 用字規範：不要使用「建議」二字。要表達後續觀察方向時，請用「可待關注」「值得留意」「後續觀察」。\n"
    "5. 誠實原則（最重要）：如果提供的標題中沒有明確的個股催化劑，"
    "就直接說「今日波動主要反映大盤/類股走勢，提供的新聞中無重大個股消息」，"
    "絕對不要編造、猜測或過度解讀理由。\n"
    "6. 中立性：這是一份觀察報告。不要出現買賣指示、目標價、進出場時機，"
    "也不要假設讀者持有這檔股票。\n"
    "7. 用繁體中文 markdown 輸出，長度控制在 200–400 字。"
)

SYNTH_SYSTEM = (
    "你是一位資深市場策略師，為這份觀察報告做每日總結。你沒有上網查證的能力，"
    "只能依據提供的資料歸納。重點是找出「跨個股的共同主題」"
    "（例如同一條供應鏈的連動、同一總經因素影響多檔標的）。"
    "提到後續關注事項時，只能基於提供的新聞中出現的資訊或一般性的週期"
    "（如財報季、FOMC 例會），不要虛構具體日期。這是觀察報告，不要出現買賣指示"
    "或目標價，也不要使用「建議」二字（改用「可待關注」「值得留意」）。"
    "用繁體中文 markdown 輸出。"
)


def analyze_market(cfg, index_lines, sector_lines, market_news):
    user = f"""今天是 {TODAY}（洛杉磯時間），以下是今日收盤數據。

【主要指數】
{index_lines}

【11 大類股 ETF + 半導體】
{sector_lines}

【市場焦點新聞標題（近 24 小時）】
{market_news}

請寫一段「今日大盤與總經摘要」（約 250–400 字），說明：
1. 今天美股整體表現與主要驅動因素（依據上面的新聞標題，要具體）
2. 類股輪動：哪些 sector 強、哪些弱、可能原因
3. 公債殖利率與 VIX 的變化反映了什麼市場情緒
直接輸出 markdown 內文。不要加任何標題行（報表已有大標，重複會佔掉版面）。"""
    return call_ai(cfg, MARKET_SYSTEM, user, max_tokens=1200)


def analyze_stock(cfg, h, snap, market_overview, peer_line, news_items):
    vol_txt = f"{snap['vol_ratio']:.2f} 倍（vs 20 日均量）" if snap.get("vol_ratio") else "無資料"
    # 盤後那段要餵給 AI，否則它會讀到「財報後大漲」的新聞、卻只拿到沒反應的
    # 正常盤收盤價，兩個訊號打架。
    ah = snap.get("after") or {}
    at = AH_CAPTURED_AT.strftime("%H:%M") if AH_CAPTURED_AT else "n/a"
    ah_line = ((f"盤後（{at} PT 擷取）：{ah['price']:.2f}（{ah['change_pct']:+.2f}%）。"
                "盤後成交稀薄，僅供參考，不可視為隔日開盤的預告。")
               if ah.get("price") is not None else "")
    notes = h.get("notes", "")
    user = f"""標的：{h['ticker']} {h.get('name', '')}
產業背景補充：{notes if notes else '（無）'}

今日數據（資料日期 {snap['date']}）：
收盤 {snap['close']:.2f}，漲跌 {snap['change_pct']:+.2f}%，量能 {vol_txt}
{ah_line}

今日大盤背景摘要（供判斷 beta 用）：
{market_overview[:700]}

同份報告中其他標的今日表現（觀察連動）：
{peer_line}

已編號的相關新聞（你只能引用這幾則，編號與報告中的佐證清單一致）：
{news_for_prompt(news_items)}

請分析這檔股票今天漲跌的原因，輸出格式（粗體小標 + 內文）：
**主要原因**：…
**產業鏈觀察**：…
**與大盤/類股的關係**：…
**後續觀察**：…"""
    return call_ai(cfg, STOCK_SYSTEM, user, max_tokens=1200)


def synthesize(cfg, market_overview, stock_sections):
    # 控制輸入長度以節省 token 成本，逐檔分析先裁切再彙整
    parts = []
    for r, a, _news in stock_sections:
        parts.append(f"### {r['ticker']} {r['name']}（{r['change_pct']:+.2f}%）\n{a[:500]}")
    analyses_text = "\n\n".join(parts)
    user = f"""以下是今天（{TODAY}）這份報告涵蓋的資料。

【大盤摘要】
{market_overview[:600]}

【各標的分析（節錄）】
{analyses_text}

請寫「綜合觀察與後續關注」（約 300–500 字），包含：
1. 3–5 個跨標的的共同主題（同一供應鏈連動、同一總經因素、資金輪動方向）
2. 今天這組標的整體表現的一句話定調
3. 後續值得留意的方向（只能依據上面資料中出現的資訊，不要虛構日期）
直接輸出 markdown 內文。不要加任何標題行（報表已有大標，重複會佔掉版面）。"""
    return call_ai(cfg, SYNTH_SYSTEM, user, max_tokens=1500)


# ------------------------------------------------------------
# HTML / PDF 報告
# ------------------------------------------------------------
CSS_TEMPLATE = """
@page {
    size: A4;
    margin: 1.6cm 1.5cm;
    @bottom-center {
        content: counter(page) " / " counter(pages);
        font-size: 8pt; color: #999;
        font-family: 'Noto Sans CJK TC', sans-serif;
    }
}
body {
    font-family: 'Noto Sans CJK TC', 'Noto Sans TC', 'PingFang TC', sans-serif;
    font-size: 10.5pt; line-height: 1.6; color: #1f2937; margin: 0;
}
.header { border-bottom: 3px solid #166534; padding-bottom: 10px; margin-bottom: 18px; }
h1 { font-size: 19pt; color: #166534; margin: 0 0 4px 0; }
.subtitle { color: #6b7280; font-size: 9.5pt; }
/* 只有報表自己的四個大標才換頁。
   AI 產出的 markdown 常自帶標題（會被轉成 h2/h3），若對所有 h2 換頁，
   那些標題會各自擠出一頁，導致大量半空白頁 —— 所以規則綁在 .section 上。 */
h2.section {
    font-size: 13.5pt; color: #166534; border-left: 5px solid #166534;
    padding-left: 9px; margin: 22px 0 10px 0; page-break-after: avoid;
    page-break-before: always;
}
h2.section.first { page-break-before: avoid; margin-top: 6px; }

/* AI markdown 自帶的標題：降級成內文小標，不換頁、不搶版面 */
h1, h2, h3, h4 { page-break-after: avoid; }
h1:not(.section), h2:not(.section) {
    font-size: 11pt; color: #166534; margin: 12px 0 5px 0;
    border: none; padding: 0;
}
h3.stock-head {
    font-size: 11.5pt; color: #111827; margin: 16px 0 6px 0;
    padding: 5px 8px; background: #f0fdf4; border-radius: 4px;
    page-break-after: avoid;
}
h3:not(.stock-head), h4 { font-size: 10.5pt; color: #374151; margin: 10px 0 4px 0; }
table { width: 100%; border-collapse: collapse; font-size: 9pt; margin: 8px 0 14px 0;
  page-break-inside: avoid; }
th { background: #166534; color: #fff; padding: 5px 7px; text-align: left; font-weight: 600; }
td { border-bottom: 1px solid #e5e7eb; padding: 4px 7px; }
tr:nth-child(even) td { background: #f9fafb; }
.up { color: __UP_COLOR__; font-weight: 700; }
.down { color: __DOWN_COLOR__; font-weight: 700; }
.flat { color: #6b7280; }
/* 每檔標的各自一頁；第一檔接在「三、」大標後面，所以只在相鄰的區塊間換頁 */
.stock-block { margin-bottom: 6px; }
.stock-block + .stock-block { page-break-before: always; }
.stock-block p { margin: 5px 0; }
.evidence { background: #f8fafc; border-left: 3px solid #94a3b8; border-radius: 3px;
  padding: 6px 10px 6px 4px; margin: 10px 0 0 0; }
.ev-title { font-size: 8.5pt; font-weight: 700; color: #475569; margin-left: 6px; }
.ev-list { margin: 4px 0 2px 0; padding-left: 22px; font-size: 8.5pt; color: #334155; }
.ev-list li { margin-bottom: 2px; line-height: 1.45; }
.ev-meta { color: #94a3b8; }
.ah-note { font-size: 8pt; color: #6b7280; line-height: 1.5;
  margin: -6px 0 14px 0; padding-left: 2px; }
.evidence a { color: #2563eb; text-decoration: none; }
.disclaimer {
    margin-top: 22px; padding-top: 8px; border-top: 1px solid #e5e7eb;
    font-size: 8pt; color: #9ca3af;
    page-break-before: avoid;   /* 跟著綜合觀察，不要自己佔一頁 */
}
strong { color: #111827; }
"""


def build_css(cfg):
    convention = cfg.get("report", {}).get("color_convention", "tw")
    if convention == "us":
        up, down = "#15803d", "#dc2626"
    else:
        up, down = "#dc2626", "#15803d"  # 台式：紅漲綠跌
    return CSS_TEMPLATE.replace("__UP_COLOR__", up).replace("__DOWN_COLOR__", down)


def pct_html(p):
    if p is None:
        return '<span class="flat">—</span>'
    cls = "up" if p > 0 else ("down" if p < 0 else "flat")
    return f'<span class="{cls}">{p:+.2f}%</span>'


def strip_ai_headings(text):
    """移除 AI 自行加上的 markdown 標題。

    報表已經有自己的大標與個股小標，AI 再寫一次「綜合觀察與後續關注」
    就是重複佔版面。prompt 已要求不要加，但模型不一定遵守，
    所以在程式端也拆一層：ATX 標題一律降級成粗體，開頭的直接丟掉。
    """
    lines, out = (text or "").splitlines(), []
    for line in lines:
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not m:
            out.append(line)
            continue
        title = m.group(1).strip()
        # 開頭的標題（前面還沒有實質內容）直接省略，其餘保留為粗體小標
        if not any(x.strip() for x in out):
            continue
        out.append(f"**{title}**")
    return "\n".join(out).strip()


def md_to_html(text):
    return md_lib.markdown(strip_ai_headings(text), extensions=["extra"])


def build_report_html(cfg, index_snaps, sector_snaps, holding_rows,
                      market_overview, stock_sections, synthesis):
    css = build_css(cfg)
    title = cfg.get("report", {}).get("title", "每日投資組合分析報告")

    idx_rows = "".join(
        f"<tr><td>{name}</td><td>{s['close']:,.2f}</td><td>{pct_html(s['change_pct'])}</td></tr>"
        for name, s in index_snaps
    )
    sec_sorted = sorted(sector_snaps, key=lambda x: x[1]["change_pct"], reverse=True)
    sec_rows = "".join(
        f"<tr><td>{s['ticker']}</td><td>{name}</td><td>{pct_html(s['change_pct'])}</td></tr>"
        for name, s in sec_sorted
    )

    pf_rows = ""
    for r in holding_rows:
        vd = r.get("vol_days", 0)
        vol_txt = (f"{r['vol_ratio']:.2f}x{'*' if 0 < vd < 20 else ''}"
                   if r.get("vol_ratio") else "—")
        ah = r.get("after") or {}
        ah_px = f"{ah['price']:,.2f}" if ah.get("price") is not None else "—"
        ah_chg = pct_html(ah["change_pct"]) if ah.get("change_pct") is not None else "—"
        pf_rows += (
            f"<tr><td><b>{r['ticker']}</b></td><td>{r['name']}</td>"
            f"<td>{r['date']}</td><td>{r['close']:,.2f}</td>"
            f"<td>{pct_html(r['change_pct'])}</td>"
            f"<td>{ah_px}</td><td>{ah_chg}</td><td>{vol_txt}</td></tr>"
        )

    # 分母不足 20 日的標的要逐一列出天數，只放一個星號讀者無從判斷差多少
    short = [f"{r['ticker']} {r['vol_days']}日" for r in holding_rows
             if r.get("vol_ratio") and 0 < r.get("vol_days", 0) < 20]
    vol_note = ("量能 = 當日成交量 ÷ 前 20 個交易日平均成交量（分母不含當日）。"
                "1.0x 代表與近期平均持平，數字越大表示今日交易越活躍。"
                + (f"標示 * 者可用資料不足 20 個交易日，分母改以實際天數計算："
                   + "、".join(short) + "。" if short else ""))

    stocks_html = ""
    for r, analysis, news_items in stock_sections:
        stocks_html += (
            f"<div class='stock-block'>"
            f"<h3 class='stock-head'>{r['ticker']}　{r['name']}　{pct_html(r['change_pct'])}"
            f"（收盤 {r['close']:,.2f}）</h3>"
            f"{md_to_html(analysis)}"
            f"{news_html(news_items)}</div>"
        )

    generated = NOW_LA.strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8"><style>{css}</style></head>
<body>
<div class="header">
  <h1>{title}</h1>
  <div class="subtitle">{TODAY}（洛杉磯時間）｜產生時間 {generated} PT｜資料來源：Yahoo Finance、Google News、Claude AI（未經網路查證）</div>
</div>

<h2 class="section first">一、大盤與總經</h2>
{md_to_html(market_overview)}
<table><tr><th>指數</th><th>收盤</th><th>漲跌幅</th></tr>{idx_rows}</table>
<table><tr><th>ETF</th><th>類股</th><th>漲跌幅</th></tr>{sec_rows}</table>

<h2 class="section">二、觀察標的總覽</h2>
<table>
<tr><th>代號</th><th>名稱</th><th>資料日期</th><th>收盤</th><th>漲跌幅</th><th>盤後</th><th>盤後漲跌</th><th>量能</th></tr>
{pf_rows}
</table>
<div class="ah-note">盤後報價擷取時間：{AH_CAPTURED_AT.strftime('%H:%M') if AH_CAPTURED_AT else '—'} PT。盤後為正常盤收盤後的延長交易時段，成交稀薄、買賣價差大，單筆委託即可牽動報價，且盤後漲跌不代表隔日開盤會延續。台股與指數無盤後交易，以「—」表示。</div>
<div class="ah-note">{vol_note}</div>

<h2 class="section">三、個股漲跌原因分析</h2>
{stocks_html}

<h2 class="section">四、綜合觀察與後續關注</h2>
{md_to_html(synthesis)}

<div class="disclaimer">本報告由自動化系統產生（AI 僅依據新聞標題與價量數據推論，未經網路查證），為一般性資訊與教育目的，非個人化投資建議。價格與新聞資料可能有延遲或錯誤，重大決策請以官方來源為準。</div>
</body></html>"""
    return html


# ------------------------------------------------------------
# Email
# ------------------------------------------------------------
def send_email(cfg, pdf_path, holding_rows):
    addr = os.environ["GMAIL_ADDRESS"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("RECIPIENT_EMAIL") or addr

    subject = f"{cfg.get('email', {}).get('subject_prefix', '每日投資組合分析')} {TODAY}"

    lines = "".join(
        f"<tr><td>{r['ticker']}</td><td style='text-align:right'>{r['close']:,.2f}</td>"
        f"<td style='text-align:right'>{r['change_pct']:+.2f}%</td></tr>"
        for r in holding_rows
    )
    body = f"""<div style="font-family:sans-serif;font-size:14px">
<p>今日觀察標的摘要（{TODAY}，詳細分析請見附件 PDF）：</p>
<table border="0" cellpadding="4" style="border-collapse:collapse;font-size:13px">
<tr style="background:#f0fdf4"><th align="left">代號</th><th>收盤</th><th>漲跌</th></tr>
{lines}
</table>
</div>"""

    msg = MIMEMultipart()
    msg["From"] = addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html", "utf-8"))
    with open(pdf_path, "rb") as f:
        part = MIMEApplication(f.read(), _subtype="pdf")
    part.add_header("Content-Disposition", "attachment",
                    filename=os.path.basename(pdf_path))
    msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(addr, pwd)
        s.send_message(msg)
    print(f"已寄送至 {to}")


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
def main():
    cfg = load_yaml("config_free.yaml")
    holdings = load_yaml("portfolio.yaml")["holdings"]
    print(f"=== {TODAY} 每日投資組合報告 ===")
    print(f"標的數：{len(holdings)}　模型：{cfg['ai']['model']}　DRY_RUN={DRY_RUN}")

    if not FORCE and not market_was_open_today():
        print("今日美股休市（週末或假日），跳過執行。")
        return

    # 1) 指數與類股
    print("抓取指數…")
    index_snaps = []
    for tkr, name in INDEXES:
        s = snapshot(tkr)
        if s:
            index_snaps.append((name, s))
        time.sleep(1)
    has_tw = any(is_tw(h["ticker"]) for h in holdings)
    if has_tw:
        s = snapshot("^TWII")
        if s:
            index_snaps.append(("台股加權指數", s))

    print("抓取類股 ETF…")
    sector_snaps = []
    for tkr, name in SECTORS:
        s = snapshot(tkr)
        if s:
            sector_snaps.append((name, s))
        time.sleep(1)

    # 2) 標的價量資料
    print("抓取標的價量…")
    holding_rows = []
    for h in holdings:
        s = snapshot(h["ticker"])
        time.sleep(1)
        if not s:
            print(f"  [警告] {h['ticker']} 無法取得價格，跳過此檔")
            continue
        row = {**s, "name": h.get("name", ""),
               "currency": currency_of(h["ticker"]), "cfg": h}
        holding_rows.append(row)

    if not holding_rows:
        print("錯誤：所有標的都抓不到價格，中止。")
        sys.exit(1)

    # 盤後報價（與多人版同一套做法，見 daily_brief_multi.py 的說明）
    global AH_CAPTURED_AT
    AH_CAPTURED_AT = dt.datetime.now(LA)
    print(f"抓取盤後報價（{AH_CAPTURED_AT.strftime('%H:%M')} PT）…")
    ah_n = 0
    for r in holding_rows:
        a = after_hours(r["ticker"])
        if a:
            r["after"] = a
            ah_n += 1
        time.sleep(0.3)
    print(f"  {ah_n}/{len(holding_rows)} 檔有盤後報價（台股與指數無盤後交易）")

    peer_line = "、".join(f"{r['ticker']} {r['change_pct']:+.2f}%" for r in holding_rows)
    index_lines = "\n".join(f"- {n}：{s['close']:,.2f}（{s['change_pct']:+.2f}%）"
                            for n, s in index_snaps)
    sector_lines = "\n".join(f"- {s['ticker']} {n}：{s['change_pct']:+.2f}%"
                             for n, s in sector_snaps)

    # 3) 新聞
    print("抓取市場新聞…")
    market_news = news_for_prompt(google_news("stock market today", limit=10, days=1))

    # 4) AI 分析（Anthropic API）
    call_gap = float(cfg["ai"].get("seconds_between_calls", 5))
    if DRY_RUN:
        market_overview = "（DRY_RUN 測試模式：此處為大盤摘要占位文字）"
        stock_sections = [(r, "**主要原因**：DRY_RUN 占位 [1]。\n\n**產業鏈觀察**：占位。",
                           news_for_holding(r["cfg"])) for r in holding_rows]
        synthesis = "（DRY_RUN 測試模式：此處為綜合觀察占位文字）"
    else:
        print("AI：大盤摘要…")
        market_overview = analyze_market(cfg, index_lines, sector_lines, market_news)
        time.sleep(call_gap)

        stock_sections = []
        for r in holding_rows:
            print(f"AI：分析 {r['ticker']}…")
            news_items = news_for_holding(r["cfg"])
            analysis = analyze_stock(cfg, r["cfg"], r,
                                     market_overview, peer_line, news_items)
            stock_sections.append((r, analysis, news_items))
            time.sleep(call_gap)

        print("AI：綜合觀察…")
        synthesis = synthesize(cfg, market_overview, stock_sections)

    # 5) PDF
    print("產生 PDF…")
    html = build_report_html(cfg, index_snaps, sector_snaps, holding_rows,
                             market_overview, stock_sections, synthesis)
    os.makedirs("output", exist_ok=True)
    pdf_path = f"output/portfolio_report_free_{TODAY}.pdf"
    HTML(string=html).write_pdf(pdf_path)
    print(f"PDF 已產生：{pdf_path}（{os.path.getsize(pdf_path) / 1024:.0f} KB）")
    print(f"AI 實際用量：{_ai_calls_used} 次")

    # 6) Email
    if DRY_RUN:
        print("DRY_RUN：跳過寄信。")
        return
    send_email(cfg, pdf_path, holding_rows)
    print("完成。")


if __name__ == "__main__":
    main()
