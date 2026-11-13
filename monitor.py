#!/usr/bin/env python3
"""科技大盤＋七巨頭＋BTC／黃金 三刀流監控（GitHub Actions 雲端版，推播到 ntfy）。

看線方法（刀神「均線三刀流」，命名同 JARVIS SELECT）：
  張飛 20MA（短、藍）、關羽 60MA（中、綠，多空分水嶺）、劉備 240MA（長、橘）
  多頭排列＝張飛>關羽>劉備；空頭排列＝張飛<關羽<劉備；其他＝糾結（盤整）
  劉備往上＝多方環境、往下＝空方環境（書上：空方環境切忌積極做多）
  1H 看短線、日K 看大方向；另加 海龜突破（日K 20 日高低點）、MACD 柱狀圖、VIX。

推播：
  1. 每小時回報：美股交易日 美東 07–19 點（含結論：依規則偏多／偏空／觀望）
  2. 變化快訊（高優先、不限時段）：結論翻轉、主要標的轉成多頭／空頭排列、指數站上／跌破關羽
  3. 每日一則「未來 7 天行事曆」：美國重要數據（CPI、PPI、非農、FOMC…）＋大型股財報
規則：數字一律從 API 取；取不到就寫「取不到」。結論是規則計算結果，不是投資建議。

本機測試：python monitor.py            → 印出回報（不推播、不寫 state）
          python monitor.py --push     → 立刻推播回報＋行事曆（不寫 state）
          python monitor.py --run      → 排程模式
環境變數 NTFY_TOPIC：ntfy 頻道名稱（GitHub Secret）。
"""
import hashlib, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

