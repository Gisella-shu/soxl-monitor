#!/usr/bin/env python3
"""SOXL 刀神均線三刀流＋美股大盤監控（GitHub Actions 雲端版，推播到 ntfy）。

看線方法（出自《海期刀神教你用100張圖實戰均線三刀流》）：
  60分K 三條均線：小藍 20MA、小綠 60MA、小橘 240MA
  小橘往上＝多方環境、往下＝空方環境；小藍>小綠>小橘＝多頭排列，反過來＝空頭排列
  另外看：價格站上/跌破哪條線、藍綠黃金/死亡交叉、日K 季線乖離（>30% 過熱）

每小時 :03 由 GitHub Actions 執行。推播時段：美股交易日 美東 07–19 點每小時一次；
其他時間只有「站上/跌破小綠或小橘、排列改變、藍綠交叉、小橘轉向、季線乖離破30%」才推播（高優先）。
規則：數字一律從 API 取；取不到就寫「取不到」。只描述線的狀態，不給買賣建議。

本機測試：python monitor.py            → 印出回報（不推播、不寫 state）
          python monitor.py --push     → 立刻推播一次（不寫 state）
          python monitor.py --run      → 排程模式
環境變數 NTFY_TOPIC：ntfy 頻道名稱（GitHub Secret）。
"""
import hashlib, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, date
from zoneinfo import ZoneInfo

ET_TZ, TPE = ZoneInfo("America/New_York"), ZoneInfo("Asia/Taipei")
YF = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=5m&range=1d&includePrePost=true"
DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, "state.json")
NA = "取不到"
UA = {"User-Agent": "Mozilla/5.0 (soxl-monitor)"}

# 大盤與個股（Yahoo 代號, 顯示名稱）
MARKET = [("^NDX", "那指100"), ("NQ=F", "那指期貨"), ("^SOX", "費半"), ("SOXL", "SOXL"), ("QQQ", "QQQ")]
MAG7 = [("NVDA", "輝達"), ("AAPL", "蘋果"), ("MSFT", "微軟"), ("GOOGL", "Google"),
        ("AMZN", "亞馬遜"), ("META", "Meta"), ("TSLA", "特斯拉")]

# 新聞關鍵字（Google News RSS，近 1 天）
NEWS_QUERIES = [
    "Nasdaq OR \"semiconductor stocks\" OR SOXL",
    "Nvidia OR TSMC OR \"chip export\" OR \"export controls\"",
    "\"AI regulation\" OR \"AI ban\" OR \"AI chips\" restrictions",
    "midterm election stocks OR Fed rate decision",
    "Apple OR Microsoft OR Alphabet OR Amazon OR Meta OR Tesla earnings OR launch",
]
NEWS_MAX = 6
# 只收主流財經／科技媒體，過濾論壇、遊戲站等雜訊
NEWS_SOURCES = ("reuters", "bloomberg", "cnbc", "wall street journal", "wsj", "marketwatch", "barron",
                "financial times", "yahoo finance", "associated press", "ap news", "investing.com", "benzinga",
                "seeking alpha", "nikkei", "fortune", "business insider", "axios", "the information",
                "techcrunch", "the verge", "forbes", "fox business", "politico", "the new york times",
                "bnn bloomberg", "investopedia", "the motley fool", "digitimes", "tom's hardware")

# NYSE 休市日
HOLIDAYS = {date(2026, 11, 26), date(2026, 12, 25), date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15),
            date(2027, 3, 26), date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
            date(2027, 11, 25), date(2027, 12, 24)}


def http(url, body=None, headers=None):
    h = dict(UA, **(headers or {}))
    data = None
    if body is not None:
        data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=20).read()


def jhttp(url, body=None):
    return json.loads(http(url, body))


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------- 刀神均線三刀流 ----------------
# 依《海期刀神教你用100張圖實戰均線三刀流》書摘：60分K，小藍=20MA、小綠=60MA、小橘=240MA。
# 斜率：書上沒寫算法，這裡用「這根 MA 比前一根高或低」判斷上揚／下彎。
YF_60 = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=60m&range=3mo"
YF_1D = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1d&range=6mo"
LINES = (("blue", "小藍", 20), ("green", "小綠", 60), ("orange", "小橘", 240))


