"""Market Theme Map: a local heatmap of stock themes (or GICS sectors).

Uses only Python's standard library, so there is nothing to install.

Run:  python app.py            (live, delayed data from Yahoo Finance)
      python app.py --demo     (made-up prices, no internet needed)
Then open http://localhost:8050

Hosting (e.g. Render): when the PORT environment variable is set, the app
listens on 0.0.0.0:$PORT and doesn't open a browser. Set APP_PASSWORD to ask
for a password (any username); leave it unset for no password.
"""
import argparse
import base64
import hmac
import http.cookiejar
import json
import math
import os
import random
import re
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
THEMES_FILE = BASE / "themes.json"
SECTORS_FILE = BASE / "sectors.json"
VIEWS = ("themes", "sectors")  # the groupings the map can switch between

# Lookbacks in trading sessions (1W = five sessions).
TICKER_RE = re.compile(r"[A-Z0-9.\-^=]{1,15}")
PERIODS = {"1D": 1, "1W": 5, "1M": 21, "3M": 63, "6M": 126, "1Y": 252}
PRICE_TTL = 5 * 60        # seconds between price refreshes
INFO_TTL = 6 * 60 * 60    # seconds to keep analyst data

ARGS = None
_lock = threading.Lock()
_prices = {"closes": {}, "dates": [], "fetched": 0.0, "asof": None, "error": None, "busy": False}
_info_cache = {}


def _clean(tickers):
    return [t.strip().upper() for t in tickers if t.strip()]


def _load_groups(path):
    """name -> tickers. An entry can also be {"etf": "XLK", "tickers": [...]}, as in sectors.json."""
    with open(path, encoding="utf-8") as f:
        groups = json.load(f)
    return {name: _clean(v["tickers"] if isinstance(v, dict) else v) for name, v in groups.items()}


def load_themes():
    return _load_groups(THEMES_FILE)


def load_sectors():
    """The 11 GICS sectors. Missing file means no sectors, so an old themes-only setup still runs."""
    try:
        return _load_groups(SECTORS_FILE)
    except FileNotFoundError:
        return {}


def load_sector_etfs():
    """Sector name -> its SPDR sector ETF (XLK, XLF...), whose move the sector card shows."""
    try:
        with open(SECTORS_FILE, encoding="utf-8") as f:
            groups = json.load(f)
    except FileNotFoundError:
        return {}
    return {name: v["etf"].strip().upper() for name, v in groups.items()
            if isinstance(v, dict) and (v.get("etf") or "").strip()}


def load_groups(view):
    return load_sectors() if view == "sectors" else load_themes()


def all_tickers(*groupings):
    return sorted({t for groups in groupings for tickers in groups.values() for t in tickers})


# ---------------------------------------------------------------- data sources

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
_cookies = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookies))
_crumb = {"value": None}
_crumb_lock = threading.Lock()
_meta = {}  # ticker -> chart metadata (name, 52-week range), used when analyst data fails


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with _opener.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def fetch_chart(ticker):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(ticker)}?range=14mo&interval=1d&includePrePost=false")
    for attempt in range(3):
        try:
            res = json.loads(http_get(url))["chart"]["result"][0]
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ticker, None, None
            time.sleep(1 + attempt * 2)
        except Exception:
            time.sleep(1 + attempt * 2)
    else:
        return ticker, None, None
    ind = res.get("indicators", {})
    meta = res.get("meta", {})
    stamps = res.get("timestamp") or []
    adj = (ind.get("adjclose") or [{}])[0].get("adjclose") or []
    raw = (ind.get("quote") or [{}])[0].get("close") or []
    offset = meta.get("gmtoffset") or 0
    day = lambda ts: time.strftime("%Y-%m-%d", time.gmtime(ts + offset))
    # Yahoo leaves the newest day's adjusted close empty until it's finalised,
    # so fall back to the plain close for any bar without one.
    bars = []
    for i, ts in enumerate(stamps):
        a = adj[i] if i < len(adj) else None
        c = raw[i] if i < len(raw) else None
        v = a if a is not None else c
        if v is not None:
            bars.append([day(ts), float(v)])
    # Make sure the latest trade is in the series, even if Yahoo hasn't added today's bar yet.
    live, live_ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if live and live_ts:
        live_day = day(live_ts)
        if bars and bars[-1][0] == live_day:
            bars[-1][1] = float(live)
        elif not bars or bars[-1][0] < live_day:
            bars.append([live_day, float(live)])
    series = [round(v, 4) for _, v in bars]
    meta["lastDay"] = bars[-1][0] if bars else None
    return ticker, series, meta


