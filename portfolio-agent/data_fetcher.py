"""
Price and macro data fetcher for the Portfolio Agent.
Sources: Yahoo Finance (no key), MEXC public API (no key), FRED API (free key).
"""
import sys
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

import requests

BASE_DIR = Path(__file__).resolve().parent
_SHARED = str(BASE_DIR.parent / "shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

from utils import CHROME_HDR  # noqa: E402
from assets import PORTFOLIO_ASSETS, YF_SYMBOLS, MEXC_SYMBOLS  # noqa: E402

# ── Yahoo Finance ──────────────────────────────────────────────────────────────

def _yf_fetch(yf_symbol, history=60):
    """Return (latest_close, list_of_closes) from Yahoo Finance."""
    try:
        days = max(history * 2, 90)
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
               f"?interval=1d&range={days}d")
        r = requests.get(url, timeout=15, headers=CHROME_HDR)
        if r.status_code != 200:
            return (None, [])
        data = r.json()
        closes_raw = (data.get("chart", {}).get("result", [{}])[0]
                      .get("indicators", {}).get("quote", [{}])[0]
                      .get("close", []))
        closes = [round(c, 4) for c in closes_raw if c is not None]
        if not closes:
            return (None, [])
        return (closes[-1], closes)
    except Exception as e:
        print(f"[Portfolio] YF {yf_symbol}: {e}")
        return (None, [])


def _yf_fetch_full(yf_symbol, history=60):
    # type: (str, int) -> tuple
    """Return (latest_close, closes, highs, lows) from Yahoo Finance."""
    try:
        days = max(history * 2, 90)
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
               f"?interval=1d&range={days}d")
        r = requests.get(url, timeout=15, headers=CHROME_HDR)
        if r.status_code != 200:
            return (None, [], [], [])
        data = r.json()
        quote = (data.get("chart", {}).get("result", [{}])[0]
                 .get("indicators", {}).get("quote", [{}])[0])
        raw_c = quote.get("close", [])
        raw_h = quote.get("high", [])
        raw_l = quote.get("low", [])
        closes, highs, lows = [], [], []
        for c, h, l in zip(raw_c, raw_h, raw_l):
            if c is not None and h is not None and l is not None:
                closes.append(round(c, 4))
                highs.append(round(h, 4))
                lows.append(round(l, 4))
        if not closes:
            return (None, [], [], [])
        return (closes[-1], closes, highs, lows)
    except Exception as e:
        print(f"[Portfolio] YF FULL {yf_symbol}: {e}")
        return (None, [], [], [])


def _pct_chg(closes, n_days):
    """% change over last n_days from closes list."""
    if len(closes) < n_days + 1:
        return None
    old = closes[-(n_days + 1)]
    new = closes[-1]
    if old and old != 0:
        return round((new - old) / old * 100, 2)
    return None


def _ma(closes, n):
    """Simple moving average over last n closes."""
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 4)


def _atr(highs, lows, closes, n=14):
    # type: (List[float], List[float], List[float], int) -> Optional[float]
    """N-day Average True Range."""
    if len(highs) < n + 1 or len(lows) < n + 1 or len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < n:
        return None
    return round(sum(trs[-n:]) / n, 4)


def _yf_fetch_weekly(yf_symbol, weeks=52):
    # type: (str, int) -> dict
    """Return weekly summary: 52-week high/low, 8-week ATR, last 4 weekly closes."""
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
               f"?interval=1wk&range={weeks}wk")
        r = requests.get(url, timeout=15, headers=CHROME_HDR)
        if r.status_code != 200:
            return {}
        data = r.json()
        quote = (data.get("chart", {}).get("result", [{}])[0]
                 .get("indicators", {}).get("quote", [{}])[0])
        raw_c = quote.get("close", [])
        raw_h = quote.get("high", [])
        raw_l = quote.get("low", [])
        closes, highs, lows = [], [], []
        for c, h, l in zip(raw_c, raw_h, raw_l):
            if c is not None and h is not None and l is not None:
                closes.append(round(c, 4))
                highs.append(round(h, 4))
                lows.append(round(l, 4))
        if not closes:
            return {}
        w52_high = max(highs[-52:]) if len(highs) >= 52 else max(highs)
        w52_low  = min(lows[-52:])  if len(lows)  >= 52 else min(lows)
        watr     = _atr(highs, lows, closes, n=8)
        return {
            "w52_high":      w52_high,
            "w52_low":       w52_low,
            "weekly_atr":    watr,
            "weekly_closes": closes[-4:],
        }
    except Exception as e:
        print(f"[Portfolio] YF WEEKLY {yf_symbol}: {e}")
        return {}


# ── MEXC perpetuals ───────────────────────────────────────────────────────────

