"""
Price and macro data fetcher for the Portfolio Agent.
Sources: Yahoo Finance (no key), Bybit public API (no key).
"""
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict

import requests

BASE_DIR = Path(__file__).parent

# Python 3.8 compatible headers
CHROME_HDR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

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


# ── Bybit perpetuals ──────────────────────────────────────────────────────────

def _bybit_ticker(symbol):
    """Return dict with price, funding_rate, oi_usd or None."""
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        items = r.json().get("result", {}).get("list", [])
        if not items:
            return None
        t = items[0]
        price = float(t.get("lastPrice", 0) or 0) or None
        fr    = t.get("fundingRate")
        oi    = t.get("openInterestValue")
        return {
            "price":        price,
            "funding_rate": round(float(fr) * 100, 4) if fr else None,
            "oi_usd_bn":    round(float(oi) / 1e9, 2) if oi else None,
        }
    except Exception:
        return None


def _bybit_first(symbols):
    """Try a list of Bybit symbols, return first successful result."""
    for sym in symbols:
        data = _bybit_ticker(sym)
        if data and data.get("price"):
            data["symbol"] = sym
            return data
    return None


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


# ── Asset universe ────────────────────────────────────────────────────────────

# Yahoo Finance tickers for each asset
YF_SYMBOLS = {
    "WTI":   "CL=F",       # WTI crude oil futures
    "BRENT": "BZ=F",       # Brent crude oil futures
    "SPX":   "^GSPC",      # S&P 500 index
    "VWCE":  "VWCE.DE",    # Vanguard FTSE All-World (acc) - XETRA
    "VWRL":  "VWRL.AS",    # Vanguard FTSE All-World (dist) - Euronext AMS
    "4GLD":  "4GLD.DE",    # Xetra-Gold ETP
    "8PSB":  "8PSB.F",     # ETC Group Physical Bitcoin - Frankfurt
}

# Bybit perpetual candidates (first working symbol used)
BYBIT_SYMBOLS = {
    "WTI":   ["OILUSDT", "USOILUSDT"],
    "BRENT": ["UKOILUSDT", "BRNTUSDT", "CRUDEOILUSDT"],
    "SPX":   ["SPX500USD", "SPXUSDT", "US500USD"],
}


def get_all_portfolio_data():
    """Fetch prices + derived stats for all portfolio assets."""
    result = {}

    for asset, yf_sym in YF_SYMBOLS.items():
        price, closes = _yf_fetch(yf_sym, history=60)
        entry = {
            "asset":      asset,
            "yf_symbol":  yf_sym,
            "price":      price,
            "chg_1d":     _pct_chg(closes, 1),
            "chg_5d":     _pct_chg(closes, 5),
            "chg_30d":    _pct_chg(closes, 30),
            "ma_20":      _ma(closes, 20),
            "ma_50":      _ma(closes, 50),
            "closes_10d": closes[-10:] if closes else [],
        }
        # Trend vs MAs
        if price and entry["ma_20"] and entry["ma_50"]:
            entry["above_ma20"] = price > entry["ma_20"]
            entry["above_ma50"] = price > entry["ma_50"]
        result[asset] = entry

    # Overlay Bybit perpetual data for tradable assets
    for asset, candidates in BYBIT_SYMBOLS.items():
        bybit = _bybit_first(candidates)
        if bybit:
            result[asset]["bybit_price"]   = bybit.get("price")
            result[asset]["bybit_symbol"]  = bybit.get("symbol")
            result[asset]["funding_rate"]  = bybit.get("funding_rate")
            result[asset]["oi_usd_bn"]     = bybit.get("oi_usd_bn")
            # Use Bybit price as primary if YF unavailable
            if result[asset]["price"] is None:
                result[asset]["price"] = bybit.get("price")

    # Derived: WTI/Brent spread
    wti_p   = result.get("WTI", {}).get("price")
    brent_p = result.get("BRENT", {}).get("price")
    if wti_p and brent_p:
        result["wti_brent_spread"] = round(brent_p - wti_p, 2)

    print(
        f"[Portfolio] "
        + " ".join(
            f"{a}:{result[a].get('price', 'N/A')}"
            for a in ["WTI", "BRENT", "SPX", "VWCE", "VWRL", "4GLD", "8PSB"]
        )
    )
    return result