def fetch_live(tickers):
    closes, days = {}, []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for t, series, meta in pool.map(fetch_chart, tickers):
            if series and len(series) >= 2:
                closes[t] = series
            if meta:
                _meta[t] = meta
                if meta.get("lastDay"):
                    days.append(meta["lastDay"])
    return closes, sorted(days)[-1:]


def get_crumb(force=False):
    with _crumb_lock:
        if _crumb["value"] and not force:
            return _crumb["value"]
        try:
            http_get("https://fc.yahoo.com")  # sets the session cookie (the page itself 404s)
        except Exception:
            pass
        _crumb["value"] = http_get("https://query1.finance.yahoo.com/v1/test/getcrumb").strip()
        return _crumb["value"]


def fetch_summary(ticker):
    modules = "financialData,summaryDetail,defaultKeyStatistics,price,assetProfile"
    for force in (False, True):
        try:
            crumb = get_crumb(force)
            url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(ticker)}"
                   f"?modules={modules}&crumb={urllib.parse.quote(crumb)}")
            res = json.loads(http_get(url))["quoteSummary"]["result"][0]
            break
        except Exception as e:
            err = e
    else:
        raise err
    flat = {}
    for mod in res.values():
        if isinstance(mod, dict):
            for k, v in mod.items():
                v = v.get("raw") if isinstance(v, dict) else v
                if v is not None and v != {} and k not in flat:
                    flat[k] = v
    return flat


