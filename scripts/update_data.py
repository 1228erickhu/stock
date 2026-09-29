#!/usr/bin/env python3
"""每天盤後從證交所、櫃買中心抓全市場日K，整理成網站要用的 latest.json。
用法：python update_data.py <輸出資料夾> <舊的 history.json>
只用 Python 內建模組，不需要 pip install。
"""
import datetime as dt
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TZ = dt.timezone(dt.timedelta(hours=8))
KEEP = 61          # 保留幾個交易日（60 日前高 + 今天）
PAUSE = 3          # 每次請求間隔秒數，避免被證交所擋
OUT = sys.argv[1] if len(sys.argv) > 1 else "out"
HIST_IN = sys.argv[2] if len(sys.argv) > 2 else "history.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (stock-check updater)", "Accept": "application/json"}
NOW = dt.datetime.now(TZ)
TODAY = NOW.date()


DEAD = set()        # 本次執行中連不上的網站，之後直接略過
FAILS = {}
START = time.time()
BUDGET = 20 * 60    # 最多花 20 分鐘抓資料，超過就先存檔，下次再補


def get(url):
    """抓 JSON；連不上的網站連續失敗兩次就放棄，避免卡住整個流程。"""
    host = urllib.parse.urlparse(url).netloc
    if host in DEAD:
        return None
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(2):
        try:
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read()
            except urllib.error.HTTPError:
                raise
            except (ssl.SSLError, urllib.error.URLError) as e:
                if "CERTIFICATE" not in str(e).upper():
                    raise
                ctx = ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
                    raw = r.read()
            FAILS[host] = 0
            txt = raw.decode("utf-8-sig", errors="replace").strip()
            if not txt or txt[0] not in "[{":
                return None
            return json.loads(txt)
        except urllib.error.HTTPError as e:
            print(f"  {host} 回應 HTTP {e.code}：{url}")
            if e.code in (401, 403, 429):
                DEAD.add(host)
                print(f"  → {host} 拒絕連線，本次不再嘗試")
            return None
        except ValueError as e:
            print(f"  資料格式看不懂：{url} → {e}")
            return None
        except Exception as e:  # noqa: BLE001  連線逾時、DNS 失敗等
            print(f"  第 {attempt + 1} 次連線失敗：{url} → {e}")
            time.sleep(3)
    FAILS[host] = FAILS.get(host, 0) + 1
    if FAILS[host] >= 2:
        DEAD.add(host)
        print(f"  → {host} 連續連不上，本次不再嘗試")
    return None


def num(x):
    if x is None:
        return None
    s = re.sub(r"<[^>]+>", "", str(x)).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def clean(x):
    return re.sub(r"<[^>]+>", "", str(x)).strip()


def pick(fields, *names):
    fields = [clean(f) for f in fields]
    for n in names:
        if n in fields:
            return fields.index(n)
    for n in names:
        for i, f in enumerate(fields):
            if n in f:
                return i
    return None


def tables(js):
    """從證交所／櫃買的舊版與新版 JSON 裡找出 (欄位, 資料) 表格。"""
    out = []
    if not isinstance(js, dict):
        return out
    for t in js.get("tables") or []:
        if isinstance(t, dict) and t.get("fields") and t.get("data"):
            out.append((t["fields"], t["data"]))
    for k, v in js.items():
        if k.startswith("fields") and isinstance(v, list):
            d = js.get("data" + k[6:])
            if isinstance(d, list) and d:
                out.append((v, d))
    if isinstance(js.get("aaData"), list) and js["aaData"]:
        out.append((None, js["aaData"]))  # 櫃買舊格式，欄位順序固定
    return out


