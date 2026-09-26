#!/usr/bin/env python3
"""
Daily end-of-day price updater for the portfolio tracker.

Runs on GitHub Actions (free). Uses only official exchange files:
  * NSE equity bhavcopy (UDiFF):  nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
  * NSE equity bhavcopy (legacy fallback): nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
  * BSE equity bhavcopy (UDiFF):  www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV
  * NSE index closing values:     nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv

It reads data/portfolio.json to learn which stocks are held (and from which
date), downloads only the days that are still missing, and writes:
  * data/prices.json   closing prices of held stocks + benchmark indices
  * data/symbols.json  list of all NSE/BSE codes and names (for lookup in the site)

Only the Python standard library is used, so nothing needs installing.
"""

import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
PORTFOLIO_FILE = os.path.join(DATA, "portfolio.json")
PRICES_FILE = os.path.join(DATA, "prices.json")
SYMBOLS_FILE = os.path.join(DATA, "symbols.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Benchmark indices kept every day (names as NSE prints them, upper-cased).
INDICES = [
    "NIFTY MICROCAP 250", "NIFTY SMALLCAP 250", "NIFTY SMALLCAP 100",
    "NIFTY MIDCAP 150", "NIFTY MIDCAP 100", "NIFTY MIDSMALLCAP 400",
    "NIFTY LARGEMIDCAP 250", "NIFTY 50", "NIFTY NEXT 50", "NIFTY 100",
    "NIFTY 200", "NIFTY 500", "NIFTY TOTAL MARKET",
]
# NSE series that are listed shares / units (EQ = normal, BE/BZ = trade-for-trade,
# SM/ST/SZ = SME platform, RR = REIT, IV = InvIT)
NSE_SERIES_PREF = ["EQ", "BE", "BZ", "SM", "ST", "SZ", "RR", "IV"]

MAX_BACKFILL_DAYS = 3 * 366  # safety limit per run
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class NotPublished(Exception):
    """File does not exist (holiday, or not published yet)."""


class FetchError(Exception):
    """Network / blocking problem. Try again on the next run."""


def log(*a):
    print(*a, flush=True)


def http_get(url, referer):
    last = None
    for attempt in range(3):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        })
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotPublished(url)
            last = FetchError(f"HTTP {e.code} for {url}")
        except Exception as e:  # timeouts, resets
            last = FetchError(f"{type(e).__name__}: {e} for {url}")
        time.sleep(2 * (attempt + 1))
    raise last


def num(x):
    try:
        v = float(str(x).strip().replace(",", ""))
        return v if v > 0 else None
    except ValueError:
        return None


def clean_rows(text):
    rdr = csv.reader(io.StringIO(text))
    rows = [[c.strip() for c in row] for row in rdr if row]
    if not rows:
        return [], []
    return rows[0], rows[1:]


# ---------------------------------------------------------------- sources

def fetch_index_file(d):
    url = ("https://nsearchives.nseindia.com/content/indices/ind_close_all_"
           f"{d:%d%m%Y}.csv")
    raw = http_get(url, "https://www.nseindia.com/")
    text = raw.decode("utf-8", "replace")
    head, rows = clean_rows(text)
    if not head or "Index Name" not in head[0]:
        raise NotPublished(url)
    hi = {h: i for i, h in enumerate(head)}
    ci = hi.get("Closing Index Value")
    out = {}
    for r in rows:
        name = r[0].strip().upper()
        if name in INDICES and ci is not None and ci < len(r):
            v = num(r[ci])
            if v:
                out["IDX:" + name] = v
    if not out:
        raise NotPublished(url)
    return out


def parse_udiff(text):
    head, rows = clean_rows(text)
    hi = {h: i for i, h in enumerate(head)}
    need = ["TckrSymb", "ClsPric"]
    if any(n not in hi for n in need):
        return None, None, None
    return head, hi, rows