def _closes(url):
    r = jhttp(url)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    rows = [(c, h) for c, h in zip(q.get("close") or [], q.get("high") or []) if c is not None and h is not None]
    return [c for c, _ in rows], [h for _, h in rows]


def _ma(xs, n, back=0):
    xs = xs[:len(xs) - back] if back else xs
    return sum(xs[-n:]) / n if len(xs) >= n else None


def analyze(closes, d_closes=None, d_highs=None):
    """回傳三刀流狀態 dict；資料不足的欄位為 None。"""
    s = {"price": closes[-1] if closes else None}
    for key, _, n in LINES:
        now, before = _ma(closes, n), _ma(closes, n, 1)
        s[key] = now
        s[key + "_up"] = None if (now is None or before is None) else now > before
        s[key + "_above"] = None if now is None else s["price"] > now
    b, g, o = s["blue"], s["green"], s["orange"]
    if None in (b, g, o):
        s["arrange"] = None
    elif b > g > o:
        s["arrange"] = "多頭排列"
    elif b < g < o:
        s["arrange"] = "空頭排列"
    else:
        s["arrange"] = "糾結（沒有排列）"
    s["env"] = None if s["orange_up"] is None else ("多方環境" if s["orange_up"] else "空方環境")
    # 藍綠交叉：上一根 vs 這一根
    pb, pg = _ma(closes, 20, 1), _ma(closes, 60, 1)
    s["cross"] = None
    if None not in (b, g, pb, pg):
        if pb <= pg and b > g:
            s["cross"] = "黃金交叉"
        elif pb >= pg and b < g:
            s["cross"] = "死亡交叉"
    # 日K 乖離（用今天最高價對日K月線、季線）
    s["dev_m"] = s["dev_q"] = None
    if d_closes:
        m20, m60, hi = _ma(d_closes, 20), _ma(d_closes, 60), d_highs[-1]
        s["dev_m"] = (hi / m20 - 1) * 100 if m20 else None
        s["dev_q"] = (hi / m60 - 1) * 100 if m60 else None
    return s


def fetch_ma(sym, daily=True):
    try:
        c, _ = _closes(YF_60.format(urllib.parse.quote(sym)))
        dc = dh = None
        if daily:
            try:
                dc, dh = _closes(YF_1D.format(urllib.parse.quote(sym)))
            except Exception as e:
                print(f"Yahoo 日K {sym} 失敗:", e)
        return analyze(c, dc, dh)
    except Exception as e:
        print(f"Yahoo 60分K {sym} 失敗:", e)
        return None


def summary(s):
    """把狀態翻成一句白話，只引用書上的說法，不給買賣建議。"""
    if not s or s["arrange"] is None:
        return "資料不足，無法判斷"
    below_all = not (s["blue_above"] or s["green_above"] or s["orange_above"])
    if below_all:
        return "價格在三條線下面，三條線都是壓力。書上說：站上小藍才算抄底第一波，沒站上前不要硬去挑戰均線。"
    if s["env"] == "空方環境":
        return "小橘往下彎＝空方環境。書上說：空方環境切忌積極做多。"
    if s["arrange"] == "多頭排列":
        return "小橘往上＋多頭排列＝多方結構。書上說：就算短暫跌破小藍、小綠，只要小橘沒破，多方結構就還在。"
    if s["orange_above"] is False:
        return "價格跌破小橘（老大）。書上說：抄底單除非很快站回小橘，否則不要抱太久。"
    return "小橘往上＝多方環境，但三條線還沒排好，屬於整理階段。"


def ma_block(s, name):
    if not s:
        return [f"**▍{name} 三刀流（60分K）**", f"• {NA}"]
    def line(key, label, n):
        v = s[key]
        if v is None:
            return f"• {label}（{n}）：{NA}"
        arrow = "" if s[key + "_up"] is None else ("↗上揚" if s[key + "_up"] else "↘下彎")
        pos = "✅ 站上" if s[key + "_above"] else "❌ 跌破"
        return f"• {label}（{n}）{v:,.2f} {arrow}｜價格{pos}"
    L = [f"**▍{name} 三刀流（60分K）**", f"• 現價 {s['price']:,.2f}"]
    L += [line(k, lab, n) for k, lab, n in LINES]
    L.append(f"• 排列：{s['arrange'] or NA}")
    L.append(f"• 大環境（看小橘方向）：{s['env'] or NA}")
    if s["cross"]:
        L.append(f"• **藍綠{s['cross']}（這一小時剛發生）**")
    if s["dev_q"] is not None:
        hot = "　⚠ 超過 30%，書上視為過熱、可能拉回" if s["dev_q"] > 30 else ""
        L.append(f"• 日K 乖離：月線 {s['dev_m']:+.1f}%｜季線 {s['dev_q']:+.1f}%{hot}")
    L.append(f"→ {summary(s)}")
    return L


