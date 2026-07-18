#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日投資組合分析報告【免費版】
AI 引擎：GitHub Models（Actions 內建 GITHUB_TOKEN，零成本、免申請 API key）
與付費版差異：AI 無法上網搜尋查證，只能依據 RSS 新聞標題與市場數據推論

流程：讀取持股 → 抓價格/指數/類股/新聞 → GitHub Models 逐檔分析 → PDF → Email

需要的環境變數：
  GITHUB_TOKEN        Actions 自動提供（workflow 需設 permissions: models: read）
  GMAIL_ADDRESS       寄件 Gmail 帳號
  GMAIL_APP_PASSWORD  Gmail 應用程式密碼（不是登入密碼）
  RECIPIENT_EMAIL     收件人（選填，不填就寄給自己）

本機測試：
  DRY_RUN=1 python daily_report_free.py   跳過 AI 與寄信，只測資料抓取與 PDF
  FORCE=1   python daily_report_free.py   休市日也強制執行
"""

import os
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
QUOTA_MSG = ("（今日 GitHub Models 免費額度已用完，此段分析略過。"
             "持股檔數較多時可減少檔數，或改用付費版）")

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
# 新聞（Google News RSS）
# 免費版沒有 AI 網路搜尋，新聞標題是 AI 唯一的資訊來源，
# 所以抓多一點（12 則），抓不夠時自動放寬到近 2 天
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
            items.append(f"- {title}（{src}，{published}）")
        return items
    except Exception as ex:
        print(f"  [警告] 新聞抓取失敗 {query}：{ex}")
        return []


def news_for_holding(h):
    name = h.get("name") or h["ticker"]
    t = h["ticker"]
    if is_tw(t):
        code = t.split(".")[0]
        items = google_news(f"{name} {code} 股價 when:1d", lang="zh")
        if len(items) < 4:
            items = google_news(f"{name} {code} when:2d", lang="zh")
    else:
        items = google_news(f"{name} {t} stock when:1d", lang="en")
        if len(items) < 4:
            items = google_news(f"{name} {t} when:2d", lang="en")
    return items


# ------------------------------------------------------------
# AI 分析（GitHub Models 免費 API）
# 免費額度（每個 GitHub 帳號）：mini 級模型約 150 次/天，
# 每次請求上限 8K tokens 輸入 / 4K 輸出，所以輸入都有做裁切
# ------------------------------------------------------------
_quota_exhausted = False


def call_ai(cfg, system, user, max_tokens=1200):
    global _quota_exhausted
    if _quota_exhausted:
        return QUOTA_MSG

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return ("（找不到 GITHUB_TOKEN。請確認 workflow 的 permissions 區塊"
                "有 models: read，且 env 有帶入 GITHUB_TOKEN）")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
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
            r = requests.post(GH_MODELS_URL, headers=headers,
                              json=payload, timeout=180)
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"].strip()

            if r.status_code == 429:
                try:
                    wait = int(r.headers.get("Retry-After", "30"))
                except ValueError:
                    wait = 30
                if wait > 300:  # 等待超過 5 分鐘 = 當日額度用完，不再硬等
                    print(f"  [限流] 當日免費額度已用完（需等 {wait} 秒重置）")
                    _quota_exhausted = True
                    return QUOTA_MSG
                print(f"  [限流] 429，等待 {wait} 秒後重試（第 {attempt + 1} 次）")
                time.sleep(wait + 2)
                continue

            if r.status_code in (401, 403):
                print(f"  [錯誤] 權限不足 HTTP {r.status_code}：{r.text[:300]}")
                return ("（GitHub Models 權限錯誤：請確認 workflow 的 "
                        "permissions 有 models: read）")

            print(f"  [警告] AI 呼叫失敗 HTTP {r.status_code}：{r.text[:300]}")
            time.sleep(10 * (attempt + 1))
        except Exception as e:
            print(f"  [警告] AI 呼叫錯誤：{e}")
            time.sleep(10 * (attempt + 1))

    return "（AI 分析產生失敗，請至 GitHub Actions 查看日誌）"


MARKET_SYSTEM = (
    "你是一位資深的總體經濟與美股市場分析師，為一位有台股技術分析背景的投資人"
    "撰寫繁體中文日報。你沒有上網查證的能力，只能依據使用者提供的收盤數據與新聞標題分析。"
    "分析要具體引用提供的資料；資料中沒有的事情不要斷言，寧可保守。"
)

STOCK_SYSTEM = (
    "你是一位資深買方產業分析師，任務是解讀投資人持有的個股「今天為什麼漲或跌」。"
    "你沒有上網查證的能力，只能依據提供的價量數據、新聞標題與大盤背景推論。要求：\n"
    "1. 具體：從提供的新聞標題中找出實際催化劑（財報、升降評、訂單、產品消息等），"
    "引用時註明是哪則標題的資訊。\n"
    "2. 產業鏈視角：依該公司在產業結構中的位置分析。例如半導體要區分 IC 設計（fabless）、"
    "晶圓代工、設備、材料、記憶體、EDA 等次產業，並考慮上下游供應鏈與主要客戶的連動；"
    "軟體股看訂閱/雲端與 AI 商業化；金融股看利率環境；以此類推。\n"
    "3. 區分 alpha 與 beta：比較個股漲跌幅與大盤及所屬類股 ETF 的表現，"
    "判斷今天的波動是「自身消息驅動」還是「跟著類股/大盤走」，或被其他權值股帶動。\n"
    "4. 誠實原則（最重要）：如果提供的標題中沒有明確的個股催化劑，"
    "就直接說「今日波動主要反映大盤/類股走勢，提供的新聞中無重大個股消息」，"
    "絕對不要編造、猜測或過度解讀理由。\n"
    "5. 用繁體中文 markdown 輸出，長度控制在 200–400 字。"
)

SYNTH_SYSTEM = (
    "你是一位資深投資組合策略師，為投資人做每日總結。你沒有上網查證的能力，"
    "只能依據提供的資料歸納。重點是找出「跨個股的共同主題」"
    "（例如同一條供應鏈的連動、同一總經因素影響多檔持股）。"
    "提到後續關注事項時，只能基於提供的新聞中出現的資訊或一般性的週期"
    "（如財報季、FOMC 例會），不要虛構具體日期。用繁體中文 markdown 輸出。"
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
直接輸出 markdown 內文，不需要大標題。"""
    return call_ai(cfg, MARKET_SYSTEM, user, max_tokens=1200)