def parse_tables(js, market):
    res = {}
    for fields, data in tables(js):
        if fields is None:
            idx = dict(code=0, name=1, c=2, o=4, h=5, l=6, v=8)
        else:
            idx = dict(
                code=pick(fields, "證券代號", "代號"), name=pick(fields, "證券名稱", "名稱"),
                o=pick(fields, "開盤價", "開盤"), h=pick(fields, "最高價", "最高"),
                l=pick(fields, "最低價", "最低"), c=pick(fields, "收盤價", "收盤"),
                v=pick(fields, "成交股數"),
            )
            if None in idx.values():
                continue
        for r in data:
            try:
                row = add_row(res, clean(r[idx["code"]]), clean(r[idx["name"]]), market,
                              r[idx["o"]], r[idx["h"]], r[idx["l"]], r[idx["c"]], r[idx["v"]])
            except (IndexError, TypeError, KeyError):
                continue
    return res


def add_row(res, code, name, market, o, h, l, c, v):
    if not re.fullmatch(r"\d{4}", code):
        return
    c = num(c)
    if not c:
        return
    res[code] = dict(n=name, m=market, o=num(o) or c, h=num(h) or c, l=num(l) or c, c=c,
                     v=round((num(v) or 0) / 1000))


def norm_date(s):
    """把 1150929、115/09/29、20260929、2026/09/29 都轉成 date。"""
    digits = re.sub(r"\D", "", str(s or ""))
    try:
        if len(digits) == 8:
            return dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
        if len(digits) == 7:
            return dt.date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:]))
    except ValueError:
        pass
    return None


def report_date_ok(js, d):
    """回應裡有標日期時，確認是我們要的那一天（避免假日回傳前一天資料）。"""
    if not isinstance(js, dict):
        return True
    for k in ("date", "reportDate", "Date"):
        if js.get(k):
            got = norm_date(js[k])
            if got and got != d:
                return False
    return True


def fetch_twse(d):
    for url in (
        f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}&type=ALLBUT0999&response=json",
        f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={d:%Y%m%d}&type=ALLBUT0999",
    ):
        js = get(url)
        time.sleep(PAUSE)
        if js and report_date_ok(js, d):
            res = parse_tables(js, "TWSE")
            if res:
                return res
    if d == TODAY:  # 最後備援：開放資料平台（只有最新一天）
        js = get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
        res = {}
        if isinstance(js, list) and js and norm_date(js[0].get("Date")) in (d, None):
            for r in js:
                add_row(res, str(r.get("Code", "")).strip(), str(r.get("Name", "")).strip(), "TWSE",
                        r.get("OpeningPrice"), r.get("HighestPrice"), r.get("LowestPrice"),
                        r.get("ClosingPrice"), r.get("TradeVolume"))
        return res
    return {}


def fetch_tpex(d):
    roc = f"{d.year - 1911}/{d:%m/%d}"
    for url in (
        f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQ?date={d:%Y/%m/%d}&type=EW&response=json",
        f"https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&d={roc}&se=EW&o=json",
    ):
        js = get(url)
        time.sleep(PAUSE)
        if js and report_date_ok(js, d):
            res = parse_tables(js, "TPEx")
            if res:
                return res
    if d == TODAY:
        js = get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
        res = {}
        if isinstance(js, list) and js and norm_date(js[0].get("Date")) in (d, None):
            for r in js:
                add_row(res, str(r.get("SecuritiesCompanyCode", "")).strip(),
                        str(r.get("CompanyName", "")).strip(), "TPEx",
                        r.get("Open"), r.get("High"), r.get("Low"), r.get("Close"), r.get("TradingShares"))
        return res
    return {}


def fetch_disposal():
    """目前處置中的股票：{代號: 說明}。格式不確定，盡量寬鬆解析，失敗就回傳空的。"""
    found = {}
    for url in ("https://openapi.twse.com.tw/v1/announcement/punish",
                "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"):
        js = get(url)
        time.sleep(PAUSE)
        if not isinstance(js, list):
            continue
        for item in js:
            if not isinstance(item, dict):
                continue
            code = None
            for k, v in item.items():
                if ("Code" in k or "代號" in k) and re.fullmatch(r"\d{4}", str(v).strip()):
                    code = str(v).strip()
            if not code:
                continue
            period = ""
            for k, v in item.items():
                ds = re.findall(r"\d{2,4}[/.-]?\d{2}[/.-]?\d{2}", str(v))
                if len(ds) >= 2:
                    period = str(v).strip()
                    end = norm_date(ds[-1])
                    if end and end < TODAY:
                        code = None
                    break
            if code:
                found[code] = f"處置期間 {period}" if period else "近期公告處置"
    return found