def fetch_demo(tickers):
    days = 300
    rng = random.Random(42 + int(time.time() // PRICE_TTL))
    closes = {}
    for t in tickers:
        r = random.Random(t)
        p = r.uniform(20, 600)
        drift = r.uniform(-0.0008, 0.0015)
        vol = r.uniform(0.012, 0.04)
        series = []
        for _ in range(days):
            p *= math.exp(drift + vol * r.gauss(0, 1))
            series.append(round(p, 2))
        series[-1] = round(series[-2] * math.exp(vol * rng.gauss(0, 1)), 2)
        closes[t] = series
    today = datetime.now().strftime("%Y-%m-%d")
    return closes, [""] * (days - 1) + [today]


def refresh_prices(force=False, wait=False):
    """Refresh prices when they're stale.

    Downloading takes 10 to 30 seconds, so once there is data to show, the
    download runs in the background and requests keep getting the last prices.
    """
    with _lock:
        stale = force or not _prices["closes"] or time.time() - _prices["fetched"] >= PRICE_TTL
        start = stale and not _prices["busy"]
        if start:
            _prices["busy"] = True
    if start:
        if _prices["closes"] and not wait:
            threading.Thread(target=_download_prices, daemon=True).start()
        else:
            _download_prices()
    elif wait:
        while _prices["busy"]:  # another refresh is running; let it finish
            time.sleep(0.25)


def _download_prices():
    try:
        # Prices for both views are kept together, so switching views needs no new download.
        etfs = {name: [etf] for name, etf in load_sector_etfs().items()}
        tickers = all_tickers(load_themes(), load_sectors(), etfs)
        try:
            fresh, dates = (fetch_demo if ARGS.demo else fetch_live)(tickers)
            if not fresh:
                raise RuntimeError("no prices returned")
        except Exception as e:  # keep serving the last good data
            with _lock:
                _prices["error"] = str(e)
                _prices["fetched"] = time.time() - PRICE_TTL + 60  # retry in a minute
            print("price refresh failed:", e)
            return
        # Symbols Yahoo skipped this time keep their last good prices.
        old = _prices["closes"]
        kept = [t for t in tickers if t not in fresh and t in old]
        closes = {**{t: old[t] for t in kept}, **fresh}
        missing = len(tickers) - len(closes)
        notes = []
        if kept:
            notes.append(f"{len(kept)} kept from the last refresh")
        if missing:
            notes.append(f"{missing} missing")
        with _lock:
            _prices.update(closes=closes, dates=dates or _prices["dates"], fetched=time.time(),
                           asof=datetime.now().strftime("%Y-%m-%d %H:%M"), error=None)
        print(f"[{_prices['asof']}] prices refreshed for {len(fresh)} tickers"
              + (f" ({', '.join(notes)})" if notes else ""))
    finally:
        _prices["busy"] = False


def get_info(ticker):
    hit = _info_cache.get(ticker)
    if hit and time.time() - hit[0] < INFO_TTL:
        return hit[1]
    if ARGS.demo:
        r = random.Random(ticker)
        price = (_prices["closes"].get(ticker) or [100])[-1]
        info = {
            "longName": f"{ticker} (demo)", "targetMeanPrice": round(price * r.uniform(0.85, 1.35), 2),
            "targetHighPrice": round(price * 1.6, 2), "targetLowPrice": round(price * 0.7, 2),
            "numberOfAnalystOpinions": r.randint(3, 45),
            "recommendationKey": r.choice(["strong_buy", "buy", "hold", "underperform"]),
            "recommendationMean": round(r.uniform(1.3, 3.4), 2),
            "trailingPE": round(r.uniform(8, 80), 1), "forwardPE": round(r.uniform(8, 60), 1),
            "marketCap": r.randint(2, 3000) * 10**9, "sector": "Demo", "industry": "Demo",
        }
    else:
        m = _meta.get(ticker, {})
        info = {"longName": m.get("longName"), "shortName": m.get("shortName"),
                "fiftyTwoWeekHigh": m.get("fiftyTwoWeekHigh"), "fiftyTwoWeekLow": m.get("fiftyTwoWeekLow")}
        try:
            info.update(fetch_summary(ticker))
        except Exception as e:
            info["error"] = str(e)
    keep = ["longName", "shortName", "sector", "industry", "marketCap", "trailingPE", "forwardPE",
            "targetMeanPrice", "targetMedianPrice", "targetHighPrice", "targetLowPrice",
            "numberOfAnalystOpinions", "recommendationKey", "recommendationMean",
            "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "error"]
    year = (_prices["closes"].get(ticker) or [])[-253:]
    if year and info.get("fiftyTwoWeekHigh") is None:
        info["fiftyTwoWeekHigh"], info["fiftyTwoWeekLow"] = max(year), min(year)
    info = {k: info.get(k) for k in keep if info.get(k) is not None}
    _info_cache[ticker] = (time.time(), info)
    return info


# ---------------------------------------------------------------- calculations

def pct_change(series, n):
    if len(series) <= n:
        return None
    base = series[-1 - n]
    return None if not base else (series[-1] / base - 1) * 100


def build_map(period, view="themes"):
    """Figures for each group in the chosen view. The list is called "themes" in either view.

    A theme's move ("avg") is the equal-weighted average of its stocks. A sector's move is
    its SPDR sector ETF's move instead, so it matches XLK, XLF and so on; if the ETF has no
    price, it falls back to the stocks' average and says so in "basis". Median and breadth
    always come from the listed stocks.
    """
    view = view if view in VIEWS else "themes"
    n = PERIODS.get(period, 1)
    refresh_prices()
    closes = _prices["closes"]
    etfs = load_sector_etfs() if view == "sectors" else {}
    themes = []
    for name, tickers in load_groups(view).items():
        members = []
        for t in tickers:
            s = closes.get(t)
            chg = pct_change(s, n) if s else None
            members.append({"t": t, "chg": None if chg is None else round(chg, 2),
                            "price": s[-1] if s else None})
        moves = [m["chg"] for m in members if m["chg"] is not None]
        avg = round(statistics.fmean(moves), 2) if moves else None
        group = {"name": name, "basis": "stocks"}
        etf = etfs.get(name)
        if etf:
            s = closes.get(etf)
            chg = pct_change(s, n) if s else None
            group["etf"] = {"t": etf, "chg": None if chg is None else round(chg, 2),
                            "price": s[-1] if s else None}
            if chg is not None:
                avg, group["basis"] = round(chg, 2), "etf"
        themes.append({
            **group,
            "avg": avg,
            "median": round(statistics.median(moves), 2) if moves else None,
            "adv": sum(1 for m in moves if m > 0),
            "count": len(moves),
            "members": sorted(members, key=lambda m: -1e9 if m["chg"] is None else -m["chg"]),
        })
    return {"period": period, "view": view, "asof": _prices["asof"], "demo": ARGS.demo,
            "lastDay": (_prices["dates"] or [None])[-1],
            "error": _prices["error"], "themes": themes}


def build_ticker(ticker, period):
    refresh_prices()
    s = _prices["closes"].get(ticker, [])
    n = PERIODS.get(period, 1)
    history = s[-(max(n, 21) + 1):]
    info = get_info(ticker)
    price = s[-1] if s else None
    target = info.get("targetMeanPrice")
    upside = round((target / price - 1) * 100, 1) if target and price else None
    return {"t": ticker, "price": price, "chg": pct_change(s, n) if s else None,
            "history": history, "upside": upside, "info": info,
            "themes": [k for k, v in load_themes().items() if ticker in v],
            "sectors": [k for k, v in load_sectors().items() if ticker in v]}


# ---------------------------------------------------------------- web server

# --------------------------------------------------------------------------
# Update notice
# --------------------------------------------------------------------------
# A release zip contains a VERSION file (written by .github/workflows/release.yml).
# The page asks /api/version, which compares it with the newest GitHub release and
# shows a "new version available" banner. It only reads the public release number;
# nothing about you is sent. A git clone (or a Render deploy) has no VERSION file
# and skips the check. Turn it off with --no-update-check or THEMEMAP_NO_UPDATE_CHECK=1.
UPDATE_REPO = "trader-larry/market-theme-map"
VERSION_FILE = BASE / "VERSION"
UPDATE_CHECK_HOURS = 12
_update = {"at": 0.0, "latest": None, "url": None}
_update_lock = threading.Lock()


def current_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _version_key(v):
    """'v1.10.2' -> (1, 10, 2). Anything that isn't a plain number counts as 0."""
    parts = [int(p) if p.isdigit() else 0 for p in v.strip().lstrip("vV").split("-")[0].split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def _latest_release():
    """Newest published release on GitHub, asked at most every UPDATE_CHECK_HOURS."""
    with _update_lock:
        if time.time() - _update["at"] < UPDATE_CHECK_HOURS * 3600:
            return _update["latest"], _update["url"]
        _update["at"] = time.time()  # also after a failure, so an offline computer isn't retried every load
        try:
            req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                         headers={"Accept": "application/vnd.github+json",
                                                  "User-Agent": "market-theme-map-update-check"})
            with urllib.request.urlopen(req, timeout=8) as r:
                rel = json.loads(r.read().decode("utf-8"))
            _update["latest"] = (rel.get("tag_name") or "").strip() or None
            _update["url"] = rel.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases/latest"
        except Exception:
            pass  # offline, rate-limited or no release yet: just show nothing
        return _update["latest"], _update["url"]


def version_info():
    cur = current_version()
    out = {"current": cur, "latest": None, "url": None, "update": False}
    if not cur or not getattr(ARGS, "update_check", True) or getattr(ARGS, "demo", False):
        return out
    latest, url = _latest_release()
    out.update(latest=latest, url=url, update=bool(latest) and _version_key(latest) > _version_key(cur))
    return out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        # Stop the browser from keeping an old copy of the page after an update.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        password = os.environ.get("APP_PASSWORD")
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except Exception:
                given = ""
            if hmac.compare_digest(given.encode(), password.encode()):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Market Theme Map", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/healthz":  # for the host's health check, so it skips the password
            return self.send_json({"ok": True})
        if not self.authorized():
            return
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/api/map":
                view = q.get("view", "themes")
                if view not in VIEWS:
                    return self.send_json({"error": "invalid view"}, 400)
                return self.send_json(build_map(q.get("period", "1D"), view))
            if url.path == "/api/ticker":
                ticker = q.get("t", "").strip().upper()
                if not TICKER_RE.fullmatch(ticker):
                    return self.send_json({"error": "invalid ticker"}, 400)
                return self.send_json(build_ticker(ticker, q.get("period", "1D")))
            if url.path == "/api/version":
                return self.send_json(version_info())
            if url.path == "/api/refresh":
                refresh_prices(force=True, wait=True)
                return self.send_json({"ok": True, "asof": _prices["asof"]})
        except Exception as e:
            return self.send_json({"error": str(e)}, 500)
        return super().do_GET()


def main():
    global ARGS
    env_port = os.environ.get("PORT")  # set by hosts such as Render
    ap = argparse.ArgumentParser(description="Local market theme map")
    ap.add_argument("--port", type=int, default=int(env_port) if env_port else 8050)
    ap.add_argument("--host", default="0.0.0.0" if env_port else "127.0.0.1",
                    help="address to listen on (default 127.0.0.1, or 0.0.0.0 when PORT is set)")
    ap.add_argument("--demo", action="store_true", help="use made-up prices (no internet)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    ap.add_argument("--no-update-check", action="store_true",
                    help="don't check GitHub for a newer version (also THEMEMAP_NO_UPDATE_CHECK=1)")
    ARGS = ap.parse_args()
    ARGS.update_check = not (ARGS.no_update_check
                             or os.environ.get("THEMEMAP_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes", "on"))
    if env_port:
        ARGS.no_browser = True

    try:
        server = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    except OSError:
        raise SystemExit(f"Port {ARGS.port} is busy. Is the app already running? "
                         f"Close it, or start with --port {ARGS.port + 1}.")
    # The port is open now (so a host sees the app starting); requests wait until prices load.
    print("Loading prices (first load can take ~20 seconds)...")
    refresh_prices(force=True)
    url = f"http://localhost:{ARGS.port}"
    ver = current_version()
    print(f"Market Theme Map {ver + ' ' if ver else ''}running at {url}  (Ctrl+C to stop)")
    if os.environ.get("APP_PASSWORD"):
        print("Password protection is on (APP_PASSWORD is set).")
    if not ARGS.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