_MEXC_CACHE = {}      # module-level cache: {symbol: ticker_dict}
_MEXC_CACHE_TS = 0.0  # timestamp of last successful fetch
_MEXC_TTL = 300       # seconds before cache is considered stale (5 min)


def _mexc_fetch_all():
    """Fetch all MEXC contract tickers and cache by symbol (upper-case).
    Cache expires after _MEXC_TTL seconds to prevent stale data in long-lived processes."""
    global _MEXC_CACHE, _MEXC_CACHE_TS
    if _MEXC_CACHE and (time.time() - _MEXC_CACHE_TS) < _MEXC_TTL:
        return _MEXC_CACHE
    try:
        r = requests.get(
            "https://contract.mexc.com/api/v1/contract/ticker",
            timeout=15, headers=CHROME_HDR,
        )
        if r.status_code != 200:
            return _MEXC_CACHE or {}
        data = r.json()
        if not data.get("success"):
            return _MEXC_CACHE or {}
        _MEXC_CACHE = {
            t["symbol"].upper(): t
            for t in data.get("data", [])
            if isinstance(t, dict) and t.get("symbol")
        }
        _MEXC_CACHE_TS = time.time()
        return _MEXC_CACHE
    except Exception as e:
        print(f"[Portfolio] MEXC fetch error: {e}")
        return _MEXC_CACHE or {}


def _mexc_first(candidates):
    """Try candidate MEXC symbols (case-insensitive), return first match."""
    tickers = _mexc_fetch_all()
    for sym in candidates:
        t = tickers.get(sym.upper())
        if not t:
            continue
        try:
            price = float(t.get("lastPrice") or 0) or None
            if not price:
                continue
            fr  = t.get("fundingRate")
            oi  = t.get("openInterest")        # contracts (base asset)
            oiv = t.get("openInterestValue")   # USDT value (preferred)
            oi_bn = None
            if oiv:
                oi_bn = round(float(oiv) / 1e9, 2)
            elif oi:
                oi_bn = round(float(oi) * price / 1e9, 2)
            return {
                "symbol":       sym.upper(),
                "price":        price,
                "funding_rate": round(float(fr) * 100, 4) if fr else None,
                "oi_usd_bn":    oi_bn,
            }
        except Exception:
            continue
    return None


# ── FRED API ─────────────────────────────────────────────────────────────────

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"


def _fred_latest(series_id, api_key, n_lookback=5):
    # type: (str, str, int) -> Optional[float]
    """Return the most-recent non-missing FRED observation for series_id."""
    try:
        r = requests.get(
            _FRED_BASE,
            params={
                "series_id":  series_id,
                "api_key":    api_key,
                "file_type":  "json",
                "sort_order": "desc",
                "limit":      n_lookback,
            },
            timeout=15,
            headers=CHROME_HDR,
        )
        if r.status_code != 200:
            print(f"[Portfolio] FRED {series_id}: HTTP {r.status_code}")
            return None
        for obs in r.json().get("observations", []):
            v = obs.get("value", ".")
            if v and v != ".":
                return float(v)
        return None
    except Exception as e:
        print(f"[Portfolio] FRED {series_id}: {e}")
        return None


def _fred_series(series_id, api_key, n=2):
    # type: (str, str, int) -> List[float]
    """Return list of last n non-missing FRED observations, newest first."""
    try:
        r = requests.get(
            _FRED_BASE,
            params={
                "series_id":  series_id,
                "api_key":    api_key,
                "file_type":  "json",
                "sort_order": "desc",
                "limit":      n * 3,
            },
            timeout=15,
            headers=CHROME_HDR,
        )
        if r.status_code != 200:
            return []
        results = []
        for obs in r.json().get("observations", []):
            v = obs.get("value", ".")
            if v and v != ".":
                results.append(float(v))
            if len(results) >= n:
                break
        return results
    except Exception as e:
        print(f"[Portfolio] FRED series {series_id}: {e}")
        return []


def get_wti_news(n_headlines=5):
    # type: (int) -> List[str]
    """Fetch recent WTI/oil/OPEC headlines from Yahoo Finance. Returns '[Mon DD] Title' strings."""
    headlines = []
    try:
        url = (
            "https://query1.finance.yahoo.com/v1/finance/search"
            "?q=crude+oil+WTI+OPEC&newsCount=8&quotesCount=0"
            "&lang=en-US&region=US&enableFuzzyQuery=false"
        )
        r = requests.get(url, timeout=10, headers=CHROME_HDR)
        if r.status_code == 200:
            for item in r.json().get("news", [])[:n_headlines]:
                title = (item.get("title") or "").strip()
                ts    = item.get("providerPublishTime", 0)
                if not title:
                    continue
                try:
                    dt_str = datetime.utcfromtimestamp(ts).strftime("%b %d")
                    headlines.append(f"[{dt_str}] {title}")
                except Exception:
                    headlines.append(title)
    except Exception as e:
        print(f"[Portfolio] WTI news fetch error: {e}")
    return headlines