ET_TZ, TPE = ZoneInfo("America/New_York"), ZoneInfo("Asia/Taipei")
YF = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval={}&range={}"
DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, "state.json")
NA = "取不到"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36", "Accept": "application/json, text/plain, */*"}

# (Yahoo 代號, 顯示名稱)
INDEX = [("^NDX", "那指100"), ("^SOX", "費半"), ("^GSPC", "標普500")]
OTHER = [("SOXL", "SOXL"), ("BTC-USD", "比特幣"), ("GC=F", "黃金")]
MAG7 = [("NVDA", "輝達"), ("AAPL", "蘋果"), ("MSFT", "微軟"), ("GOOGL", "Google"),
        ("AMZN", "亞馬遜"), ("META", "Meta"), ("TSLA", "特斯拉")]
ALERT_SYMS = [s for s, _ in INDEX + OTHER]
LINES = (("z", "張飛", 20), ("g", "關羽", 60), ("l", "劉備", 240))

# 財報：這些一定列；其他公司市值 ≥ 2000 億美元才列
WATCH_EARN = {s for s, _ in MAG7} | {"AVGO", "TSM", "AMD", "MU", "ASML", "QCOM", "ARM", "INTC", "AMAT", "LRCX",
                                      "KLAC", "MRVL", "SMCI", "ORCL", "CRM", "ADBE", "NFLX", "PLTR", "COST", "WMT"}

NEWS_QUERIES = [
    "Nasdaq OR \"S&P 500\" OR \"semiconductor stocks\" stock market",
    "Nvidia OR TSMC OR \"chip export\" OR \"export controls\"",
    "\"AI regulation\" OR \"AI chips\" restrictions OR tariffs",
    "Fed rate OR CPI OR PPI OR jobs report stocks",
    "Apple OR Microsoft OR Alphabet OR Amazon OR Meta OR Tesla",
    "Bitcoin OR gold price",
]
NEWS_MAX = 5
NEWS_SOURCES = ("reuters", "bloomberg", "cnbc", "wall street journal", "wsj", "marketwatch", "barron",
                "financial times", "yahoo finance", "associated press", "ap news", "investing.com", "benzinga",
                "seeking alpha", "nikkei", "fortune", "business insider", "axios", "the information",
                "techcrunch", "the verge", "forbes", "fox business", "politico", "the new york times",
                "bnn bloomberg", "investopedia", "the motley fool", "digitimes", "tom's hardware", "coindesk",
                "kitco")

# NYSE 休市日
HOLIDAYS = {date(2026, 11, 26), date(2026, 12, 25), date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15),
            date(2027, 3, 26), date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
            date(2027, 11, 25), date(2027, 12, 24)}

# 美國重要數據中文名（ForexFactory 標題 → 中文）
EVENT_ZH = [("Core CPI", "核心 CPI"), ("CPI", "CPI 消費者物價"), ("Core PPI", "核心 PPI"), ("PPI", "PPI 生產者物價"),
            ("Non-Farm", "非農就業"), ("Unemployment Rate", "失業率"), ("FOMC", "FOMC 聯準會"),
            ("Federal Funds Rate", "聯準會利率決議"), ("Core PCE", "核心 PCE"), ("PCE", "PCE 物價"),
            ("Retail Sales", "零售銷售"), ("GDP", "GDP"), ("ISM", "ISM 指數"), ("Fed Chair", "聯準會主席談話"),
            ("Powell", "聯準會主席談話"), ("JOLTS", "職缺 JOLTS"), ("Unemployment Claims", "初領失業金"),
            ("Consumer Sentiment", "消費者信心"), ("Consumer Confidence", "消費者信心")]


def http(url, headers=None):
    h = dict(UA, **(headers or {}))
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=20).read()


def jhttp(url, headers=None):
    return json.loads(http(url, headers))


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------- 指標 ----------------
def candles(sym, interval, rng):
    r = jhttp(YF.format(urllib.parse.quote(sym), interval, rng))["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    rows = [(c, h, l) for c, h, l in zip(q.get("close") or [], q.get("high") or [], q.get("low") or [])
            if None not in (c, h, l)]
    return [x[0] for x in rows], [x[1] for x in rows], [x[2] for x in rows]


def ma(xs, n, back=0):
    xs = xs[:len(xs) - back] if back else xs
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema(xs, n):
    k, out = 2 / (n + 1), []
    for x in xs:
        out.append(x if not out else x * k + out[-1] * (1 - k))
    return out


def three_blades(closes):
    """三刀流狀態：排列、站上幾條、劉備方向、關羽站上與否。"""
    if not closes:
        return None
    p = closes[-1]
    v = {k: ma(closes, n) for k, _, n in LINES}
    s = {"p": p, **v}
    s["above"] = sum(1 for k, _, _ in LINES if v[k] is not None and p > v[k])
    s["g_above"] = None if v["g"] is None else p > v["g"]
    lb = ma(closes, 240, 1)
    s["l_up"] = None if (v["l"] is None or lb is None) else v["l"] > lb
    if None in v.values():
        s["arr"] = None
    elif v["z"] > v["g"] > v["l"]:
        s["arr"] = "多頭"
    elif v["z"] < v["g"] < v["l"]:
        s["arr"] = "空頭"
    else:
        s["arr"] = "糾結"
    return s


def analyze(sym):
    """回傳 dict：h=1H 三刀流、d=日K 三刀流、chg=日漲跌%、turtle、macd、dev_q（季線乖離）。失敗回 None。"""
    try:
        hc, _, _ = candles(sym, "60m", "3mo")
        dc, dh, dl = candles(sym, "1d", "2y")
    except Exception as e:
        print(f"Yahoo {sym} 失敗:", e)
        return None
    a = {"h": three_blades(hc), "d": three_blades(dc), "p": hc[-1] if hc else (dc[-1] if dc else None)}
    a["chg"] = (a["p"] / dc[-2] - 1) * 100 if (a["p"] and len(dc) >= 2) else None
    # 海龜：今天收盤突破前 20 日最高＝突破；跌破前 20 日最低＝破低
    a["turtle"] = None
    if len(dc) > 21:
        hi, lo = max(dh[-21:-1]), min(dl[-21:-1])
        a["turtle"] = "突破" if dc[-1] > hi else ("破低" if dc[-1] < lo else "通道內")
    # MACD(12,26,9) 柱狀圖正負（日K）
    a["macd"] = None
    if len(dc) > 35:
        dif = [x - y for x, y in zip(ema(dc, 12), ema(dc, 26))]
        a["macd"] = dif[-1] - ema(dif, 9)[-1] > 0
    m60 = ma(dc, 60)
    a["dev_q"] = (dh[-1] / m60 - 1) * 100 if (m60 and dh) else None
    return a


def score(a):
    """依三刀流規則打分（-6～+6）。只用書上／網站提到的因素。"""
    if not a:
        return None
    s, h, d = 0, a["h"] or {}, a["d"] or {}
    s += {"多頭": 1, "空頭": -1}.get(h.get("arr"), 0)          # 1H 排列
    s += {True: 1, False: -1}.get(h.get("l_up"), 0)           # 1H 劉備方向（多空環境）
    s += {True: 1, False: -1}.get(h.get("g_above"), 0)        # 1H 關羽＝多空分水嶺
    s += {"多頭": 1, "空頭": -1}.get(d.get("arr"), 0)          # 日K 排列（大方向）
    s += {"突破": 1, "破低": -1}.get(a.get("turtle"), 0)       # 海龜
    s += {True: 1, False: -1}.get(a.get("macd"), 0)           # MACD 動能
    return s


def tag(sc):
    if sc is None:
        return NA
    return "偏多" if sc >= 3 else ("偏空" if sc <= -3 else "觀望")


def verdict(results, vix):
    scores = [score(results.get(s)) for s, _ in INDEX]
    scores = [x for x in scores if x is not None]
    if not scores:
        return None, "大盤資料取不到，無法判斷"
    tot = sum(scores)
    if vix is not None and vix >= 25:
        tot -= 1
    mx = 6 * len(scores)
    if tot >= mx / 3:
        v = "偏多"
        why = "三大指數多數站上關羽、排列偏多。書上：多頭排列下順勢做多，跌破張飛、關羽但劉備沒破，多方結構不算壞。"
    elif tot <= -mx / 3:
        v = "偏空"
        why = "三大指數多數跌破關羽、排列偏空。書上：劉備往下＝空方環境，切忌積極做多；反彈到關羽附近是壓力。"
    else:
        v = "觀望"
        why = "訊號互相矛盾（糾結盤）。網站：三線糾結＝盤整、方向不明；書上：三軍列陣才出手，陣型亂了就收兵。"
    return v, f"**→ 依三刀流規則：{v}（大盤分數 {tot:+d}／±{mx}）**\n{why}"


# ---------------- 價格字串 ----------------
def fmt_price(p):
    return NA if p is None else (f"{p:,.0f}" if p >= 1000 else f"{p:,.2f}")


def pct(v, nd=1):
    return NA if v is None else f"{v:+.{nd}f}%"


def line_full(name, a):
    if not a or not a["h"]:
        return f"• {name} {NA}"
    h, d = a["h"], a["d"] or {}
    parts = [f"{name} {fmt_price(a['p'])} {pct(a['chg'])}",
             f"1H {h['arr'] or NA}·站上{h['above']}/3",
             f"日K {d.get('arr') or NA}",
             f"海龜{a['turtle'] or NA}",
             "MACD" + ({True: "+", False: "−"}.get(a["macd"], NA)),
             f"【{tag(score(a))}】"]
    s = "• " + "｜".join(parts)
    if a["dev_q"] is not None and a["dev_q"] > 30:
        s += f"\n  ⚠ 季線乖離 {a['dev_q']:+.0f}%（書上 >30% 視為過熱）"
    return s


def line_short(name, a):
    if not a or not a["h"]:
        return f"• {name} {NA}"
    h = a["h"]
    return f"• {name} {fmt_price(a['p'])} {pct(a['chg'])}｜{h['arr'] or NA}·站上{h['above']}/3【{tag(score(a))}】"


# ---------------- 新聞 ----------------
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


# ---------------- 行事曆（未來 7 天） ----------------
WD = "一二三四五六日"


def econ_events(now_tpe):
    out = []
    for wk in ("thisweek",):  # ForexFactory 只提供本週；週一的行事曆會涵蓋整週
        try:
            for e in jhttp(f"https://nfs.faireconomy.media/ff_calendar_{wk}.json"):
                if e.get("country") != "USD":
                    continue
                title, high = e.get("title", ""), e.get("impact") == "High"
                if "speaks" in title.lower():  # 官員談話：只收高影響（如主席）
                    if not high:
                        continue
                    zh = "聯準會發言（" + re.sub(r"(?i)\s*(fed chair|fomc member|speaks)\s*", " ", title).strip() + "）"
                else:
                    zh = next((z for k, z in EVENT_ZH if k.lower() in title.lower()), None)
                    if not high and not zh:
                        continue
                t = datetime.fromisoformat(e["date"]).astimezone(TPE)
                if not (now_tpe - timedelta(hours=2) <= t <= now_tpe + timedelta(days=7)):
                    continue
                extra = f"（預估 {e['forecast']}／前值 {e['previous']}）" if e.get("forecast") else ""
                star = "🔴" if e.get("impact") == "High" else "🟠"
                out.append((t, f"{star} {t:%m/%d}({WD[t.weekday()]}) {t:%H:%M} {zh or title}{extra}"))
        except Exception as ex:
            print(f"行事曆 {wk} 失敗:", ex)
    seen, res = set(), []
    for t, s in sorted(out):
        if s not in seen:
            seen.add(s); res.append(s)
    return res


def _cap(s):
    try:
        return float(re.sub(r"[^\d.]", "", s or "") or 0)
    except Exception:
        return 0


def earnings(now_et):
    out, hdr = [], {"Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
    for i in range(8):
        d = (now_et + timedelta(days=i)).date()
        if d.weekday() > 4:
            continue
        try:
            rows = (jhttp(f"https://api.nasdaq.com/api/calendar/earnings?date={d}", hdr).get("data") or {}).get("rows") or []
        except Exception as ex:
            print(f"財報 {d} 失敗:", ex)
            continue
        for r in rows:
            sym = r.get("symbol", "")
            if sym in WATCH_EARN or _cap(r.get("marketCap")) >= 2e11:
                when = {"time-pre-market": "盤前", "time-after-hours": "盤後"}.get(r.get("time"), "")
                out.append(f"• {d:%m/%d}({WD[d.weekday()]}) {sym} {when}".rstrip())
    return out


def build_calendar(now_tpe, now_et):
    ev, ea = econ_events(now_tpe), earnings(now_et)
    L = [f"**📅 未來 7 天美股行事曆（{now_tpe:%m/%d} 起，台北時間）**", "", "**▍本週重要數據／聯準會**"]
    L += ev or [f"• {NA}（或本週沒有重大數據）"]
    L += ["", "**▍大型股財報**（美東日期）"]
    L += ea or [f"• {NA}（或這 7 天沒有大型股財報）"]
    L += ["", "🔴＝高影響。數據公布前後波動通常放大；個別事件（如輝達新聞）見每小時回報的新聞標題。"]
    return "\n".join(L)


# ---------------- 組回報 ----------------
def build(res, vix, news, prev, now_tpe, now_et, events):
    v, vtxt = verdict(res, vix)
    L = [f"**{now_tpe:%m/%d %H:%M} 台北｜美東 {now_et:%H:%M}**", ""]
    if events:
        L += ["**⚠ 變化：" + "、".join(events) + "**", ""]
    L += [vtxt, ""]
    L += ["**▍大盤三刀流**（張飛20/關羽60/劉備240）"]
    L += [line_full(n, res.get(s)) for s, n in INDEX]
    L.append("• VIX 恐慌 " + (NA if vix is None else f"{vix:.2f}" + ("（>25 恐慌）" if vix >= 25 else
                                                                   "（20–25 偏高）" if vix >= 20 else "（平靜）")))
    L += ["", "**▍SOXL／比特幣／黃金**"] + [line_full(n, res.get(s)) for s, n in OTHER]
    L += ["", "**▍七巨頭**（1H）"] + [line_short(n, res.get(s)) for s, n in MAG7]
    if news:
        L += ["", "**▍新消息**"] + [f"• [{n['t'][:90]}]({n['l']})" for n in news[:NEWS_MAX]]
    L += ["", "看法：站上x/3＝價格站上幾條均線；【偏多/偏空/觀望】＝1H排列、劉備方向、關羽、日K排列、海龜、MACD 六項加總。"
              "規則計算結果，不是投資建議。"]
    return "\n".join(L), v


def detect(res, prev, v):
    ev = []
    pv = prev.get("verdict")
    if v and pv and v != pv:
        ev.append(f"大盤結論 {pv}→{v}")
    names = dict(INDEX + OTHER)
    ps = prev.get("assets", {})
    for s in ALERT_SYMS:
        a, p = res.get(s), ps.get(s)
        if not a or not a["h"] or not p:
            continue
        arr, parr = a["h"]["arr"], p.get("arr")
        if arr in ("多頭", "空頭") and parr and arr != parr:
            ev.append(f"{names[s]}轉{arr}排列")
        if s in dict(INDEX) or s == "SOXL":
            ga, pga = a["h"]["g_above"], p.get("g_above")
            if ga is not None and pga is not None and ga != pga:
                ev.append(f"{names[s]}{'站上' if ga else '跌破'}關羽")
    return ev


def trim(text, limit=3900):
    b = text.encode("utf-8")
    return text if len(b) <= limit else b[:limit].decode("utf-8", "ignore") + "\n…（過長截斷）"


def push(title, body, urgent=False, tags="chart_with_upwards_trend"):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("（沒有 NTFY_TOPIC，略過推播）"); return
    h = {"Title": title.encode("utf-8"), "Markdown": "yes", "Tags": tags}
    if urgent:
        h["Priority"] = "high"
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=trim(body).encode("utf-8"), headers=h)
    urllib.request.urlopen(req, timeout=20).read()


def trading_day(now_et):
    return now_et.weekday() <= 4 and now_et.date() not in HOLIDAYS


def report_window(now_et):
    """美股交易日的 美東 07–19 點（盤前 3 小時＋正盤＋盤後 3 小時）。"""
    return trading_day(now_et) and 7 <= now_et.hour <= 19


def main():
    now_tpe, now_et = datetime.now(TPE), datetime.now(ET_TZ)
    prev = load_state()
    seen_list = prev.get("seen", [])
    res = {s: analyze(s) for s, _ in INDEX + OTHER + MAG7}
    try:
        vix = candles("^VIX", "1d", "5d")[0][-1]
    except Exception as e:
        print("VIX 失敗:", e); vix = None
    news = fetch_news(set(seen_list))
    _, v = build(res, vix, [], prev, now_tpe, now_et, [])
    events = detect(res, prev, v)
    shown = news[:NEWS_MAX]
    text, v = build(res, vix, shown, prev, now_tpe, now_et, events)
    while len(text.encode("utf-8")) > 3900 and shown:  # ntfy 上限約 4KB，太長就少放幾則新聞
        shown = shown[:-1]
        text, v = build(res, vix, shown, prev, now_tpe, now_et, events)
    news = shown

    if "--run" not in sys.argv:
        print(text)
        cal = build_calendar(now_tpe, now_et)
        print("\n" + cal)
        if "--push" in sys.argv:
            push(f"美股三刀流：{v or NA}", text)
            push("📅 未來 7 天美股行事曆", cal, tags="calendar")
        return

    send, urgent, title = report_window(now_et), False, f"美股三刀流：{v or NA}"
    if events:
        send, urgent, title = True, True, "⚠ " + "、".join(events)[:80]
    fail = all(res[s] is None for s, _ in INDEX)
    if fail and prev.get("fail"):
        send, urgent, title = True, True, "⚠ 連續兩小時取不到大盤 K 線資料"

    print(text, f"\n[推播：{'是 ' + title if send else '否'}]")
    if send:
        try:
            push(title, text, urgent)
        except Exception as e:
            print("推播失敗:", e)
        if news:
            seen_list.extend(n["k"] for n in news[:NEWS_MAX])

    cal_day = prev.get("cal_day")
    if trading_day(now_et) and 7 <= now_et.hour and cal_day != str(now_et.date()):
        try:
            push("📅 未來 7 天美股行事曆", build_calendar(now_tpe, now_et), tags="calendar")
            cal_day = str(now_et.date())
        except Exception as e:
            print("行事曆推播失敗:", e)

    assets = dict(prev.get("assets", {}))
    for s in ALERT_SYMS:
        a = res.get(s)
        if a and a["h"]:
            assets[s] = {"arr": a["h"]["arr"], "g_above": a["h"]["g_above"]}
    new = {"time": now_tpe.strftime("%m/%d %H:%M"), "fail": fail, "seen": seen_list[-400:],
           "verdict": v or prev.get("verdict"), "assets": assets, "cal_day": cal_day}
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
