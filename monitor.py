#!/usr/bin/env python3
"""SOXL 資金費率＋美股大盤監控（GitHub Actions 雲端版，推播到 ntfy）。

每小時 :03 由 GitHub Actions 執行（GitHub 排程可能延遲 5–15 分鐘）。
推播時段（以美東時間計，夏令/冬令自動換算）：
  盤前  美東 07:xx、08:xx、09:xx   （台北冬令 20:03、21:03、22:03）
  正盤  美東 10:xx–16:xx            （台北冬令 23:03–05:03）
  盤後  美東 17:xx、18:xx、19:xx    （台北冬令 06:03、07:03、08:03）
  僅限美股交易日；其他時間只在四格檢查表翻紅時推播。
規則：數字一律從 API 取；取不到就寫「取不到」，不用上次的值冒充。不給買賣建議。

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
HL = "https://api.hyperliquid.xyz/info"
PX = "https://api.pionex.com/api/v1/market/fundingRates?symbol={}&limit=1"
YF = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=5m&range=1d&includePrePost=true"
ASSET = "xyz:SOXL"
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


# ---------------- 資金費率 ----------------
def fetch_funding():
    d = {"funding_ann": None, "premium": None, "oi": None, "mark": None, "realized": None,
         "soxlx_ann": None, "soxsx_ann": None, "soxsx_8h": None}
    try:
        meta, ctxs = jhttp(HL, {"type": "metaAndAssetCtxs", "dex": "xyz"})
        i = next(k for k, u in enumerate(meta["universe"]) if u["name"] == ASSET)
        c = ctxs[i]
        d["funding_ann"] = float(c["funding"]) * 24 * 365 * 100
        d["premium"] = float(c["premium"]) * 100
        d["oi"] = float(c["openInterest"])
        d["mark"] = float(c["markPx"])
    except Exception as e:
        print("Hyperliquid ctx 失敗:", e)
    try:
        hist = jhttp(HL, {"type": "fundingHistory", "coin": ASSET, "startTime": int(time.time() * 1000) - 3 * 3600 * 1000})
        rows = sorted(hist, key=lambda h: h["time"])[-3:]
        if rows:
            d["realized"] = [[datetime.fromtimestamp(h["time"] / 1000, TPE).strftime("%H:%M"),
                              float(h["fundingRate"]) * 100] for h in rows]
    except Exception as e:
        print("Hyperliquid history 失敗:", e)
    for sym, key in (("SOXLX_USDT_PERP", "soxlx"), ("SOXSX_USDT_PERP", "soxsx")):
        try:
            v = float(jhttp(PX.format(sym))["data"]["rates"][0]["fundingRate"]) * 100
            d[key + "_ann"] = v * 3 * 365
            if key == "soxsx":
                d["soxsx_8h"] = v
        except Exception as e:
            print(f"Pionex {sym} 失敗:", e)
    return d


# ---------------- 價格 ----------------
def quote(sym):
    """回傳 (最新價, 相對前一正盤收盤 %, 是否為盤前盤後價)；失敗回 None。"""
    try:
        r = jhttp(YF.format(urllib.parse.quote(sym)))["chart"]["result"][0]
        m = r["meta"]
        closes = [c for c in (r.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c is not None]
        last = closes[-1] if closes else m.get("regularMarketPrice")
        tp = m.get("currentTradingPeriod", {})
        now = time.time()
        reg = tp.get("regular", {})
        in_reg = reg.get("start", 0) <= now < reg.get("end", 0)
        if in_reg or sym.endswith("=F"):
            base = m.get("chartPreviousClose") or m.get("previousClose")
        elif now < reg.get("start", 0):          # 盤前：基準 = 昨天正盤收盤
            base = m.get("regularMarketPrice")
            # range=1d 在盤前時 regularMarketPrice 即前一交易日收盤
        else:                                     # 盤後：基準 = 今天正盤收盤
            base = m.get("regularMarketPrice")
        chg = (last / base - 1) * 100 if (last and base) else None
        return last, chg, (not in_reg and not sym.endswith("=F"))
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


def oi_str(v):
    return NA if v is None else f"{v / 10000:,.1f}萬口"


def build(d, prices, news, prev, now_tpe, now_et):
    oi_chg = (d["oi"] / prev["oi"] - 1) * 100 if (d["oi"] is not None and prev.get("oi")) else None
    checks = {
        "費率為正": None if d["funding_ann"] is None else d["funding_ann"] > 0,
        "OI 無驟減15%": None if oi_chg is None else oi_chg > -15,
        "SOXS 費率為負": None if d["soxsx_8h"] is None else d["soxsx_8h"] < 0,
    }
    red = [k for k, v in checks.items() if v is False]
    na = [k for k, v in checks.items() if v is None]
    verdict = "減 1/3" if red else ("無法判定（有欄位取不到）" if na else "續抱")

    has_prev = bool(prev.get("time"))
    pv = lambda k, nd=0: pct(prev.get(k), nd) if has_prev else NA
    pr = prev.get("realized")
    real = NA if not d["realized"] else "  ".join(f"{t} {v:+.4f}%" for t, v in d["realized"])
    mark = NA if d["mark"] is None else f"{d['mark']:.2f}"
    oi_now = oi_str(d["oi"]) + (f"({oi_chg:+.1f}%)" if oi_chg is not None else "")

    L = [f"**{now_tpe:%m/%d %H:%M} 台北｜美東 {now_et:%H:%M}**", f"上次：{prev.get('time', '無')}", "",
         "**▍SOXL 資金費率**（Hyperliquid 標記價 " + mark + "）",
         f"• 預估年化 {pct(d['funding_ann'], 0)}｜上次 {pv('funding_ann')}",
         f"• 最近實收 {real}｜上次 " + (f"{pr[-1][0]} {pr[-1][1]:+.4f}%" if pr else NA),
         f"• OI {oi_now}｜上次 {oi_str(prev.get('oi')) if has_prev else NA}",
         f"• 溢價 {pct(d['premium'], 2)}｜上次 {pv('premium', 2)}",
         f"• Pionex SOXLX 年化 {pct(d['soxlx_ann'], 0)}｜上次 {pv('soxlx_ann')}",
         f"• Pionex SOXSX 年化 {pct(d['soxsx_ann'], 0)}｜上次 {pv('soxsx_ann')}",
         "", "**▍四格檢查表**"]
    for k, v in checks.items():
        L.append(f"• {k}：{NA}" if v is None else (f"• {k}：🟢" if v else f"• **{k}：🔴 翻紅**"))
    L += ["• 陣型：＿＿（自己填）", f"**→ {verdict}**" if red else f"→ {verdict}", ""]

    def pline(group, title):
        out = [f"**▍{title}**（相對前一正盤收盤）"]
        for sym, name in group:
            q = prices.get(sym)
            if not q or q[0] is None:
                out.append(f"• {name} {NA}")
            else:
                tag = "（盤前/盤後）" if q[2] else ""
                out.append(f"• {name} {q[0]:,.2f} {pct(q[1], 2)}{tag}")
        return out
    L += pline(MARKET, "大盤") + [""] + pline(MAG7, "七巨頭")

    if news:
        L += ["", "**▍新消息標題**"]
        for n in news[:NEWS_MAX]:
            L.append(f"• [{n['t'][:110]}]({n['l']})")
    L += ["", "僅回報數字與檢查表，不構成投資建議。"]
    return "\n".join(L), checks


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


def main():
    now_tpe, now_et = datetime.now(TPE), datetime.now(ET_TZ)
    prev = load_state()
    seen_list = prev.get("seen", [])
    seen = set(seen_list)
    d = fetch_funding()
    prices = fetch_prices()
    news = fetch_news(seen)
    text, checks = build(d, prices, news, prev, now_tpe, now_et)

    if "--run" not in sys.argv:
        print(text)
        if "--push" in sys.argv:
            push("SOXL／美股 回報", text)
        return

    prev_checks = prev.get("checks", {})
    turned_red = [k for k, v in checks.items() if v is False and prev_checks.get(k) is not False]
    all_fail = all(d[k] is None for k in ("funding_ann", "oi", "soxsx_8h"))
    send, urgent, title = False, False, "SOXL／美股 回報"
    if report_window(now_et):
        send = True
    if turned_red:
        send, urgent, title = True, True, "⚠ 檢查表翻紅：" + "、".join(turned_red)
    if all_fail and prev.get("all_fail"):
        send, urgent, title = True, True, "⚠ 連續兩小時取不到資金費率資料"

    print(text, f"\n[推播：{'是 ' + title if send else '否'}]")
    if send:
        try:
            push(title, text, urgent)
        except Exception as e:
            print("推播失敗:", e)
        if news:  # 只有真的推播出去的標題才記為已讀
            seen_list.extend(n["k"] for n in news[:NEWS_MAX])

    new = {"time": now_tpe.strftime("%m/%d %H:%M"), "checks": checks, "all_fail": all_fail,
           "seen": seen_list[-400:]}
    for k in ("funding_ann", "premium", "oi", "realized", "soxlx_ann", "soxsx_ann"):
        new[k] = d[k]
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