_MEGA_CAPS = ["NVDA", "AAPL", "MSFT", "META", "GOOGL", "AMZN", "TSLA"]


def get_earnings_calendar(days_ahead=14):
    # type: (int) -> List[str]
    """Return mega-cap earnings due within days_ahead days. Returns 'TICKER (Mon DD)' strings."""
    upcoming = []  # type: List[str]
    now_ts  = datetime.utcnow().timestamp()
    cutoff  = now_ts + days_ahead * 86400
    for ticker in _MEGA_CAPS:
        try:
            url = (
                f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
                f"?modules=calendarEvents"
            )
            r = requests.get(url, timeout=10, headers=CHROME_HDR)
            if r.status_code != 200:
                continue
            earn_dates = (
                r.json()
                .get("quoteSummary", {})
                .get("result", [{}])[0]
                .get("calendarEvents", {})
                .get("earnings", {})
                .get("earningsDate", [])
            )
            for ed in earn_dates:
                ts = ed.get("raw", 0)
                if now_ts <= ts <= cutoff:
                    dt_str = datetime.utcfromtimestamp(ts).strftime("%b %d")
                    upcoming.append(f"{ticker} ({dt_str})")
                    break
        except Exception:
            pass
    return upcoming


def get_crs_data(fred_key=None, vix_spot=None):
    # type: (Optional[str], Optional[float]) -> Dict
    """
    Fetch Crash Risk Score component data.
    Yahoo Finance series are always fetched (no key needed).
    FRED series (HY OAS, 2s10s, TIPS, ISM, SLOOS, SOFR) require FRED_API_KEY in .env.
    Obtain a free key at https://fred.stlouisfed.org/docs/api/api_key.html
    """
    data = {}  # type: Dict

    # ── Yahoo Finance (always available) ──────────────────────────────────────
    vix9d, _  = _yf_fetch("^VIX9D", history=5)   # 9-day VIX (term structure signal)
    copper, _ = _yf_fetch("HG=F",   history=5)   # Copper futures $/lb
    gold_p, _ = _yf_fetch("GC=F",   history=5)   # Gold futures $/oz
    irx, _    = _yf_fetch("^IRX",   history=5)   # 13-week T-bill rate (%)
    tnx, _    = _yf_fetch("^TNX",   history=5)   # 10Y Treasury yield (%)

    data["vix_9d"]       = vix9d
    data["vix_spot"]     = vix_spot   # passed from already-fetched prices
    data["copper_price"] = copper
    data["gold_price"]   = gold_p

    # 3m10y spread derived from Yahoo Finance tickers
    if tnx is not None and irx is not None:
        data["curve_3m10y"] = round(tnx - irx, 3)
    else:
        data["curve_3m10y"] = None

    # ── FRED API (requires FRED_API_KEY) ──────────────────────────────────────
    if fred_key:
        data["hy_oas"]          = _fred_latest("BAMLH0A0HYM2", fred_key)   # HY OAS (bps)
        data["curve_2s10s"]     = _fred_latest("T10Y2Y",       fred_key)   # % (neg = inverted)
        data["tips_10y"]        = _fred_latest("DFII10",       fred_key)   # 10Y real yield %
        data["ism_pmi"]         = _fred_latest("NAPM",         fred_key, n_lookback=3)
        data["lending_std"]     = _fred_latest("DRTSCILM",     fred_key, n_lookback=5)
        data["sofr"]            = _fred_latest("SOFR",         fred_key)
        data["fed_funds_upper"] = _fred_latest("DFEDTARU",     fred_key)
        data["tga_balance"]     = _fred_latest("WTREGEN",      fred_key, n_lookback=5)
        data["rrp_balance"]     = _fred_latest("RRPONTSYD",    fred_key, n_lookback=5)
        # EIA crude oil inventories — weekly, thousands of barrels
        _crude = _fred_series("WCRSTUS1", fred_key, n=2)
        data["crude_inv_level_kb"] = int(_crude[0]) if _crude else None
        data["crude_inv_chg_kb"]   = (int(_crude[0] - _crude[1])
                                      if len(_crude) >= 2 else None)
        # Baker Hughes US oil rig count (weekly) + 2Y Treasury for implied cuts
        data["rig_count"]   = _fred_latest("RIGSOILNA", fred_key, n_lookback=5)
        data["us_2y_yield"] = _fred_latest("DGS2",      fred_key, n_lookback=3)
    else:
        for k in ("hy_oas", "curve_2s10s", "tips_10y", "ism_pmi",
                  "lending_std", "sofr", "fed_funds_upper",
                  "tga_balance", "rrp_balance",
                  "crude_inv_level_kb", "crude_inv_chg_kb",
                  "rig_count", "us_2y_yield"):
            data[k] = None

    return data


