#!/usr/bin/env python3
"""
Daily end-of-day price updater for the portfolio tracker.

Runs on GitHub Actions (free). Downloads ONLY the latest available trading
session from these official exchange files (no multi-day back-filling):
  * NSE equity bhavcopy (UDiFF):  nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
  * BSE equity bhavcopy (UDiFF):  www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV
  * NSE index closing values:     nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv

Writes:
  * data/prices.json
      latest.NSE.close         {ticker: close}           every NSE listed share
      latest.BSE.close         {scrip code: close}       every BSE listed share (e.g. "544342")
      latest.BSE.symbol_close  {ticker: close}           same BSE prices keyed by BSE ticker
      latest.IDX.close         {index name: close}       benchmark indices
      series                   daily closes of held stocks and indices, one new day
                               added per run (history builds up going forward)
  * data/symbols.json  list of all NSE/BSE codes and names (for lookup in the site)

History back-fill (only when needed): if a held stock has trades older than
its stored price history, ONE request to Yahoo Finance's daily chart endpoint
(symbol.NS for NSE, scrip code.BO for BSE) fills the missing dates from the
earliest trade date. Yahoo is not an official exchange source, so:
  * official NSE/BSE closes are never overwritten,
  * Yahoo prices are checked against the official closes stored for the same
    dates and rejected if they differ by more than 1% (median),
  * Yahoo's split-adjusted closes are converted back to the actual traded
    prices (as in the exchange files), so quantities and trade prices match,
  * filled dates are listed in series[key]["yahoo_dates"].
Once filled, a stock is marked (series[key]["hist_from"]) and never re-fetched.

Every network request has timeout=10 and the whole run stops waiting after a
fixed time budget, so a daily run finishes in under 15 seconds.

Check mode (no saving):  python scripts/update_prices.py --check BSE:544342
fetches Yahoo's history for one stock and compares it day by day with the
official closes already stored.
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
import urllib.parse
import urllib.request
import zipfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
PORTFOLIO_FILE = os.path.join(DATA, "portfolio.json")
PRICES_FILE = os.path.join(DATA, "prices.json")
SYMBOLS_FILE = os.path.join(DATA, "symbols.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Benchmark indices kept (names as NSE prints them, upper-cased).
INDICES = [
    "NIFTY MICROCAP 250", "NIFTY SMALLCAP 250", "NIFTY SMALLCAP 100",
    "NIFTY MIDCAP 150", "NIFTY MIDCAP 100", "NIFTY MIDSMALLCAP 400",
    "NIFTY LARGEMIDCAP 250", "NIFTY 50", "NIFTY NEXT 50", "NIFTY 100",
    "NIFTY 200", "NIFTY 500", "NIFTY TOTAL MARKET",
]
# NSE series that are listed shares / units (EQ = normal, BE/BZ = trade-for-trade,
# SM/ST/SZ = SME platform, RR = REIT, IV = InvIT)
NSE_SERIES_PREF = ["EQ", "BE", "BZ", "SM", "ST", "SZ", "RR", "IV"]

TIMEOUT = 10          # seconds, every request
BUDGET = 12.0         # seconds spent looking for files, all sources in parallel
LOOKBACK_DAYS = 6     # how far back to look for the most recent session (weekends, holidays)
MAX_YAHOO = 6         # at most this many stocks back-filled per run (the rest on the next run)
YAHOO_MAX_DIFF = 0.01 # reject Yahoo history if it differs from official closes by more than 1% (median)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
START = time.monotonic()


class NotPublished(Exception):
    """File does not exist for that date (holiday, weekend, not published yet)."""


class FetchError(Exception):
    """Network problem or out of time. Try again on the next run."""


def log(*a):
    print(*a, flush=True)


def http_get(url, referer):
    """One attempt, never longer than TIMEOUT and never past the time budget."""
    left = BUDGET - (time.monotonic() - START)
    if left <= 0.5:
        raise FetchError(f"time budget used up before {url}")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    })
    try:
        with urllib.request.urlopen(req, timeout=min(TIMEOUT, left)) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotPublished(url)
        raise FetchError(f"HTTP {e.code} for {url}")
    except Exception as e:  # timeouts, resets, DNS
        raise FetchError(f"{type(e).__name__}: {e} for {url}")


def num(x):
    try:
        v = float(str(x).strip().replace(",", ""))
        return v if v > 0 else None
    except ValueError:
        return None


def clean_rows(text):
    rows = [[c.strip() for c in row] for row in csv.reader(io.StringIO(text)) if row]
    return (rows[0], rows[1:]) if rows else ([], [])


def parse_udiff(text):
    head, rows = clean_rows(text)
    hi = {h: i for i, h in enumerate(head)}
    if "TckrSymb" not in hi or "ClsPric" not in hi:
        return None, None, None
    return head, hi, rows


# ---------------------------------------------------------------- sources (one date each)

def fetch_index(d):
    """{index name: close}"""
    url = f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{d:%d%m%Y}.csv"
    head, rows = clean_rows(http_get(url, "https://www.nseindia.com/").decode("utf-8", "replace"))
    if not head or "Index Name" not in head[0] or "Closing Index Value" not in head:
        raise NotPublished(url)
    ci = head.index("Closing Index Value")
    out = {}
    for r in rows:
        name = r[0].upper()
        if name in INDICES and ci < len(r) and num(r[ci]):
            out[name] = num(r[ci])
    if not out:
        raise NotPublished(url)
    return out


def fetch_nse(d):
    """{ticker: (close, company name)}"""
    url = ("https://nsearchives.nseindia.com/content/cm/"
           f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
    raw = http_get(url, "https://www.nseindia.com/")
    if raw[:2] != b"PK":
        raise NotPublished(url)
    z = zipfile.ZipFile(io.BytesIO(raw))
    head, hi, rows = parse_udiff(z.read(z.namelist()[0]).decode("utf-8", "replace"))
    if head is None:
        raise NotPublished(url)
    best = {}
    for r in rows:
        if len(r) < len(head):
            continue
        ser = r[hi["SctySrs"]].upper() if "SctySrs" in hi else "EQ"
        px = num(r[hi["ClsPric"]])
        if ser not in NSE_SERIES_PREF or not px:
            continue
        sym = r[hi["TckrSymb"]].upper()
        name = r[hi["FinInstrmNm"]] if "FinInstrmNm" in hi else sym
        rank = NSE_SERIES_PREF.index(ser)
        if sym not in best or rank < best[sym][2]:
            best[sym] = (px, name, rank)
    if not best:
        raise NotPublished(url)
    return {k: (v[0], v[1]) for k, v in best.items()}


def fetch_bse(d):
    """{scrip code: (close, company name, BSE ticker)}"""
    url = ("https://www.bseindia.com/download/BhavCopy/Equity/"
           f"BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV")
    head, hi, rows = parse_udiff(http_get(url, "https://www.bseindia.com/").decode("utf-8", "replace"))
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
        if code and px:
            name = r[hi["FinInstrmNm"]] if "FinInstrmNm" in hi else code
            out[code] = (px, name, r[hi["TckrSymb"]].upper())
    if not out:
        raise NotPublished(url)
    return out


def latest_session(kind, fn, today):
    """Newest date (today or up to LOOKBACK_DAYS back) whose file exists. Returns (date, data) or (None, None)."""
    for back in range(LOOKBACK_DAYS + 1):
        d = today - dt.timedelta(days=back)
        try:
            return d, fn(d)
        except NotPublished:
            continue
        except FetchError as e:
            log(f"{kind}: {e}")
            return None, None
    log(f"{kind}: no file found in the last {LOOKBACK_DAYS + 1} days")
    return None, None


# ---------------------------------------------------------------- history back-fill (Yahoo Finance)

def same_company(a, b):
    """First word of two company names agree (guards the NSE fallback against a different company)."""
    wa = "".join(ch for ch in (a or "").upper().split(" ")[0] if ch.isalpha())
    wb = "".join(ch for ch in (b or "").upper().split(" ")[0] if ch.isalpha())
    return len(wa) >= 3 and wa[:4] == wb[:4]


def yahoo_symbols(key, bse_rows, nse_rows=()):
    """Yahoo names to try, best first. BSE shares are listed on Yahoo either by scrip code
    (500325.BO) or by ticker (STALLION.BO); the NSE ticker is a last resort for a BSE holding."""
    ex, code = key.split(":", 1)
    if ex == "NSE":
        return [code + ".NS"]
    if ex != "BSE":
        return []
    by_code = {r[0]: r[1] for r in bse_rows}
    names = {r[0]: r[2] for r in bse_rows if len(r) > 2}
    nse_names = dict(nse_rows)
    by_sym = {r[1]: r[0] for r in bse_rows}
    if not code.isdigit():
        code = by_sym.get(code, "")
    out = []
    if code:
        out.append(code + ".BO")
    ticker = by_code.get(code) or (key.split(":", 1)[1] if not key.split(":", 1)[1].isdigit() else "")
    if ticker:
        out.append(ticker + ".BO")
        if ticker in nse_names and same_company(names.get(code), nse_names[ticker]):
            out.append(ticker + ".NS")
    return out


def fetch_yahoo_any(candidates, start, end):
    """Try each Yahoo name until one has data. Returns (name used, closes, splits)."""
    last = None
    for ysym in candidates:
        try:
            hist, splits = fetch_yahoo_history(ysym, start, end)
            if hist:
                return ysym, hist, splits
            last = NotPublished(f"{ysym}: no prices in range")
        except (NotPublished, FetchError) as e:
            last = e
            log(f"  Yahoo {ysym}: {'not found' if isinstance(e, NotPublished) else e}")
    raise last or NotPublished("no Yahoo symbol to try")


def fetch_yahoo_history(ysym, start, end):
    """Daily closes from start to end (inclusive) in ONE request: {iso date: actual traded close}."""
    p1 = int(dt.datetime.combine(start, dt.time(), IST).timestamp())
    p2 = int(dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), IST).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ysym)}"
           f"?period1={p1}&period2={p2}&interval=1d&events=split")
    raw = http_get(url, "https://finance.yahoo.com/")
    try:
        res = json.loads(raw)["chart"]["result"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise FetchError(f"unexpected Yahoo reply for {ysym}")
    stamps = res.get("timestamp") or []
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    # Yahoo's close is adjusted for later splits/bonuses; undo that to get the traded price
    splits = []
    for ev in ((res.get("events") or {}).get("splits") or {}).values():
        try:
            when = dt.datetime.fromtimestamp(int(ev["date"]), IST).date()
            ratio = float(ev["numerator"]) / float(ev["denominator"])
            if ratio > 0:
                splits.append((when, ratio))
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            continue
    out = {}
    for t, c in zip(stamps, closes):
        if c is None or c <= 0:
            continue
        d = dt.datetime.fromtimestamp(int(t), IST).date()
        if d < start or d > end:
            continue
        factor = 1.0
        for when, ratio in splits:
            if when > d:
                factor *= ratio
        out[d.isoformat()] = round(c * factor, 4)
    return out, splits


def compare_with_official(yahoo, official):
    """Median relative difference on dates present in both, and the number of such dates."""
    diffs = sorted(abs(yahoo[d] / official[d] - 1) for d in official if d in yahoo and official[d])
    if not diffs:
        return None, 0
    return diffs[len(diffs) // 2], len(diffs)


# ---------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def held_keys(portfolio):
    """{key: earliest trade date} for every stock in any portfolio. Keys look like NSE:RELIANCE or BSE:544342."""
    keys = {}
    for p in portfolio.get("portfolios", []):
        for e in p.get("entries", []):
            if e.get("code") and e.get("exch") in ("NSE", "BSE"):
                k = f'{e["exch"]}:{str(e["code"]).strip().upper()}'
                try:
                    d = dt.date.fromisoformat(str(e["date"])[:10])
                except (KeyError, ValueError):
                    continue
                if k not in keys or d < keys[k]:
                    keys[k] = d
    return keys


def check(key):
    """Compare Yahoo's history for one stock with the official closes stored in prices.json (no saving)."""
    prices = load_json(PRICES_FILE, {})
    symbols = load_json(SYMBOLS_FILE, {})
    official = dict((prices.get("series", {}).get(key) or {}).get("px", {}))
    for d in (prices.get("series", {}).get(key) or {}).get("yahoo_dates", []):
        official.pop(d, None)
    cands = yahoo_symbols(key, symbols.get("BSE", []), symbols.get("NSE", []))
    today = dt.datetime.now(IST).date()
    start = dt.date.fromisoformat(min(official)) - dt.timedelta(days=30) if official else today - dt.timedelta(days=60)
    log(f"Check {key}: trying Yahoo {', '.join(cands)} for {start} to {today}")
    try:
        ysym, hist, splits = fetch_yahoo_any(cands, start, today)
    except (NotPublished, FetchError) as e:
        log(f"No Yahoo history found: {e}")
        return
    log(f"Yahoo {ysym} returned {len(hist)} daily closes; splits/bonuses in range: {splits or 'none'}")
    log(f"{'date':<12}{'official':>12}{'yahoo':>12}{'diff %':>9}")
    for d in sorted(set(official) | set(hist)):
        o, y = official.get(d), hist.get(d)
        diff = f"{(y / o - 1) * 100:+.2f}" if o and y else ""
        log(f"{d:<12}{o if o else '-':>12}{y if y else '-':>12}{diff:>9}")
    med, n = compare_with_official(hist, official)
    log(f"Dates in both: {n}; median difference: {med * 100:.3f}%" if n else "No dates in both to compare")

    # Spot check: Yahoo-filled dates against the official exchange file of that exact day
    filled = sorted((prices.get("series", {}).get(key) or {}).get("yahoo_dates", []))
    if filled:
        stored = prices["series"][key]["px"]
        picks = sorted({filled[0], filled[len(filled) // 4], filled[len(filled) // 2], filled[-1]})
        ex, code = key.split(":", 1)
        if ex == "BSE" and not code.isdigit():
            code = {r[1]: r[0] for r in symbols.get("BSE", [])}.get(code, code)
        log(f"Spot check of {len(picks)} Yahoo-filled dates against the official {ex} file of that day:")
        for d in picks:
            try:
                day = fetch_bse(dt.date.fromisoformat(d)) if ex == "BSE" else fetch_nse(dt.date.fromisoformat(d))
                o = day.get(code, (None,))[0]
                diff = f"{(stored[d] / o - 1) * 100:+.2f}%" if o else "not traded that day"
                log(f"  {d}: stored {stored[d]}  official {o}  {diff}")
            except (NotPublished, FetchError) as e:
                log(f"  {d}: official file not available ({e})")


def main():
    now_ist = dt.datetime.now(IST)
    today = now_ist.date()
    log(f"Run at {now_ist:%Y-%m-%d %H:%M} IST")

    portfolio = load_json(PORTFOLIO_FILE, {"portfolios": []})
    prices = load_json(PRICES_FILE, {})
    before = json.dumps({k: v for k, v in prices.items() if k != "updated_ist"}, sort_keys=True)
    for old in ("cov", "holidays"):  # used by the old multi-day back-filling, no longer needed
        prices.pop(old, None)
    series = prices.setdefault("series", {})
    latest = prices.setdefault("latest", {})
    symbols = load_json(SYMBOLS_FILE, {})
    held = held_keys(portfolio)

    # Stocks whose stored history does not reach back to their earliest trade
    bse_rows, nse_rows = symbols.get("BSE", []), symbols.get("NSE", [])
    need_hist = {k: d for k, d in sorted(held.items())
                 if (series.get(k) or {}).get("hist_from", "9999") > d.isoformat()
                 and yahoo_symbols(k, bse_rows, nse_rows)}
    if len(need_hist) > MAX_YAHOO:
        log(f"{len(need_hist)} stocks need history; doing {MAX_YAHOO} now, the rest next run")
        need_hist = dict(list(need_hist.items())[:MAX_YAHOO])
    hist_found = {}

    def hist_worker(key, start):
        cands = yahoo_symbols(key, bse_rows, nse_rows)
        try:
            ysym, hist, _ = fetch_yahoo_any(cands, start, today - dt.timedelta(days=1))
            hist_found[key] = (ysym, hist)
        except (NotPublished, FetchError) as e:
            hist_found[key] = ("/".join(cands), e)

    # One latest session per source, all three in parallel. A hard wall clock
    # limit stops waiting even if a server sends data extremely slowly.
    found = {}

    def worker(kind, fn):
        found[kind] = latest_session(kind, fn, today)

    threads = [threading.Thread(target=worker, args=a, daemon=True)
               for a in (("IDX", fetch_index), ("NSE", fetch_nse), ("BSE", fetch_bse))]
    threads += [threading.Thread(target=hist_worker, args=(k, d), daemon=True) for k, d in need_hist.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, BUDGET + 1.0 - (time.monotonic() - START)))
    for kind in ("IDX", "NSE", "BSE"):
        if kind not in found:
            log(f"{kind}: stopped waiting, out of time")
    d_idx, idx = found.get("IDX", (None, None))
    d_nse, nse = found.get("NSE", (None, None))
    d_bse, bse = found.get("BSE", (None, None))

    def add_point(key, name, d, close):
        s = series.setdefault(key, {"name": name, "px": {}})
        if name and name != key.split(":", 1)[1]:
            s["name"] = name
        s["px"][d.isoformat()] = round(close, 4)
        s["px"] = dict(sorted(s["px"].items()))
        if d.isoformat() in s.get("yahoo_dates", []):  # an official close replaces a Yahoo one
            s["yahoo_dates"].remove(d.isoformat())

    if idx:
        log(f"Indices: session {d_idx}, {len(idx)} indices")
        latest["IDX"] = {"date": d_idx.isoformat(), "close": idx}
        for name, close in idx.items():
            add_point("IDX:" + name, name.title(), d_idx, close)
    if nse:
        log(f"NSE bhavcopy: session {d_nse}, {len(nse)} securities")
        latest["NSE"] = {"date": d_nse.isoformat(), "close": {s: round(v[0], 4) for s, v in sorted(nse.items())}}
    if bse:
        log(f"BSE bhavcopy: session {d_bse}, {len(bse)} securities")
        latest["BSE"] = {
            "date": d_bse.isoformat(),
            "close": {c: round(v[0], 4) for c, v in sorted(bse.items())},
            "symbol_close": {v[2]: round(v[0], 4) for c, v in sorted(bse.items())},
        }

    # Daily point for each held stock (builds history from today onwards)
    for key in sorted(held):
        ex_, code = key.split(":", 1)
        if ex_ == "NSE" and nse and code in nse:
            add_point(key, nse[code][1], d_nse, nse[code][0])
        elif ex_ == "BSE" and bse:
            if code not in bse:  # a BSE ticker typed instead of the scrip code
                code = next((c for c, v in bse.items() if v[2] == code), code)
            if code in bse:
                add_point(key, bse[code][1], d_bse, bse[code][0])
            else:
                log(f"{key}: not in the BSE file of {d_bse}")
        elif ex_ == "NSE" and nse:
            log(f"{key}: not in the NSE file of {d_nse}")

    # History back-fill: only dates without an official close, only if it agrees with official closes
    for key, start in need_hist.items():
        if key not in hist_found:
            log(f"{key}: history request ran out of time, will retry next run")
            continue
        ysym, hist = hist_found[key]
        if isinstance(hist, Exception):
            log(f"{key}: history from Yahoo ({ysym}) failed: {hist}. Will retry next run")
            continue
        s = series.setdefault(key, {"name": key.split(":", 1)[1], "px": {}})
        filled = set(s.get("yahoo_dates", []))
        official = {d: v for d, v in s["px"].items() if d not in filled}
        med, n = compare_with_official(hist, official)
        if n and med > YAHOO_MAX_DIFF:
            log(f"{key}: Yahoo ({ysym}) differs from official closes by {med * 100:.2f}% (median of {n} days). Not used")
            continue
        new = {d: v for d, v in hist.items() if d not in official}
        s["px"].update(new)
        s["px"] = dict(sorted(s["px"].items()))
        s["yahoo_dates"] = sorted(filled | set(new))
        s["hist_from"] = start.isoformat()
        s["hist_source"] = f"Yahoo Finance {ysym} (dates without an official NSE/BSE close)"
        check_txt = f"matches official closes within {med * 100:.3f}% (median of {n} days)" if n else "no official closes to compare yet"
        log(f"{key}: filled {len(new)} daily closes from {start} via Yahoo {ysym}; {check_txt}")

    # Symbol list for code lookup in the website (from the same files, no extra download)
    sym_date = max([d for d in (d_nse, d_bse) if d], default=None)
    if sym_date and symbols.get("asof") != sym_date.isoformat():
        symbols = {
            "asof": sym_date.isoformat(),
            "NSE": sorted([s, v[1]] for s, v in nse.items()) if nse else symbols.get("NSE", []),
            "BSE": sorted([c, v[2], v[1]] for c, v in bse.items()) if bse else symbols.get("BSE", []),
        }

    dates = [x["date"] for x in latest.values() if isinstance(x, dict) and x.get("date")]
    prices["last_trading_day"] = max(dates) if dates else prices.get("last_trading_day")
    prices["updated_ist"] = now_ist.strftime("%Y-%m-%d %H:%M")
    prices["sources"] = {
        "NSE": "NSE UDiFF equity bhavcopy (nsearchives.nseindia.com)",
        "BSE": "BSE UDiFF equity bhavcopy (bseindia.com)",
        "IDX": "NSE index closing values (ind_close_all)",
    }

    os.makedirs(DATA, exist_ok=True)
    after = json.dumps({k: v for k, v in prices.items() if k != "updated_ist"}, sort_keys=True)
    if after != before or not os.path.exists(PRICES_FILE):
        with open(PRICES_FILE, "w", encoding="utf-8") as f:
            json.dump(prices, f, separators=(",", ":"))
        log("Saved", PRICES_FILE)
    else:
        log("No new prices")
    with open(SYMBOLS_FILE, "w", encoding="utf-8") as f:
        json.dump(symbols, f, separators=(",", ":"))
    log(f"Finished in {time.monotonic() - START:.1f} s")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        check(sys.argv[2].upper())
    else:
        main()