def ma_events(s, prev):
    """跟上一次比，找出值得馬上通知的變化。"""
    if not s or not prev:
        return []
    ev = []
    for key, label, _ in LINES[1:]:  # 小綠、小橘 的站上／跌破
        a, pa = s[key + "_above"], prev.get(key + "_above")
        if a is not None and pa is not None and a != pa:
            ev.append(("站上" if a else "跌破") + label)
    if s["arrange"] and prev.get("arrange") and s["arrange"] != prev["arrange"]:
        ev.append("轉為" + s["arrange"].split("（")[0])
    if s["cross"]:
        ev.append("藍綠" + s["cross"])
    if s["env"] and prev.get("env") and s["env"] != prev["env"]:
        ev.append("小橘轉向→" + s["env"])
    if s["dev_q"] is not None and s["dev_q"] > 30 and not (prev.get("dev_q") or 0) > 30:
        ev.append("季線乖離超過30%")
    return ev


# ---------------- 價格 ----------------
def _chg(a, b):
    return (a / b - 1) * 100 if (a and b) else None


def quote(sym):
    """回傳顯示用字串；失敗回 None。
    指數（^ 開頭）沒有盤前盤後 → 只顯示正盤；期貨 → 相對前一結算；
    個股/ETF：盤中＝即時｜盤後＝今日收盤＋盤後價｜盤前＝盤前價（相對昨收）。"""
    try:
        r = jhttp(YF.format(urllib.parse.quote(sym)))["chart"]["result"][0]
        m = r["meta"]
        closes = [c for c in (r.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c is not None]
        last = closes[-1] if closes else None
        rmp, prev_close = m.get("regularMarketPrice"), m.get("chartPreviousClose") or m.get("previousClose")
        reg = m.get("currentTradingPeriod", {}).get("regular", {})
        now = time.time()
        if sym.startswith("^"):
            return f"{rmp:,.2f} {pct(_chg(rmp, prev_close), 2)}"
        if sym.endswith("=F"):
            p = last or rmp
            return f"{p:,.2f} {pct(_chg(p, prev_close), 2)}"
        if reg.get("start", 0) <= now < reg.get("end", 0):
            p = last or rmp
            return f"{p:,.2f} {pct(_chg(p, prev_close), 2)}"
        if now < reg.get("start", 0):  # 盤前：rmp 為前一交易日收盤
            return f"盤前 {last:,.2f} {pct(_chg(last, rmp), 2)}" if last else f"{rmp:,.2f}（尚無盤前成交）"
        s = f"收 {rmp:,.2f} {pct(_chg(rmp, prev_close), 2)}"  # 盤後
        if last and abs(last - rmp) > 1e-9:
            s += f"｜盤後 {last:,.2f} {pct(_chg(last, rmp), 2)}"
        return s
    except Exception as e:
        print(f"Yahoo {sym} 失敗:", e)
        return None


def fetch_prices():
    return {sym: quote(sym) for sym, _ in MARKET + MAG7}


# ---------------- 新聞標題 ----------------
def fetch_news(seen):
    items = []
    for q in NEWS_QUERIES:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q + " when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        try:
            root = ET.fromstring(http(url))
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                link = it.findtext("link") or ""
                src = (it.findtext("source") or title.rsplit(" - ", 1)[-1]).lower()
                if not any(s in src for s in NEWS_SOURCES):
                    continue
                pub = it.findtext("pubDate") or ""
                key = hashlib.md5(re.sub(r"\s+-\s+[^-]+$", "", title).lower().encode()).hexdigest()[:12]
                try:
                    ts = time.mktime(time.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S"))
                except Exception:
                    ts = 0
                items.append((ts, key, title, link))
        except Exception as e:
            print("News 失敗:", q, e)
    fresh, keys = [], set()
    for ts, key, title, link in sorted(items, reverse=True):
        if key in seen or key in keys:
            continue
        keys.add(key)
        fresh.append({"k": key, "t": title, "l": link})
    return fresh


# ---------------- 組回報 ----------------
def pct(v, nd=1):
    return NA if v is None else f"{v:+.{nd}f}%"


def build(soxl, qqq, prices, news, prev, now_tpe, now_et, events):
    L = [f"**{now_tpe:%m/%d %H:%M} 台北｜美東 {now_et:%H:%M}**", f"上次：{prev.get('time', '無')}", ""]
    if events:
        L += ["**⚠ 變化：" + "、".join(events) + "**", ""]
    L += ma_block(soxl, "SOXL") + [""]
    if qqq:
        q = qqq
        L += ["**▍那指（QQQ）三刀流**",
              f"• 小綠{'站上' if q['green_above'] else '跌破'}｜小橘{'站上' if q['orange_above'] else '跌破'}"
              f"｜{q['arrange'] or NA}｜{q['env'] or NA}" if q["arrange"] else f"• {NA}", ""]

    def pline(group, title):
        out = [f"**▍{title}**"]
        for sym, name in group:
            out.append(f"• {name} {prices.get(sym) or NA}")
        return out
    L += pline(MARKET, "大盤") + [""] + pline(MAG7, "七巨頭")

    if news:
        L += ["", "**▍新消息標題**"]
        for n in news[:NEWS_MAX]:
            L.append(f"• [{n['t'][:110]}]({n['l']})")
    L += ["", "依刀神三刀流書上規則整理，只描述線的狀態，不構成投資建議。"]
    return "\n".join(L)


def trim(text, limit=3900):
    b = text.encode("utf-8")
    if len(b) <= limit:
        return text
    return b[:limit].decode("utf-8", "ignore") + "\n…（過長截斷）"


def push(title, body, urgent=False):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("（沒有 NTFY_TOPIC，略過推播）"); return
    h = {"Title": title.encode("utf-8"), "Markdown": "yes", "Tags": "chart_with_upwards_trend"}
    if urgent:
        h["Priority"] = "high"
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=trim(body).encode("utf-8"), headers=h)
    urllib.request.urlopen(req, timeout=20).read()


def report_window(now_et):
    """美股交易日的 美東 07–19 點（盤前 3 小時＋正盤＋盤後 3 小時）。"""
    if now_et.weekday() > 4 or now_et.date() in HOLIDAYS:
        return False
    return 7 <= now_et.hour <= 19


KEEP = ("blue_above", "green_above", "orange_above", "arrange", "env", "dev_q")


def main():
    now_tpe, now_et = datetime.now(TPE), datetime.now(ET_TZ)
    prev = load_state()
    seen_list = prev.get("seen", [])
    soxl = fetch_ma("SOXL")
    qqq = fetch_ma("QQQ", daily=False)
    prices = fetch_prices()
    news = fetch_news(set(seen_list))
    events = ma_events(soxl, prev.get("soxl"))
    text = build(soxl, qqq, prices, news, prev, now_tpe, now_et, events)

    if "--run" not in sys.argv:
        print(text)
        if "--push" in sys.argv:
            push("SOXL 三刀流回報", text)
        return

    send, urgent, title = report_window(now_et), False, "SOXL 三刀流回報"
    if events:
        send, urgent, title = True, True, "⚠ SOXL " + "、".join(events)
    if soxl is None and prev.get("ma_fail"):
        send, urgent, title = True, True, "⚠ 連續兩小時取不到 SOXL K線資料"

    print(text, f"\n[推播：{'是 ' + title if send else '否'}]")
    if send:
        try:
            push(title, text, urgent)
        except Exception as e:
            print("推播失敗:", e)
        if news:  # 只有真的推播出去的標題才記為已讀
            seen_list.extend(n["k"] for n in news[:NEWS_MAX])

    new = {"time": now_tpe.strftime("%m/%d %H:%M"), "ma_fail": soxl is None, "seen": seen_list[-400:],
           "soxl": {k: soxl[k] for k in KEEP} if soxl else prev.get("soxl")}
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