# ── Macro data (independent of crypto agent) ─────────────────────────────────

def get_macro_data():
    """Fetch global macro indicators needed for portfolio analysis.
    Reuses the same sources as crypto-agent/whale_tracker.py but standalone.
    """
    # Try importing from crypto-agent to avoid duplication
    crypto_agent = BASE_DIR.parent / "crypto-agent"
    if crypto_agent.exists():
        sys.path.insert(0, str(crypto_agent))
        try:
            from whale_tracker import get_macro_data as _crypto_macro
            return _crypto_macro()
        except Exception:
            pass

    # Standalone fallback (minimal macro — yields + USDJPY via Yahoo Finance)
    macro = {
        "us_10y": None, "us_30y": None,
        "japan_10y": None, "japan_30y": None,
        "usdjpy": None, "spx": None,
        "carry_regime": "UNKNOWN",
        "japan_stress": "UNKNOWN",
        "us_curve_status": "UNKNOWN",
    }
    for key, sym in [("us_10y", "^TNX"), ("us_30y", "^TYX"), ("spx", "^GSPC")]:
        val, _ = _yf_fetch(sym, history=5)
        macro[key] = val
    usdjpy, _ = _yf_fetch("USDJPY=X", history=10)
    macro["usdjpy"] = usdjpy
    return macro


def get_all_portfolio_data():
    """Fetch prices + derived stats for all portfolio assets."""
    result = {}

    for asset, yf_sym in YF_SYMBOLS.items():
        price, closes, highs, lows = _yf_fetch_full(yf_sym, history=60)
        atr14         = _atr(highs, lows, closes, 14)
        range_20_high = max(highs[-20:]) if len(highs) >= 20 else None
        range_20_low  = min(lows[-20:])  if len(lows)  >= 20 else None
        entry = {
            "asset":         asset,
            "yf_symbol":     yf_sym,
            "price":         price,
            "chg_1d":        _pct_chg(closes, 1),
            "chg_5d":        _pct_chg(closes, 5),
            "chg_30d":       _pct_chg(closes, 30),
            "ma_20":         _ma(closes, 20),
            "ma_50":         _ma(closes, 50),
            "closes_10d":    closes[-10:] if closes else [],
            "atr_14":        atr14,
            "range_20_high": range_20_high,
            "range_20_low":  range_20_low,
        }
        if price and entry["ma_20"] and entry["ma_50"]:
            entry["above_ma20"] = price > entry["ma_20"]
            entry["above_ma50"] = price > entry["ma_50"]
        if asset in ("WTI", "SPX"):
            entry["weekly"] = _yf_fetch_weekly(yf_sym)
        result[asset] = entry

    # Overlay MEXC perpetual data for tradable assets.
    # MEXC price is always primary for assets traded as perps — positions and
    # P&L must be calculated against the actual exchange price, not YF futures.
    for asset, candidates in MEXC_SYMBOLS.items():
        mexc = _mexc_first(candidates)
        if mexc:
            result[asset]["mexc_price"]    = mexc.get("price")
            result[asset]["mexc_symbol"]   = mexc.get("symbol")
            result[asset]["funding_rate"]  = mexc.get("funding_rate")
            result[asset]["oi_usd_bn"]     = mexc.get("oi_usd_bn")
            # MEXC perpetual price is primary; keep YF price as reference only
            result[asset]["yf_price"]      = result[asset].get("price")
            result[asset]["price"]         = mexc.get("price")

    # Extra context indicators
    vix, _    = _yf_fetch("^VIX",    history=5)
    eurusd, _ = _yf_fetch("EURUSD=X", history=5)
    dxy, _    = _yf_fetch("DX-Y.NYB", history=5)
    result["_vix"]    = vix
    result["_eurusd"] = eurusd
    result["_dxy"]    = dxy

    # Derived: WTI/Brent spread
    wti_p   = result.get("WTI", {}).get("price")
    brent_p = result.get("BRENT", {}).get("price")
    if wti_p and brent_p:
        result["wti_brent_spread"] = round(brent_p - wti_p, 2)

    # Gold/silver ratio — spot futures (USD/oz), standard market reference
    _gold_spot, _   = _yf_fetch("GC=F", history=5)
    _silver_spot, _ = _yf_fetch("SI=F", history=5)
    if _gold_spot and _silver_spot and _silver_spot > 0:
        result["gold_silver_ratio"] = round(_gold_spot / _silver_spot, 1)
    else:
        result["gold_silver_ratio"] = None

    print(
        f"[Portfolio] "
        + " ".join(
            f"{a}:{result[a].get('price', 'N/A')}"
            for a in PORTFOLIO_ASSETS
        )
    )
    return result
