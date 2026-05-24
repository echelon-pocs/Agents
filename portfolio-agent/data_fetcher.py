"""
Price and macro data fetcher for the Portfolio Agent.
Sources: Yahoo Finance (no key), MEXC public API (no key).
"""
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict

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


# ── MEXC perpetuals ───────────────────────────────────────────────────────────

_MEXC_CACHE = {}  # module-level cache: {symbol: ticker_dict}


def _mexc_fetch_all():
    """Fetch all MEXC contract tickers and cache by symbol (upper-case)."""
    global _MEXC_CACHE
    if _MEXC_CACHE:
        return _MEXC_CACHE
    try:
        r = requests.get(
            "https://contract.mexc.com/api/v1/contract/ticker",
            timeout=15, headers=CHROME_HDR,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        if not data.get("success"):
            return {}
        _MEXC_CACHE = {
            t["symbol"].upper(): t
            for t in data.get("data", [])
            if isinstance(t, dict) and t.get("symbol")
        }
        return _MEXC_CACHE
    except Exception as e:
        print(f"[Portfolio] MEXC fetch error: {e}")
        return {}


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

    # Overlay MEXC perpetual data for tradable assets
    for asset, candidates in MEXC_SYMBOLS.items():
        mexc = _mexc_first(candidates)
        if mexc:
            result[asset]["mexc_price"]    = mexc.get("price")
            result[asset]["mexc_symbol"]   = mexc.get("symbol")
            result[asset]["funding_rate"]  = mexc.get("funding_rate")
            result[asset]["oi_usd_bn"]     = mexc.get("oi_usd_bn")
            # Use MEXC price as primary if YF unavailable
            if result[asset]["price"] is None:
                result[asset]["price"] = mexc.get("price")

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

    print(
        f"[Portfolio] "
        + " ".join(
            f"{a}:{result[a].get('price', 'N/A')}"
            for a in PORTFOLIO_ASSETS
        )
    )
    return result