def main():
    sys.stdout.reconfigure(line_buffering=True)  # 讓 GitHub 的記錄即時顯示
    try:
        with open(HIST_IN, encoding="utf-8") as f:
            hist = json.load(f)
    except (OSError, ValueError):
        hist = {}
    dates = set(hist.get("dates", []))
    stocks = hist.get("s", {})

    # 要抓的日期：資料不夠就往回補 100 天，夠了只補最近 10 天
    look = 100 if len(dates) < 40 else 10
    cands = [TODAY - dt.timedelta(days=i) for i in range(look)]
    cands = [d for d in cands if d.weekday() < 5 and (d.isoformat() not in dates or d == TODAY)]
    if NOW.hour < 14 and TODAY in cands:
        cands.remove(TODAY)

    print(f"已有 {len(dates)} 個交易日，這次要檢查 {len(cands)} 天")
    for d in cands:  # 由新到舊
        if time.time() - START > BUDGET:
            print("時間到了，先把抓到的存起來，下次執行再繼續往回補")
            break
        if "www.twse.com.tw" in DEAD and d != TODAY:
            print("證交所主網站連不上，無法往回補歷史，改成每天累積")
            break
        iso = d.isoformat()
        if len(dates) >= KEEP and iso not in dates and dates and iso < min(dates):
            break
        print(f"抓 {iso} …")
        tw = fetch_twse(d)
        if not tw:
            print("  上市沒有資料（假日或還沒公布），跳過")
            continue
        tp = fetch_tpex(d)
        print(f"  上市 {len(tw)} 檔、上櫃 {len(tp)} 檔")
        for code, r in {**tw, **tp}.items():
            s = stocks.setdefault(code, {"r": {}})
            s["n"], s["m"] = r["n"], r["m"]
            s["r"][iso] = [r["o"], r["h"], r["l"], r["c"], r["v"]]
        dates.add(iso)

    if not dates:
        sys.exit(f"一天的資料都沒抓到。連不上的網站：{', '.join(sorted(DEAD)) or '無'}。請把這段記錄截圖回報。")

    keep = sorted(dates)[-KEEP:]
    keep_set = set(keep)
    for code in list(stocks):
        stocks[code]["r"] = {k: v for k, v in stocks[code]["r"].items() if k in keep_set}
        if not stocks[code]["r"]:
            del stocks[code]

    disp = fetch_disposal()
    rd = lambda x: None if x is None else round(x, 2)  # noqa: E731
    avg = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731

    latest = {}
    for code, s in stocks.items():
        days = sorted(s["r"])
        rows = [s["r"][d] for d in days]
        o, h, l, c, v = rows[-1]
        prev = rows[:-1]
        item = {
            "d": days[-1], "n": s["n"], "m": s["m"], "o": o, "h": h, "l": l, "c": c, "v": v,
            "pc": prev[-1][3] if prev else None,
            "v5p": rd(avg([r[4] for r in prev[-5:]])),
            "v5": rd(avg([r[4] for r in rows[-5:]])),
            "ma5p": rd(avg([r[3] for r in prev[-5:]])) if len(prev) >= 5 else None,
            "ma5": rd(avg([r[3] for r in rows[-5:]])) if len(rows) >= 5 else None,
            "hp": max((r[1] for r in prev[-60:]), default=None),
            "hi": max(r[1] for r in rows[-60:]),
            "k": len(rows),
        }
        if code in disp:
            item["x"] = disp[code]
        latest[code] = item

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "latest.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": NOW.isoformat(timespec="minutes"), "date": keep[-1], "days": len(keep),
                   "s": latest}, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT, "history.json"), "w", encoding="utf-8") as f:
        json.dump({"dates": keep, "s": stocks}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"完成：{len(latest)} 檔，資料到 {keep[-1]}，共 {len(keep)} 個交易日，處置股 {len(disp)} 檔")


if __name__ == "__main__":
    main()