def fetch_nse(d):
    """Returns {symbol: (close, name)} for NSE listed shares on day d."""
    url = ("https://nsearchives.nseindia.com/content/cm/"
           f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
    try:
        raw = http_get(url, "https://www.nseindia.com/")
        if raw[:2] != b"PK":
            raise NotPublished(url)
        z = zipfile.ZipFile(io.BytesIO(raw))
        text = z.read(z.namelist()[0]).decode("utf-8", "replace")
        head, hi, rows = parse_udiff(text)
        if head is None:
            raise NotPublished(url)
        best = {}
        for r in rows:
            if len(r) < len(head):
                continue
            sym = r[hi["TckrSymb"]].upper()
            ser = r[hi["SctySrs"]].upper() if "SctySrs" in hi else "EQ"
            if ser not in NSE_SERIES_PREF:
                continue
            px = num(r[hi["ClsPric"]])
            if not px:
                continue
            name = r[hi["FinInstrmNm"]] if "FinInstrmNm" in hi else sym
            rank = NSE_SERIES_PREF.index(ser)
            if sym not in best or rank < best[sym][2]:
                best[sym] = (px, name, rank)
        if best:
            return {k: (v[0], v[1]) for k, v in best.items()}
        raise NotPublished(url)
    except NotPublished:
        pass
    # Fallback: legacy full bhavcopy (no company names)
    url2 = ("https://nsearchives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{d:%d%m%Y}.csv")
    raw = http_get(url2, "https://www.nseindia.com/")
    head, rows = clean_rows(raw.decode("utf-8", "replace"))
    hi = {h.upper(): i for i, h in enumerate(head)}
    if "SYMBOL" not in hi or "CLOSE_PRICE" not in hi:
        raise NotPublished(url2)
    best = {}
    for r in rows:
        sym = r[hi["SYMBOL"]].upper()
        ser = r[hi["SERIES"]].upper()
        if ser not in NSE_SERIES_PREF:
            continue
        px = num(r[hi["CLOSE_PRICE"]])
        if not px:
            continue
        rank = NSE_SERIES_PREF.index(ser)
        if sym not in best or rank < best[sym][2]:
            best[sym] = (px, sym, rank)
    if not best:
        raise NotPublished(url2)
    return {k: (v[0], v[1]) for k, v in best.items()}


def fetch_bse(d):
    """Returns {scrip_code: (close, name, bse_symbol)} for BSE on day d."""
    url = ("https://www.bseindia.com/download/BhavCopy/Equity/"
           f"BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV")
    raw = http_get(url, "https://www.bseindia.com/")
    head, hi, rows = parse_udiff(raw.decode("utf-8", "replace"))
    if head is None or "FinInstrmId" not in hi:
        raise NotPublished(url)
    out = {}
    for r in rows:
        if len(r) < len(head):
            continue
        if "FinInstrmTp" in hi and r[hi["FinInstrmTp"]].upper() not in ("STK", ""):
            continue
        code = r[hi["FinInstrmId"]].strip()
        px = num(r[hi["ClsPric"]])
        if not code or not px:
            continue
        name = r[hi["FinInstrmNm"]] if "FinInstrmNm" in hi else code
        out[code] = (px, name, r[hi["TckrSymb"]].upper())
    if not out:
        raise NotPublished(url)
    return out


# ---------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def parse_date(s):
    return dt.date.fromisoformat(str(s)[:10])


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


def wanted_keys(portfolio):
    """{key: earliest date needed}"""
    keys = {}
    earliest = None
    for p in portfolio.get("portfolios", []):
        for f in p.get("flows", []):
            d = parse_date(f["date"])
            earliest = d if earliest is None or d < earliest else earliest
        for t in p.get("trades", []):
            d = parse_date(t["date"])
            k = f'{t["exch"].upper()}:{str(t["code"]).strip().upper()}'
            if k not in keys or d < keys[k]:
                keys[k] = d
            earliest = d if earliest is None or d < earliest else earliest
    if earliest is not None:
        start = earliest - dt.timedelta(days=10)
        for name in INDICES:
            keys["IDX:" + name] = start
    return keys


def main():
    probe = "--probe" in sys.argv
    now_ist = dt.datetime.now(IST)
    today = now_ist.date()
    log(f"Run at {now_ist:%Y-%m-%d %H:%M} IST")

    portfolio = load_json(PORTFOLIO_FILE, {"portfolios": []})
    prices = load_json(PRICES_FILE, {})
    prices.setdefault("series", {})
    prices.setdefault("cov", {})
    prices.setdefault("holidays", [])
    holidays = set(prices["holidays"])
    symbols = load_json(SYMBOLS_FILE, {})

    keys = wanted_keys(portfolio)
    if probe and not keys:
        keys = {"NSE:RELIANCE": today - dt.timedelta(days=10),
                "BSE:500325": today - dt.timedelta(days=10)}
        for name in INDICES:
            keys["IDX:" + name] = today - dt.timedelta(days=10)

    cache = {}

    def get(kind, d):
        ck = (kind, d)
        if ck not in cache:
            try:
                fn = {"IDX": fetch_index_file, "NSE": fetch_nse, "BSE": fetch_bse}[kind]
                cache[ck] = fn(d)
            except (NotPublished, FetchError) as e:
                cache[ck] = e
        v = cache[ck]
        if isinstance(v, Exception):
            raise v
        return v

    # 1) latest trading day (index file published) -> refresh symbol list
    latest = None
    for back in range(0, 10):
        d = today - dt.timedelta(days=back)
        if d.isoformat() in holidays:
            continue
        try:
            get("IDX", d)
            latest = d
            break
        except NotPublished:
            continue
        except FetchError as e:
            log("Index file fetch error:", e)
            break
    log("Latest trading day with published data:", latest)

    if latest and symbols.get("asof") != latest.isoformat():
        new_sym = {"asof": latest.isoformat()}
        try:
            nse = get("NSE", latest)
            new_sym["NSE"] = sorted([s, v[1]] for s, v in nse.items())
            log(f"NSE bhavcopy {latest}: {len(nse)} securities")
        except Exception as e:
            log("NSE symbol list failed:", e)
        try:
            bse = get("BSE", latest)
            new_sym["BSE"] = sorted([c, v[2], v[1]] for c, v in bse.items())
            log(f"BSE bhavcopy {latest}: {len(bse)} securities")
        except Exception as e:
            log("BSE symbol list failed:", e)
        if "NSE" in new_sym or "BSE" in new_sym:
            new_sym.setdefault("NSE", symbols.get("NSE", []))
            new_sym.setdefault("BSE", symbols.get("BSE", []))
            symbols = new_sym

    # 2) fill missing days for every wanted key
    def covered(k, d):
        c = prices["cov"].get(k)
        return bool(c) and c[0] <= d.isoformat() <= c[1]

    need_days = {}
    last_day = latest or (today - dt.timedelta(days=1))
    for k, start in keys.items():
        start = max(start, today - dt.timedelta(days=MAX_BACKFILL_DAYS))
        for d in daterange(start, last_day):
            if d.isoformat() in holidays or covered(k, d):
                continue
            need_days.setdefault(d, []).append(k)

    log(f"Keys: {len(keys)}; days to fill: {len(need_days)}")
    done = {k: set() for k in keys}
    broken = set()
    for d in sorted(need_days):
        ks = need_days[d]
        try:
            idx = get("IDX", d)
        except NotPublished:
            if d < today:
                holidays.add(d.isoformat())
                for k in ks:
                    done[k].add(d)
            continue
        except FetchError as e:
            log("Stop: index fetch error", e)
            break
        for k in ks:
            kind, code = k.split(":", 1)
            if kind in broken:
                continue
            try:
                if kind == "IDX":
                    v = idx.get(k)
                    name = code.title()
                elif kind == "NSE":
                    hit = get("NSE", d).get(code)
                    v, name = (hit if hit else (None, None))
                elif kind == "BSE":
                    hit = get("BSE", d).get(code)
                    v, name = ((hit[0], hit[1]) if hit else (None, None))
                else:
                    continue
            except NotPublished as e:
                log(f"{kind} file missing on trading day {d}: will retry later")
                broken.add(kind)
                continue
            except FetchError as e:
                log(f"{kind} fetch error on {d}: {e}")
                broken.add(kind)
                continue
            s = prices["series"].setdefault(k, {"name": name or code, "px": {}})
            if v:
                s["px"][d.isoformat()] = round(v, 4)
                if name and name != code:
                    s["name"] = name
            done[k].add(d)
        time.sleep(0.25)

    # 3) update coverage (contiguous from start date)
    for k, start in keys.items():
        start = max(start, today - dt.timedelta(days=MAX_BACKFILL_DAYS))
        end = None
        for d in daterange(start, last_day):
            if d.isoformat() in holidays or covered(k, d) or d in done[k]:
                end = d
            else:
                break
        if end:
            prices["cov"][k] = [start.isoformat(), end.isoformat()]

    # tidy + sort
    for k, s in prices["series"].items():
        s["px"] = dict(sorted(s["px"].items()))
    prices["holidays"] = sorted(holidays)
    prices["updated_ist"] = now_ist.strftime("%Y-%m-%d %H:%M")
    all_days = [d for s in prices["series"].values() for d in s["px"]]
    prices["last_trading_day"] = max(all_days) if all_days else None
    prices["sources"] = {
        "NSE": "NSE UDiFF equity bhavcopy (nsearchives.nseindia.com)",
        "BSE": "BSE UDiFF equity bhavcopy (bseindia.com)",
        "IDX": "NSE index closing values (ind_close_all)",
    }

    if probe:
        for k in sorted(prices["series"]):
            px = prices["series"][k]["px"]
            log(k, prices["series"][k]["name"], list(px.items())[-3:])
        log("holidays:", prices["holidays"][-5:])
        log("symbols asof", symbols.get("asof"), "NSE", len(symbols.get("NSE", [])),
            "BSE", len(symbols.get("BSE", [])))
        return

    os.makedirs(DATA, exist_ok=True)
    with open(PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump(prices, f, separators=(",", ":"))
    with open(SYMBOLS_FILE, "w", encoding="utf-8") as f:
        json.dump(symbols, f, separators=(",", ":"))
    log("Saved", PRICES_FILE, "and", SYMBOLS_FILE)
    if broken:
        log("Some sources were not available this run:", ", ".join(sorted(broken)))


if __name__ == "__main__":
    main()