def analyze_stock(cfg, h, snap, market_overview, peer_line, news_items):
    news_text = "\n".join(news_items) if news_items else "（近兩日 RSS 未抓到相關標題）"
    vol_txt = f"{snap['vol_ratio']:.2f} 倍（vs 20 日均量）" if snap.get("vol_ratio") else "無資料"
    notes = h.get("notes", "")
    user = f"""持股：{h['ticker']} {h.get('name', '')}
投資人補充的產業背景：{notes if notes else '（無）'}

今日數據（資料日期 {snap['date']}）：
收盤 {snap['close']:.2f}，漲跌 {snap['change_pct']:+.2f}%，量能 {vol_txt}

今日大盤背景摘要（供判斷 beta 用）：
{market_overview[:700]}

我投資組合中所有持股今日表現（觀察連動）：
{peer_line}

相關新聞標題：
{news_text}

請分析這檔股票今天漲跌的原因，輸出格式（粗體小標 + 內文）：
**主要原因**：…
**產業鏈觀察**：…
**與大盤/類股的關係**：…
**後續觀察**：…"""
    return call_ai(cfg, STOCK_SYSTEM, user, max_tokens=1200)


def synthesize(cfg, market_overview, pnl_line, stock_sections):
    # 免費版每次請求輸入上限約 8K tokens，逐檔分析先裁切再彙整
    parts = []
    for r, a in stock_sections:
        parts.append(f"### {r['ticker']} {r['name']}（{r['change_pct']:+.2f}%）\n{a[:500]}")
    analyses_text = "\n\n".join(parts)
    user = f"""以下是今天（{TODAY}）投資組合的資料。

【大盤摘要】
{market_overview[:600]}

【投資組合當日損益】
{pnl_line}

【各持股分析（節錄）】
{analyses_text}

請寫「綜合觀察與後續關注」（約 300–500 字），包含：
1. 3–5 個跨個股的共同主題（同一供應鏈連動、同一總經因素、資金輪動方向）
2. 今天投資組合整體表現的一句話定調
3. 後續值得留意的方向（只能依據上面資料中出現的資訊，不要虛構日期）
直接輸出 markdown 內文，不需要大標題。"""
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
h2 {
    font-size: 13.5pt; color: #166534; border-left: 5px solid #166534;
    padding-left: 9px; margin: 22px 0 10px 0; page-break-after: avoid;
}
h3 {
    font-size: 11.5pt; color: #111827; margin: 16px 0 6px 0;
    padding: 5px 8px; background: #f0fdf4; border-radius: 4px;
    page-break-after: avoid;
}
table { width: 100%; border-collapse: collapse; font-size: 9pt; margin: 8px 0 14px 0; }
th { background: #166534; color: #fff; padding: 5px 7px; text-align: left; font-weight: 600; }
td { border-bottom: 1px solid #e5e7eb; padding: 4px 7px; }
tr:nth-child(even) td { background: #f9fafb; }
.up { color: __UP_COLOR__; font-weight: 700; }
.down { color: __DOWN_COLOR__; font-weight: 700; }
.flat { color: #6b7280; }
.stock-block { page-break-inside: avoid; margin-bottom: 6px; }
.stock-block p { margin: 5px 0; }
.disclaimer {
    margin-top: 22px; padding-top: 8px; border-top: 1px solid #e5e7eb;
    font-size: 8pt; color: #9ca3af;
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


def money(v, cur):
    sign = "+" if v > 0 else ""
    prefix = "NT$" if cur == "TWD" else "$"
    return f"{sign}{prefix}{v:,.0f}" if cur == "TWD" else f"{sign}{prefix}{v:,.2f}"


def md_to_html(text):
    return md_lib.markdown(text or "", extensions=["extra"])


def build_report_html(cfg, index_snaps, sector_snaps, holding_rows, totals,
                      market_overview, stock_sections, synthesis):
    css = build_css(cfg)
    title = cfg.get("report", {}).get("title", "每日投資組合分析報告（免費版）")

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
        vol_txt = f"{r['vol_ratio']:.2f}x" if r.get("vol_ratio") else "—"
        pnl_cls = "up" if r["day_pnl"] > 0 else ("down" if r["day_pnl"] < 0 else "flat")
        pf_rows += (
            f"<tr><td><b>{r['ticker']}</b></td><td>{r['name']}</td>"
            f"<td>{r['date']}</td><td>{r['close']:,.2f}</td>"
            f"<td>{pct_html(r['change_pct'])}</td><td>{vol_txt}</td>"
            f"<td class='{pnl_cls}'>{money(r['day_pnl'], r['currency'])}</td></tr>"
        )
    totals_txt = "　".join(
        f"{cur} 部位當日損益：<span class='{'up' if v > 0 else ('down' if v < 0 else 'flat')}'>{money(v, cur)}</span>"
        for cur, v in totals.items()
    )

    stocks_html = ""
    for r, analysis in stock_sections:
        stocks_html += (
            f"<div class='stock-block'>"
            f"<h3>{r['ticker']}　{r['name']}　{pct_html(r['change_pct'])}"
            f"（收盤 {r['close']:,.2f}）</h3>"
            f"{md_to_html(analysis)}</div>"
        )

    generated = NOW_LA.strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8"><style>{css}</style></head>
<body>
<div class="header">
  <h1>{title}</h1>
  <div class="subtitle">{TODAY}（洛杉磯時間）｜產生時間 {generated} PT｜資料來源：Yahoo Finance、Google News、GitHub Models AI（免費版，未經網路查證）</div>
</div>

<h2>一、大盤與總經</h2>
{md_to_html(market_overview)}
<table><tr><th>指數</th><th>收盤</th><th>漲跌幅</th></tr>{idx_rows}</table>
<table><tr><th>ETF</th><th>類股</th><th>漲跌幅</th></tr>{sec_rows}</table>

<h2>二、投資組合總覽</h2>
<table>
<tr><th>代號</th><th>名稱</th><th>資料日期</th><th>收盤</th><th>漲跌幅</th><th>量能</th><th>當日損益</th></tr>
{pf_rows}
</table>
<p><b>{totals_txt}</b></p>

<h2>三、個股漲跌原因分析</h2>
{stocks_html}

<h2>四、綜合觀察與後續關注</h2>
{md_to_html(synthesis)}

<div class="disclaimer">本報告由自動化系統產生（免費版：AI 僅依據新聞標題與價量數據推論，未經網路查證），僅供個人參考，不構成任何投資建議。價格與新聞資料可能有延遲或錯誤，重大決策請以官方來源為準。</div>
</body></html>"""
    return html


# ------------------------------------------------------------
# Email
# ------------------------------------------------------------
def send_email(cfg, pdf_path, holding_rows, totals):
    addr = os.environ["GMAIL_ADDRESS"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("RECIPIENT_EMAIL") or addr

    subject = f"{cfg.get('email', {}).get('subject_prefix', '每日投資組合分析（免費版）')} {TODAY}"

    lines = "".join(
        f"<tr><td>{r['ticker']}</td><td style='text-align:right'>{r['close']:,.2f}</td>"
        f"<td style='text-align:right'>{r['change_pct']:+.2f}%</td>"
        f"<td style='text-align:right'>{money(r['day_pnl'], r['currency'])}</td></tr>"
        for r in holding_rows
    )
    totals_txt = "、".join(f"{cur} {money(v, cur)}" for cur, v in totals.items())
    body = f"""<div style="font-family:sans-serif;font-size:14px">
<p>今日投資組合摘要（{TODAY}，詳細分析請見附件 PDF）：</p>
<table border="0" cellpadding="4" style="border-collapse:collapse;font-size:13px">
<tr style="background:#f0fdf4"><th align="left">代號</th><th>收盤</th><th>漲跌</th><th>當日損益</th></tr>
{lines}
</table>
<p>當日損益合計：{totals_txt}</p>
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
    print(f"=== {TODAY} 每日投資組合報告（免費版）===")
    print(f"持股數：{len(holdings)}　模型：{cfg['ai']['model']}　DRY_RUN={DRY_RUN}")

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

    # 2) 持股資料
    print("抓取持股…")
    holding_rows, totals = [], {}
    for h in holdings:
        s = snapshot(h["ticker"])
        time.sleep(1)
        if not s:
            print(f"  [警告] {h['ticker']} 無法取得價格，跳過此檔")
            continue
        cur = currency_of(h["ticker"])
        shares = float(h.get("shares", 0))
        day_pnl = shares * (s["close"] - s["prev_close"])
        row = {**s, "name": h.get("name", ""), "shares": shares,
               "currency": cur, "day_pnl": day_pnl, "cfg": h}
        holding_rows.append(row)
        totals[cur] = totals.get(cur, 0) + day_pnl

    if not holding_rows:
        print("錯誤：所有持股都抓不到價格，中止。")
        sys.exit(1)

    peer_line = "、".join(f"{r['ticker']} {r['change_pct']:+.2f}%" for r in holding_rows)
    index_lines = "\n".join(f"- {n}：{s['close']:,.2f}（{s['change_pct']:+.2f}%）"
                            for n, s in index_snaps)
    sector_lines = "\n".join(f"- {s['ticker']} {n}：{s['change_pct']:+.2f}%"
                             for n, s in sector_snaps)

    # 3) 新聞
    print("抓取市場新聞…")
    market_news = "\n".join(google_news("stock market today when:1d", limit=10)) or "（無）"

    # 4) AI 分析（GitHub Models）
    call_gap = float(cfg["ai"].get("seconds_between_calls", 5))
    if DRY_RUN:
        market_overview = "（DRY_RUN 測試模式：此處為大盤摘要占位文字）"
        stock_sections = [(r, "**主要原因**：DRY_RUN 占位。\n\n**產業鏈觀察**：占位。")
                          for r in holding_rows]
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
            stock_sections.append((r, analysis))
            time.sleep(call_gap)

        print("AI：綜合觀察…")
        pnl_line = "、".join(f"{cur} {money(v, cur)}" for cur, v in totals.items())
        synthesis = synthesize(cfg, market_overview, pnl_line, stock_sections)

    # 5) PDF
    print("產生 PDF…")
    html = build_report_html(cfg, index_snaps, sector_snaps, holding_rows, totals,
                             market_overview, stock_sections, synthesis)
    os.makedirs("output", exist_ok=True)
    pdf_path = f"output/portfolio_report_free_{TODAY}.pdf"
    HTML(string=html).write_pdf(pdf_path)
    print(f"PDF 已產生：{pdf_path}（{os.path.getsize(pdf_path) / 1024:.0f} KB）")

    # 6) Email
    if DRY_RUN:
        print("DRY_RUN：跳過寄信。")
        return
    send_email(cfg, pdf_path, holding_rows, totals)
    print("完成。")


if __name__ == "__main__":
    main()
