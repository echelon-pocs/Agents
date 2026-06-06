"""
Whale Tracker — On-chain data fetching and profitable wallet discovery.

Chains covered: BTC, ETH, SOL, XRP, SUI, ONDO (ERC-20)
APIs used (all free): blockchain.info, Etherscan, Solana RPC, XRPL, Sui RPC, CoinGecko
"""

import json
import re
import sys
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

_SHARED = str(Path(__file__).resolve().parent.parent / "shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

from utils import CHROME_HDR  # noqa: E402

HALVING_DATE = datetime(2024, 4, 20)

# ─── Known institutional / named whale wallets ────────────────────────────────

KNOWN_WALLETS = {
    "BTC": {
        "Strategy (MicroStrategy)": "1P5ZEDWTKTFGxQjZphgWPQUpe554WKDfHQ",
        "Metaplanet":               "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3h",
        "Fidelity BTC Cold":        "bc1qd2gy3yv9gggfqz3kjcw5xt7g9xm7q8g8rqxkf",
        "Coinbase Cold 1":          "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo",
        "Binance Hot":              "1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s",
    },
    "ETH": {
        "Jump Trading":             "0x756D64Dc5eDb56740fC617628dC832DDBCfd373c",
        "Wintermute":               "0x4f3a120E72C76c22ae802D129F599BFDbc31cb81",
        "Justin Sun":               "0x3DdfA8eC3052539b6C9549F12cEA2C295cfF5296",
        "Abraxas Capital":          "0x6555e1CC97d3cbA6eAddebBCD7Ca51d75771e0B8",
        "DWF Labs":                 "0x562680a4dC50ed2f14d75BF31f494cfE0b8D10a1",
    },
    "SOL": {
        "Jump Crypto SOL":          "CakcnaRDHka2gXyfxNmREAqATHAAinHnGGAoWGdBdCkC",
        "Alameda (dormant)":        "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5",
        "Solana Foundation":        "mvines9iiHiQTysrwkJjGf2gb9Ex9jXJX8ns3qwf2kN",
    },
    "XRP": {
        "Ripple Escrow 1":          "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
        "Bitstamp Hot":             "rrpNnNLKrartuEqfJGpqyDwPj1BBN1ov77",
        "XRP ETF Flows":            "rN7n3473SaZBCG4dFL83w7PB5AMtGMCVDQ",
    },
    "SUI": {
        "Nasdaq SUI Staker":        "0x6b2f4b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b",
        "Mysten Labs Treasury":     "0x0000000000000000000000000000000000000000000000000000000000000005",
    },
    "ONDO": {
        "ONDO Foundation":          "0xb24ca28d4e2742907b0536de50be35f3e3fb3e8f",
        "Pantera Capital":          "0x3a4f40631a4f463c9d061d83c1f7bfba0bc68c68",
        "ONDO Whale 0xb5E4":        "0xb5E4Be6Da2aB1a02E3Df028b3f8b9948B49813a9",
    },
}

# ONDO token contract
ONDO_CONTRACT = "0xfAbA6f8e4a5E8Ab82F62fe7C39859FA577269BE3"

# Etherscan V2 base URL
ESCAN = "https://api.etherscan.io/v2/api"

COINGECKO_IDS = {
    # Tier 1
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "SOL":  "solana",
    "XRP":  "ripple",
    "BNB":  "binancecoin",
    "ONDO": "ondo-finance",
    # Tier 2
    "SUI":  "sui",
    "DOGE": "dogecoin",
    "ADA":  "cardano",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "DOT":  "polkadot",
    "ATOM": "cosmos",
    "LTC":  "litecoin",
    "BCH":  "bitcoin-cash",
    "UNI":  "uniswap",
    "AAVE": "aave",
    "OP":   "optimism",
    "ARB":  "arbitrum",
    "APT":  "aptos",
    "INJ":  "injective-protocol",
    "TIA":  "celestia",
    "HYPE": "hyperliquid",
    "TAO":  "bittensor",
    "WLD":  "worldcoin-org",
    "TRX":  "tron",
}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get(url: str, params: dict = None, timeout: int = 12,
         retries: int = 2, backoff: float = 2.0) -> Optional[dict]:
    """GET with retry on 429/503. retries=2 → up to 3 total attempts."""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "CryptoAgent/1.0"})
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 503) and attempt < retries:
                time.sleep(backoff * (2 ** attempt))
                continue
        except Exception:
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
    return None


# ─── Technical Indicators (computed from CoinGecko OHLCV) ───────────────────

# Fixed list for daily technical analysis
_TA_SYMBOLS = ["BTC", "ETH", "XRP", "SUI", "SOL", "WLD", "DOGE", "ADA", "ONDO", "TRX"]


def _ema(values, period):
    # type: (List[float], int) -> Optional[float]
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    for v in values[period:]:
        result = v * k + result * (1 - k)
    return result


def _rsi(closes, period=14):
    # type: (List[float], int) -> Optional[float]
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1 + rs)), 1)


def _bbands(closes, period=20, num_std=2):
    # type: (List[float], int, int) -> dict
    if len(closes) < period:
        return {}
    recent = closes[-period:]
    sma = sum(recent) / period
    variance = sum((x - sma) ** 2 for x in recent) / period
    std = variance ** 0.5
    upper = sma + num_std * std
    lower = sma - num_std * std
    current = closes[-1]
    width_pct = round((upper - lower) / sma * 100, 2) if sma > 0 else 0
    pct_b = round((current - lower) / (upper - lower), 3) if (upper - lower) > 0 else 0.5
    # Squeeze: current width < 80% of 20-period average width
    widths = []
    for i in range(period, len(closes) + 1):
        chunk = closes[i - period:i]
        s = sum(chunk) / period
        v = sum((x - s) ** 2 for x in chunk) / period
        widths.append((v ** 0.5) * 2 * num_std / s * 100 if s > 0 else 0)
    avg_width = sum(widths) / len(widths) if widths else width_pct
    squeeze = width_pct < avg_width * 0.80
    return {
        "bb_upper":     round(upper, 4),
        "bb_lower":     round(lower, 4),
        "bb_pct_b":     pct_b,
        "bb_width_pct": width_pct,
        "bb_squeeze":   squeeze,
    }


def _atr(highs, lows, closes, period=14):
    # type: (List[float], List[float], List[float], int) -> Optional[float]
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _macd(closes, fast=12, slow=26, signal_period=9):
    # type: (List[float], int, int, int) -> dict
    if len(closes) < slow + signal_period:
        return {}
    k_fast = 2.0 / (fast + 1)
    k_slow = 2.0 / (slow + 1)
    k_sig  = 2.0 / (signal_period + 1)
    # Seed from SMA
    ema_f = sum(closes[:fast]) / fast
    ema_s = sum(closes[:slow]) / slow
    # Advance fast EMA to slow start
    for c in closes[fast:slow]:
        ema_f = c * k_fast + ema_f * (1 - k_fast)
    # Build MACD line
    macd_line = []
    for c in closes[slow:]:
        ema_f = c * k_fast + ema_f * (1 - k_fast)
        ema_s = c * k_slow + ema_s * (1 - k_slow)
        macd_line.append(ema_f - ema_s)
    if len(macd_line) < signal_period:
        return {}
    # Signal line
    sig = sum(macd_line[:signal_period]) / signal_period
    for m in macd_line[signal_period:]:
        sig = m * k_sig + sig * (1 - k_sig)
    hist = macd_line[-1] - sig
    prev_hist = macd_line[-2] - sig if len(macd_line) > signal_period else hist
    if hist > prev_hist * 1.01:
        direction = "RISING"
    elif hist < prev_hist * 0.99:
        direction = "FALLING"
    else:
        direction = "FLAT"
    return {
        "macd_above_signal": macd_line[-1] > sig,
        "macd_hist_direction": direction,
    }


def _fetch_ohlcv(symbol):
    # type: (str) -> Optional[List[List[float]]]
    """Fetch 60-day daily OHLC from CoinGecko for a given symbol.
    60 days gives enough candles for MACD(12,26,9) which needs ≥35."""
    cg_id = COINGECKO_IDS.get(symbol)
    if not cg_id:
        return None
    data = _get(
        f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc",
        params={"vs_currency": "usd", "days": "90"},
        timeout=15,
    )
    if not isinstance(data, list) or len(data) < 35:
        return None
    return data  # [[ts_ms, open, high, low, close], ...]


def _rsi_signal(rsi):
    # type: (Optional[float]) -> str
    if rsi is None:
        return "N/A"
    if rsi < 30:
        return "OVERSOLD"
    if rsi < 45:
        return "WEAK"
    if rsi < 55:
        return "NEUTRAL"
    if rsi < 70:
        return "STRONG"
    return "OVERBOUGHT"


def compute_coin_technicals(symbol):
    # type: (str) -> dict
    """Compute RSI14, EMA20, BBands(20,2σ), ATR14, MACD(12,26,9) for one coin."""
    candles = _fetch_ohlcv(symbol)
    if not candles:
        return {"error": "no_data"}
    try:
        opens  = [c[1] for c in candles]
        highs  = [c[2] for c in candles]
        lows   = [c[3] for c in candles]
        closes = [c[4] for c in candles]
        current_price = closes[-1]

        rsi_val  = _rsi(closes)
        ema20    = _ema(closes, 20)
        bb       = _bbands(closes)
        atr_val  = _atr(highs, lows, closes)
        macd_res = _macd(closes)

        result = {
            "rsi_14":        rsi_val,
            "rsi_signal":    _rsi_signal(rsi_val),
        }

        if ema20 is not None:
            ema20_r = round(ema20, 4)
            dist    = round((current_price - ema20) / ema20 * 100, 2)
            result.update({
                "ema20":           ema20_r,
                "price_vs_ema20":  "ABOVE" if current_price >= ema20 else "BELOW",
                "ema20_dist_pct":  dist,
            })

        result.update(bb)

        if atr_val is not None:
            atr_pct = round(atr_val / current_price * 100, 2)
            result.update({
                "atr_14":           round(atr_val, 4),
                "atr_pct":          atr_pct,
                "atr_stop_1_5x":    round(atr_pct * 1.5, 2),
                "atr_stop_2x":      round(atr_pct * 2.0, 2),
            })

        result.update(macd_res)
        return result

    except Exception as e:
        return {"error": str(e)}


def get_all_technicals():
    # type: () -> Dict[str, dict]
    """Fetch and compute technicals for all fixed-list symbols in parallel."""
    results = {}  # type: Dict[str, dict]
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(compute_coin_technicals, sym): sym
                   for sym in _TA_SYMBOLS}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                results[sym] = future.result()
            except Exception as e:
                results[sym] = {"error": str(e)}
    ok  = sum(1 for v in results.values() if "error" not in v)
    err = sum(1 for v in results.values() if "error" in v)
    print(f"[Technicals] computed {ok}/{len(_TA_SYMBOLS)} coins OK, {err} errors")
    return results


# Binance spot symbols for assets that CoinGecko may miss or rate-limit
_BINANCE_SPOT = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "BNB": "BNBUSDT", "SUI": "SUIUSDT",
    "DOGE": "DOGEUSDT", "ADA": "ADAUSDT", "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT", "DOT": "DOTUSDT", "ATOM": "ATOMUSDT",
    "LTC": "LTCUSDT", "BCH": "BCHUSDT", "UNI": "UNIUSDT",
    "AAVE": "AAVEUSDT", "OP": "OPUSDT", "ARB": "ARBUSDT",
    "APT": "APTUSDT", "INJ": "INJUSDT", "TIA": "TIAUSDT",
    "ONDO": "ONDOUSDT",
    # Note: HYPE and TAO are NOT on Binance — use MEXC fallback below
}

# MEXC spot symbols for tokens not on Binance (HYPE, TAO, etc.)
# These are fetched from MEXC *always* — not just as a fallback —
# because CoinGecko free tier may return stale cached prices for newer tokens.
_MEXC_SPOT = {
    "HYPE": "HYPEUSDT",
    "TAO":  "TAOUSDT",
}

# Assets that should always use MEXC as primary (bypasses CoinGecko for these)
_ALWAYS_MEXC = set(_MEXC_SPOT.keys())


def _fetch_binance_spot_prices(symbols):
    # type: (list) -> Dict[str, float]
    """Bulk-fetch Binance spot prices. Only queries symbols known to be on Binance."""
    pairs = [_BINANCE_SPOT[s] for s in symbols if s in _BINANCE_SPOT]
    if not pairs:
        return {}
    try:
        import json as _json
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbols": _json.dumps(pairs)},
            timeout=10,
        )
        if r.status_code != 200:
            return {}
        pair_to_price = {item["symbol"]: float(item["price"])
                         for item in r.json() if item.get("price")}
        return {sym: pair_to_price[_BINANCE_SPOT[sym]]
                for sym in symbols
                if sym in _BINANCE_SPOT and _BINANCE_SPOT[sym] in pair_to_price}
    except Exception as e:
        print(f"[Prices] Binance fallback error: {e}")
        return {}


def _fetch_mexc_spot_prices(symbols):
    # type: (list) -> Dict[str, float]
    """Fetch individual MEXC spot prices for tokens not on Binance (HYPE, TAO)."""
    result = {}
    for sym in symbols:
        pair = _MEXC_SPOT.get(sym)
        if not pair:
            continue
        try:
            r = requests.get(
                "https://api.mexc.com/api/v3/ticker/price",
                params={"symbol": pair},
                timeout=8,
                headers=CHROME_HDR,
            )
            if r.status_code == 200:
                price = float(r.json().get("price", 0))
                if price:
                    result[sym] = price
        except Exception as e:
            print(f"[Prices] MEXC spot {sym} error: {e}")
    return result


def get_prices(prices: Dict[str, float] = None) -> Dict[str, float]:
    """Fetch current USD prices for all tracked assets.
    Primary: CoinGecko. Fallback 1: Binance spot. Fallback 2: MEXC spot (HYPE, TAO).
    If a pre-fetched dict is passed, return it immediately (cache passthrough).
    """
    if prices is not None:
        return prices

    result = {}  # type: Dict[str, float]

    # Always fetch MEXC-primary assets first (HYPE, TAO) — never rely on CoinGecko
    # for these because CoinGecko free tier may return stale cached prices.
    mexc_primary = _fetch_mexc_spot_prices(list(_ALWAYS_MEXC))
    result.update(mexc_primary)
    if mexc_primary:
        print(f"[Prices] MEXC primary: " +
              " ".join(f"{s}={v}" for s, v in mexc_primary.items()))

    # CoinGecko — fetch remaining assets (skip ALWAYS_MEXC)
    cg_ids = {s: cg for s, cg in COINGECKO_IDS.items() if s not in _ALWAYS_MEXC}
    ids = ",".join(cg_ids.values())
    data = _get("https://api.coingecko.com/api/v3/simple/price",
                params={"ids": ids, "vs_currencies": "usd"})
    if data:
        for sym, cg_id in cg_ids.items():
            price = data.get(cg_id, {}).get("usd", 0)
            if price:
                result[sym] = price

    # Binance spot fallback for mainstream tokens still missing after CoinGecko
    missing_binance = [s for s in cg_ids if not result.get(s) and s in _BINANCE_SPOT]
    if missing_binance:
        print(f"[Prices] CoinGecko miss → Binance: {missing_binance}")
        result.update(_fetch_binance_spot_prices(missing_binance))

    still_missing = [s for s in COINGECKO_IDS if not result.get(s)]
    if still_missing:
        print(f"[Prices] WARNING: no price for {still_missing}")

    return result

def get_market_globals() -> Dict:
    """Fear & Greed, BTC dominance, total market cap — free, no key."""
    result = {
        "fear_greed": None, "fear_greed_label": None,
        "btc_dominance": None, "total_market_cap_bn": None,
        "altcoin_season_index": None,
    }
    # Fear & Greed
    fg = _get("https://api.alternative.me/fng/", params={"limit": 1})
    if fg and fg.get("data"):
        d = fg["data"][0]
        result["fear_greed"]       = int(d.get("value", 0))
        result["fear_greed_label"] = d.get("value_classification", "")
    # BTC Dominance + total cap (CoinGecko global, free tier)
    gd = (_get("https://api.coingecko.com/api/v3/global") or {}).get("data", {})
    if gd:
        btc_d = round(gd.get("market_cap_percentage", {}).get("btc", 0), 1)
        result["btc_dominance"] = btc_d
        result["total_market_cap_bn"] = round(
            gd.get("total_market_cap", {}).get("usd", 0) / 1e9, 0)
        # Altcoin season proxy from BTC dominance
        result["altcoin_season_index"] = (
            80 if btc_d < 40 else
            60 if btc_d < 50 else
            35 if btc_d < 60 else 20
        )
    return result


def get_btc_cycle_metrics() -> Dict:
    """
    Compute BTC cycle position and proxy on-chain metrics via free APIs.
    Uses Binance klines for price history (no key, reliable).
    """
    today = datetime.utcnow()
    days_since_halving = (today - HALVING_DATE).days
    cycle_year = min(4, days_since_halving // 365 + 1)

    result = {
        "days_since_halving":        days_since_halving,
        "cycle_year":                cycle_year,
        "btc_200w_ma":               None,
        "btc_200w_ma_premium_pct":   None,
        "btc_realized_price_approx": None,
        "btc_mvrv_approx":           None,
        "btc_volume_ratio_24h_30d":  None,
    }

    # Fetch 1000 daily candles from Binance (free, no key)
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": "BTCUSDT", "interval": "1d", "limit": 1000},
                         timeout=20)
        if r.status_code == 200:
            klines = r.json()
            closes  = [float(k[4]) for k in klines]  # close price
            volumes = [float(k[5]) for k in klines]  # volume in BTC

            if len(closes) >= 365:
                result["btc_realized_price_approx"] = round(
                    sum(closes[-365:]) / 365, 0)

            if len(closes) >= 200:
                ma_window = min(len(closes), 1000)
                result["btc_200w_ma"] = round(
                    sum(closes[-ma_window:]) / ma_window, 0)

            if closes and result["btc_200w_ma"]:
                current = closes[-1]
                result["btc_200w_ma_premium_pct"] = round(
                    (current - result["btc_200w_ma"]) / result["btc_200w_ma"] * 100, 1)

            if closes and result["btc_realized_price_approx"]:
                result["btc_mvrv_approx"] = round(
                    closes[-1] / result["btc_realized_price_approx"], 2)

            if len(volumes) >= 30:
                avg_30d = sum(volumes[-31:-1]) / 30
                result["btc_volume_ratio_24h_30d"] = round(
                    volumes[-2] / avg_30d if avg_30d else 1.0, 2)

    except Exception as e:
        print(f"[CycleMetrics] fetch failed: {e}")

    print(f"[CycleMetrics] Y{cycle_year}/4 ({days_since_halving}d since halving) "
          f"| MA1000d: ${result['btc_200w_ma']:,.0f} "
          f"({result['btc_200w_ma_premium_pct']:+.1f}%) "
          f"| MVRV≈{result['btc_mvrv_approx']} "
          f"| Vol ratio: {result['btc_volume_ratio_24h_30d']}")
    return result


def get_historical_price(symbol: str, date_str: str) -> float:
    """Get USD price on a specific date (YYYY-MM-DD) from CoinGecko."""
    cg_id = COINGECKO_IDS.get(symbol)
    if not cg_id:
        return 0
    d = datetime.strptime(date_str, "%Y-%m-%d")
    data = _get(f"https://api.coingecko.com/api/v3/coins/{cg_id}/history",
                params={"date": d.strftime("%d-%m-%Y"), "localization": "false"})
    if data:
        return data.get("market_data", {}).get("current_price", {}).get("usd", 0)
    return 0

# ─── BTC ─────────────────────────────────────────────────────────────────────

def get_btc_large_transfers(min_usd: float = 1_000_000, prices: Dict[str, float] = None) -> List[Dict]:
    """Detect large BTC transfers in last 24h via Blockchair."""
    price_data = get_prices(prices)
    btc_price = price_data.get("BTC", 80000)
    min_btc = min_usd / btc_price

    data = _get("https://api.blockchair.com/bitcoin/transactions",
                params={"limit": 100, "s": "output_total(desc)",
                        "q": f"output_total({int(min_btc * 1e8)}..),"
                             f"time({(datetime.utcnow()-timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')}..)"})
    if not data:
        return []

    results = []
    for tx in data.get("data", []):
        results.append({
            "chain": "BTC",
            "hash": tx.get("hash", ""),
            "value_btc": tx.get("output_total", 0) / 1e8,
            "value_usd": (tx.get("output_total", 0) / 1e8) * btc_price,
            "time": tx.get("time", ""),
            "sender": tx.get("sender", "unknown"),
            "recipient": tx.get("recipient", "unknown"),
        })
    return results

def get_btc_wallet_activity(address: str) -> Dict:
    """Get recent BTC wallet balance and tx count."""
    data = _get(f"https://blockchain.info/rawaddr/{address}?limit=5")
    if not data:
        return {}
    return {
        "balance_btc": data.get("final_balance", 0) / 1e8,
        "tx_count": data.get("n_tx", 0),
        "recent_txs": len(data.get("txs", [])),
    }

# ─── ETH / ONDO ──────────────────────────────────────────────────────────────

ETHERSCAN_KEY = "YourEtherscanKey"  # set in .env as ETHERSCAN_API_KEY (optional, free tier works without)

def get_eth_large_transfers(etherscan_key: str = "", min_usd: float = 1_000_000, prices: Dict[str, float] = None) -> List[Dict]:
    """Detect large ETH transfers in last 24h via Etherscan."""
    price_data = get_prices(prices)
    eth_price = price_data.get("ETH", 2500)
    min_eth = min_usd / eth_price

    start_block = _estimate_block_from_hours_ago(24, "eth")
    params = {
        "chainid": 1, "module": "account", "action": "txlistinternal",
        "startblock": start_block, "endblock": 99999999,
        "sort": "desc", "apikey": etherscan_key or "YourKey",
    }
    data = _get("https://api.etherscan.io/v2/api", params=params)

    results = []
    seen = set()
    if data and data.get("status") == "1":
        for tx in data.get("result", [])[:200]:
            val_eth = int(tx.get("value", 0)) / 1e18
            if val_eth >= min_eth and tx.get("hash") not in seen:
                seen.add(tx["hash"])
                results.append({
                    "chain": "ETH",
                    "hash": tx.get("hash"),
                    "from": tx.get("from"),
                    "to": tx.get("to"),
                    "value_eth": val_eth,
                    "value_usd": val_eth * eth_price,
                    "timestamp": tx.get("timeStamp"),
                })
    return results

def get_ondo_large_transfers(etherscan_key: str = "", min_usd: float = 500_000, prices: Dict[str, float] = None) -> List[Dict]:
    """Detect large ONDO token transfers in last 24h via Etherscan."""
    price_data = get_prices(prices)
    ondo_price = price_data.get("ONDO", 0.45)

    start_block = _estimate_block_from_hours_ago(24, "eth")
    params = {
        "chainid": 1, "module": "account", "action": "tokentx",
        "contractaddress": ONDO_CONTRACT,
        "startblock": start_block, "endblock": 99999999,
        "sort": "desc", "apikey": etherscan_key or "YourKey",
    }
    data = _get("https://api.etherscan.io/v2/api", params=params)

    results = []
    if data and data.get("status") == "1":
        for tx in data.get("result", [])[:200]:
            decimals = int(tx.get("tokenDecimal", 18))
            amount = int(tx.get("value", 0)) / (10 ** decimals)
            value_usd = amount * ondo_price
            if value_usd >= min_usd:
                results.append({
                    "chain": "ONDO",
                    "hash": tx.get("hash"),
                    "from": tx.get("from"),
                    "to": tx.get("to"),
                    "amount_ondo": amount,
                    "value_usd": value_usd,
                    "timestamp": tx.get("timeStamp"),
                })
    return results

def _estimate_block_from_hours_ago(hours: int, chain: str) -> int:
    """Rough block number estimate for time range filtering."""
    blocks_per_hour = {"eth": 300, "bsc": 1200}
    result = _get(ESCAN, params={"chainid": 1, "module": "proxy",
                                 "action": "eth_blockNumber"})
    raw = (result or {}).get("result", "")
    if raw and raw.startswith("0x"):
        try:
            return int(raw, 16) - (hours * blocks_per_hour.get(chain, 300))
        except ValueError:
            pass
    return 21500000  # safe fallback


def _block_from_hours_ago(hours: int, etherscan_key: str = "") -> int:
    """Wrapper used by discovery functions — always targets ETH mainnet."""
    result = _get(ESCAN, params={"chainid": 1, "module": "proxy",
                                 "action": "eth_blockNumber",
                                 "apikey": etherscan_key or "YourKey"})
    raw = (result or {}).get("result", "")
    if raw and raw.startswith("0x"):
        try:
            return int(raw, 16) - (hours * 300)
        except ValueError:
            pass
    return 21500000

# ─── SOL ─────────────────────────────────────────────────────────────────────

def get_sol_large_transfers(min_usd: float = 1_000_000, prices: Dict[str, float] = None) -> List[Dict]:
    """Detect large SOL transfers via public Solana RPC."""
    price_data = get_prices(prices)
    sol_price = price_data.get("SOL", 95)

    # Query recent signatures from known large holders
    results = []
    for label, address in KNOWN_WALLETS.get("SOL", {}).items():
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": 5}]
        }
        try:
            r = requests.post("https://api.mainnet-beta.solana.com",
                              json=payload, timeout=10)
            if r.status_code == 200:
                sigs = r.json().get("result", [])
                for sig in sigs:
                    results.append({
                        "chain": "SOL",
                        "wallet_label": label,
                        "address": address,
                        "signature": sig.get("signature", ""),
                        "slot": sig.get("slot", 0),
                        "err": sig.get("err"),
                    })
        except Exception:
            pass
    return results

# ─── XRP ─────────────────────────────────────────────────────────────────────

def get_xrp_large_transfers(min_usd: float = 500_000, prices: Dict[str, float] = None) -> List[Dict]:
    """Detect large XRP payments via XRPL public API."""
    price_data = get_prices(prices)
    xrp_price = price_data.get("XRP", 1.45)
    min_xrp = min_usd / xrp_price

    data = _get("https://data.ripple.com/v2/transactions",
                params={"type": "Payment", "descending": "true",
                        "limit": 50, "result": "tesSUCCESS",
                        "start": (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    results = []
    if data:
        for tx in data.get("transactions", []):
            delivered = tx.get("meta", {}).get("delivered_amount", {})
            if isinstance(delivered, str):  # XRP amount in drops
                xrp_amount = int(delivered) / 1e6
                if xrp_amount >= min_xrp:
                    results.append({
                        "chain": "XRP",
                        "hash": tx.get("hash", ""),
                        "from": tx.get("tx", {}).get("Account", ""),
                        "to": tx.get("tx", {}).get("Destination", ""),
                        "amount_xrp": xrp_amount,
                        "value_usd": xrp_amount * xrp_price,
                    })
    return results

# ─── Profitable Wallet Discovery — Early Buyer Method ────────────────────────
#
# Strategy: work BACKWARDS from confirmed price moves.
# If token X is up >20% vs 30 days ago, find wallets that bought large amounts
# BEFORE the move (first 5 days of the window). Those wallets are proven smart money.
# Track what they're buying TODAY as a copy-trade signal.
#
# Why this works vs. the old Uniswap event scan:
#   - Old: extracted router addresses (not users) from Swap event topics → always empty
#   - New: uses tokentx (actual token transfers to/from real wallets) → reliable data
#   - Old: paired arbitrary consecutive swaps as round-trips → meaningless P&L
#   - New: uses real price appreciation over measured window → verified profit
#
# Weighting note: whale signals remain at 70% / TA 30%.
# Discovered wallets feed INTO the whale signal layer — their current positions
# count as whale bullish/bearish signals for the assets they're touching.

# Tokens to scan for early buyers. Add any ERC-20 contract here.
SCANNABLE_TOKENS: Dict[str, str] = {
    "ONDO": ONDO_CONTRACT,
    "UNI":  "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
    "LINK": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
    "AAVE": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
}

# Minimum USD size to count as "serious" buy — filters noise and bots
MIN_BUY_USD = 75_000

# Exchange hot wallets to exclude (they're not buyers, just routing)
_EXCHANGE_ADDRS_FLAT: Optional[List[str]] = None

def _exchange_addrs() -> List[str]:
    global _EXCHANGE_ADDRS_FLAT
    if _EXCHANGE_ADDRS_FLAT is None:
        _EXCHANGE_ADDRS_FLAT = [
            a.lower()
            for addrs in EXCHANGE_HOT_WALLETS.get("ETH", {}).values()
            for a in addrs
        ]
    return _EXCHANGE_ADDRS_FLAT


def discover_early_buyers(
    etherscan_key: str = "",
    lookback_days: int = 30,
    entry_window_days: int = 7,
    min_profit_pct: float = 20,
    min_buy_usd: float = MIN_BUY_USD,
) -> List[Dict]:
    """
    Find wallets that bought a token during its accumulation phase
    (first `entry_window_days` of the lookback window) and now sit
    on >= min_profit_pct unrealised gain.

    Returns wallets sorted by profit%, ready to feed into whale signal layer.
    Each wallet also carries `current_holdings` so Claude can see what
    they're holding TODAY as a copy-trade signal.
    """
    prices = get_prices()
    found: Dict[str, Dict] = {}  # address → wallet info

    for symbol, contract in SCANNABLE_TOKENS.items():
        token_price_now = prices.get(symbol, 0)
        if not token_price_now:
            continue

        # Price 30 days ago
        date_entry = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        price_entry = get_historical_price(symbol, date_entry) or token_price_now
        time.sleep(0.3)  # CoinGecko rate limit

        gain_pct = (token_price_now - price_entry) / price_entry * 100 if price_entry else 0

        if gain_pct < min_profit_pct:
            # Token didn't move enough — skip (no confirmed smart money signal)
            continue

        print(f"[WhaleTracker] {symbol} up {gain_pct:.1f}% in {lookback_days}d — scanning early buyers...")

        # Get token transfers during the entry window (first entry_window_days)
        start_block = _block_from_hours_ago(lookback_days * 24, etherscan_key)
        end_block   = _block_from_hours_ago((lookback_days - entry_window_days) * 24, etherscan_key)

        data = _get(ESCAN, params={
            "chainid": 1, "module": "account", "action": "tokentx",
            "contractaddress": contract,
            "startblock": start_block, "endblock": end_block,
            "sort": "asc",
            "apikey": etherscan_key or "YourKey",
        })

        if not data or data.get("status") != "1":
            continue

        for tx in data.get("result", [])[:300]:
            buyer = tx.get("to", "").lower()

            # Skip: exchange routing, contract addresses (start with 0x000), null
            if not buyer or buyer in _exchange_addrs():
                continue
            if buyer.startswith("0x000000"):
                continue

            decimals = int(tx.get("tokenDecimal", 18))
            amount   = int(tx.get("value", 0)) / (10 ** decimals)
            buy_usd  = amount * price_entry

            if buy_usd < min_buy_usd:
                continue

            profit_pct  = gain_pct  # unrealised gain on this position
            current_val = amount * token_price_now

            if buyer not in found:
                found[buyer] = {
                    "address":        buyer,
                    "avg_profit_pct": 0.0,
                    "tokens_bought":  [],
                    "total_invested": 0.0,
                    "total_now":      0.0,
                    "trade_count":    0,
                    "source":         "early-buyer scan",
                    "chain":          "ETH",
                    "discovered":     datetime.utcnow().strftime("%Y-%m-%d"),
                }

            w = found[buyer]
            w["tokens_bought"].append({
                "symbol":      symbol,
                "amount":      round(amount, 2),
                "buy_usd":     round(buy_usd, 0),
                "current_usd": round(current_val, 0),
                "profit_pct":  round(profit_pct, 1),
            })
            w["total_invested"] += buy_usd
            w["total_now"]      += current_val
            w["trade_count"]    += 1

    # Compute blended avg_profit_pct and filter
    results = []
    for w in found.values():
        if w["total_invested"] <= 0:
            continue
        w["avg_profit_pct"] = round(
            (w["total_now"] - w["total_invested"]) / w["total_invested"] * 100, 2
        )
        if w["avg_profit_pct"] >= min_profit_pct:
            results.append(w)

    results.sort(key=lambda x: x["avg_profit_pct"], reverse=True)
    top = results[:15]

    if top:
        print(f"[WhaleTracker] Found {len(top)} early-buyer wallets "
              f"(avg profit range: {top[-1]['avg_profit_pct']}%–{top[0]['avg_profit_pct']}%)")
    else:
        print("[WhaleTracker] No early-buyer wallets found this run "
              "(all tracked tokens moved <20% in lookback window — normal in sideways markets)")

    return top


def get_profitable_wallet_current_activity(
    wallets: List[Dict],
    etherscan_key: str = "",
) -> List[Dict]:
    """
    For each discovered profitable wallet, check what ERC-20 tokens
    they received in the last 48h. This is the copy-trade signal:
    if a proven smart-money wallet is accumulating X right now, that's
    a high-weight bullish signal for X.
    """
    if not wallets:
        return []

    prices  = get_prices()
    signals = []
    start   = _block_from_hours_ago(48, etherscan_key)

    for w in wallets[:10]:  # check top 10 only to stay within rate limits
        addr = w.get("address", "")
        if not addr:
            continue

        data = _get(ESCAN, params={
            "chainid": 1, "module": "account", "action": "tokentx",
            "address": addr,
            "startblock": start, "endblock": 99999999,
            "sort": "desc",
            "apikey": etherscan_key or "YourKey",
        })

        if not data or data.get("status") != "1":
            continue

        for tx in data.get("result", [])[:20]:
            # Only inbound transfers (wallet is the buyer)
            if tx.get("to", "").lower() != addr.lower():
                continue
            if tx.get("from", "").lower() in _exchange_addrs():
                direction = "WITHDRAWAL_FROM_EXCHANGE"
            else:
                direction = "WALLET_TO_WALLET"

            symbol    = tx.get("tokenSymbol", "?")
            decimals  = int(tx.get("tokenDecimal", 18))
            amount    = int(tx.get("value", 0)) / (10 ** decimals)
            usd_val   = amount * prices.get(symbol, 0)

            if usd_val < 10_000:
                continue

            signals.append({
                "wallet":    addr,
                "wallet_profit_pct": w.get("avg_profit_pct", 0),
                "action":    "BUY",
                "symbol":    symbol,
                "amount":    round(amount, 2),
                "value_usd": round(usd_val, 0),
                "direction": direction,
                "timestamp": tx.get("timeStamp"),
            })

        time.sleep(0.15)  # stay within free tier rate limits

    return signals

# ─── Known exchange deposit addresses (bearish signal) ───────────────────────

EXCHANGE_HOT_WALLETS = {
    "ETH": {
        "Binance": ["0x28C6c06298d514Db089934071355E5743bf21d60",
                    "0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8"],
        "Coinbase": ["0x503828976D22510aad0201ac7EC88293211D23Da"],
        "Kraken": ["0x2910543Af39abA0Cd09dBb2D50200b3E800A63D2"],
        "OKX": ["0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b"],
    }
}

def classify_transfer_direction(from_addr: str, to_addr: str, chain: str = "ETH") -> str:
    """
    Returns 'DEPOSIT' (bearish), 'WITHDRAWAL' (bullish), or 'UNKNOWN'.
    Exchange deposits = selling pressure. Withdrawals = accumulation.
    """
    exchange_addrs = []
    for addrs in EXCHANGE_HOT_WALLETS.get(chain, {}).values():
        exchange_addrs.extend([a.lower() for a in addrs])

    from_l = from_addr.lower()
    to_l = to_addr.lower()

    if to_l in exchange_addrs:
        return "DEPOSIT_TO_EXCHANGE"   # bearish
    if from_l in exchange_addrs:
        return "WITHDRAWAL_FROM_EXCHANGE"  # bullish
    return "WALLET_TO_WALLET"

# ─── Macro Liquidity Regime Data ─────────────────────────────────────────────

def get_macro_data() -> Dict:
    """
    Fetch macro indicators for liquidity regime analysis.
    Sources: stooq.com (yields, SPX) — free, no key.
             Binance fapi — free, no key (BTC funding + OI as liquidation proxy).
    All fetches are independent; failures return None and are noted.
    """
    result: Dict = {
        "us_10y": None, "us_30y": None,
        "japan_10y": None, "japan_30y": None,
        "usdjpy": None, "usdjpy_5d_ago": None,
        "spx": None, "btc_funding_rate_pct": None, "btc_oi_usd_bn": None,
    }

    # stooq CSV helper: returns (latest_close, [last_n_closes])
    def _stooq(symbol: str, history: int = 1, _verbose: bool = False):
        try:
            r = requests.get(f"https://stooq.com/q/d/l/?s={symbol}&i=d",
                             timeout=12, headers=CHROME_HDR)
            if r.status_code != 200:
                if _verbose:
                    print(f"[stooq] {symbol}: HTTP {r.status_code}")
                return (None, [])
            lines = [l for l in r.text.strip().splitlines()
                     if l and not l.lower().startswith("date")]
            if not lines:
                if _verbose:
                    print(f"[stooq] {symbol}: no data rows (body={r.text[:80]!r})")
                return (None, [])
            closes = []
            for row in lines[-(history + 1):]:
                parts = row.split(",")
                if len(parts) >= 5:
                    try:
                        closes.append(float(parts[4]))
                    except ValueError:
                        pass
            if _verbose and not closes:
                print(f"[stooq] {symbol}: rows found but no close prices; last row={lines[-1]!r}")
            return (closes[-1] if closes else None, closes)
        except Exception as _e:
            if _verbose:
                print(f"[stooq] {symbol}: exception {_e}")
            return (None, [])

    # Yahoo Finance JSON helper: returns (latest_close, [last_n_closes])
    def _yfinance(symbol: str, history: int = 1):
        try:
            days = max(history * 2, 20)
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                   f"?interval=1d&range={days}d")
            r = requests.get(url, timeout=12, headers=CHROME_HDR)
            if r.status_code != 200:
                return (None, [])
            data = r.json()
            closes_raw = (data.get("chart", {}).get("result", [{}])[0]
                          .get("indicators", {}).get("quote", [{}])[0]
                          .get("close", []))
            closes = [round(c, 4) for c in closes_raw if c is not None]
            return (closes[-1] if closes else None, closes)
        except Exception:
            return (None, [])

    # Nasdaq Data Link (free public datasets — no key for USTREASURY/YIELD)
    def _nasdaq_yield(row_field: str):
        try:
            url = "https://data.nasdaq.com/api/v3/datasets/USTREASURY/YIELD.json?rows=2"
            r = requests.get(url, timeout=12, headers=CHROME_HDR)
            if r.status_code != 200:
                return None
            data = r.json()
            cols = data["dataset"]["column_names"]
            rows = data["dataset"]["data"]
            if not rows or row_field not in cols:
                return None
            idx = cols.index(row_field)
            val = rows[0][idx]
            return round(float(val), 4) if val is not None else None
        except Exception:
            return None

    # Bitfinex public ticker — no auth, gives USDJPY mid-price
    def _bitfinex_usdjpy():
        try:
            r = requests.get("https://api-pub.bitfinex.com/v2/ticker/tUSDJPY",
                             timeout=10)
            if r.status_code != 200:
                return None
            data = r.json()
            # [bid, bid_size, ask, ask_size, ..., last_price, ...]
            if isinstance(data, list) and len(data) >= 7:
                return round(float(data[6]), 4)
            return None
        except Exception:
            return None

    # AwesomeAPI — free FOREX rates, no key
    def _awesomeapi_usdjpy():
        try:
            r = requests.get("https://economia.awesomeapi.com.br/json/last/USD-JPY",
                             timeout=10)
            if r.status_code != 200:
                return None
            data = r.json()
            val = data.get("USDJPY", {}).get("bid") or data.get("USDJPY", {}).get("ask")
            return round(float(val), 4) if val else None
        except Exception:
            return None

    # Fetch with stooq primary, then Yahoo Finance, then Nasdaq Data Link
    def _yield_multi(stooq_sym: str, yf_sym: str, nasdaq_field: str = "", history: int = 1):
        val, hist = _stooq(stooq_sym, history)
        if val is None:
            val, hist = _yfinance(yf_sym, history)
        if val is None and nasdaq_field:
            val = _nasdaq_yield(nasdaq_field)
            hist = [val] if val else []
        return (val, hist)

    # MOF (Japan Ministry of Finance) publishes daily JGB yield CSV — no auth
    def _mof_jgb():
        """Return (10y, 30y) from MOF CSV, or (None, None) on failure."""
        urls = [
            "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv",
            "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv",
        ]
        for url in urls:
            try:
                r = requests.get(url, timeout=15, headers=CHROME_HDR)
                if r.status_code != 200:
                    print(f"[JGB] MOF CSV HTTP {r.status_code}: {url}")
                    continue
                all_lines = [l.strip() for l in r.text.strip().splitlines() if l.strip()]
                if not all_lines:
                    continue
                # Find header row to locate columns dynamically
                header_idx = None
                for idx, ln in enumerate(all_lines):
                    if re.search(r'(?i)date.*\b10', ln):
                        header_idx = idx
                        break
                if header_idx is not None:
                    hdr = [h.strip().strip('"').lower() for h in all_lines[header_idx].split(",")]
                    data_rows = [ln for ln in all_lines[header_idx + 1:] if ln and not ln.lower().startswith("date")]
                    col10 = next((i for i, h in enumerate(hdr) if h in ("10", "10y", "10yr", "10 yr")), None)
                    col30 = next((i for i, h in enumerate(hdr) if h in ("30", "30y", "30yr", "30 yr")), None)
                else:
                    # No header found — fall back to hardcoded positions
                    # MOF CSV: Date,1Y,2Y,3Y,4Y,5Y,6Y,7Y,8Y,9Y,10Y,15Y,20Y,25Y,30Y,40Y
                    data_rows = [ln for ln in all_lines if not ln.lower().startswith("date") and not ln.lower().startswith('"date')]
                    col10, col30 = 10, 14
                if not data_rows:
                    continue
                parts = [p.strip().strip('"') for p in data_rows[-1].split(",")]
                def _f(idx):
                    if idx is None:
                        return None
                    try:
                        v = parts[idx]
                        return round(float(v), 4) if v else None
                    except (IndexError, ValueError):
                        return None
                j10, j30 = _f(col10), _f(col30)
                if j10 is not None or j30 is not None:
                    return (j10, j30)
            except Exception as _e:
                print(f"[JGB] MOF CSV exception: {_e}")
                continue
        return (None, None)

    def _jgb_nasdaq():
        """Nasdaq Data Link MOFJ public dataset — Japan JGB rates, no auth needed."""
        try:
            r = requests.get(
                "https://data.nasdaq.com/api/v3/datasets/MOFJ/INTEREST_RATE_JAPAN.json?rows=3",
                timeout=15, headers=CHROME_HDR,
            )
            if r.status_code != 200:
                return (None, None)
            ds   = r.json().get("dataset", {})
            cols = [c.lower() for c in ds.get("column_names", [])]
            rows = ds.get("data", [])
            if not rows:
                return (None, None)
            row = rows[0]
            def _col(*keywords):
                for kw in keywords:
                    for i, c in enumerate(cols):
                        if kw in c:
                            try:
                                v = row[i]
                                return round(float(v), 4) if v else None
                            except (TypeError, ValueError):
                                pass
                return None
            return (_col("10 year", "10-year", "10y", "10yr"),
                    _col("30 year", "30-year", "30y", "30yr"))
        except Exception:
            return (None, None)

    def _jgb_fred():
        """FRED API for Japan 10Y yield (IRLTLT01JPM156N). Optional FRED_API_KEY in .env."""
        try:
            import os as _os
            import os.path as _osp
            fred_key = _os.environ.get("FRED_API_KEY", "")
            if not fred_key:
                env_path = _osp.join(_osp.dirname(_osp.abspath(__file__)), ".env")
                if _osp.exists(env_path):
                    with open(env_path) as _ef:
                        for _ln in _ef:
                            _ln = _ln.strip()
                            if _ln.startswith("FRED_API_KEY="):
                                fred_key = _ln.split("=", 1)[1].strip()
            if not fred_key:
                return (None, None)
            r = requests.get(
                "https://api.stlouisfed.org/fred/series/observations"
                "?series_id=IRLTLT01JPM156N&sort_order=desc&limit=3"
                "&api_key=" + fred_key + "&file_type=json",
                timeout=15,
            )
            if r.status_code == 200:
                obs = r.json().get("observations", [])
                if obs and obs[0].get("value", ".") != ".":
                    return (round(float(obs[0]["value"]), 4), None)
        except Exception:
            pass
        return (None, None)

    def _jgb_yfinance_try():
        """Yahoo Finance fallback — Japan 10Y/30Y via Reuters-style suffix."""
        j10, j30 = None, None
        for sym in ("JP10YT=RR", "^JN10Y", "IRJPY=R"):
            v, _ = _yfinance(sym, history=3)
            if v is not None:
                j10 = v
                break
        for sym in ("JP30YT=RR", "^JN30Y"):
            v, _ = _yfinance(sym, history=3)
            if v is not None:
                j30 = v
                break
        return (j10, j30)

    def _jgb_worldgov():
        """worldgovernmentbonds.com — scrape Japan 10Y and 30Y yield."""
        j10, j30 = None, None
        try:
            r = requests.get(
                "http://www.worldgovernmentbonds.com/country/japan/",
                timeout=15, headers=CHROME_HDR,
            )
            if r.status_code != 200:
                return (None, None)
            text = r.text
            for label, years in (("10 Years", "10"), ("30 Years", "30")):
                m = re.search(
                    re.escape(label) + r'.*?(\d+\.\d+)%',
                    text, re.DOTALL
                )
                if m:
                    val = round(float(m.group(1)), 4)
                    if years == "10":
                        j10 = val
                    else:
                        j30 = val
        except Exception:
            pass
        return (j10, j30)

    def _jgb_boj():
        """Bank of Japan official statistics page — scrape yield values."""
        try:
            r = requests.get(
                "https://www.boj.or.jp/en/statistics/market/interest/index.htm",
                timeout=15, headers=CHROME_HDR,
            )
            if r.status_code != 200:
                return (None, None)
            text = r.text
            m10 = re.search(r'10.year[^<]*?(\d+\.\d{2,4})', text, re.IGNORECASE)
            m30 = re.search(r'30.year[^<]*?(\d+\.\d{2,4})', text, re.IGNORECASE)
            j10 = round(float(m10.group(1)), 4) if m10 else None
            j30 = round(float(m30.group(1)), 4) if m30 else None
            return (j10, j30)
        except Exception:
            return (None, None)

    # ── Fetch independent series in parallel ─────────────────────────────────
    def _fetch_us10y():
        return ("us_10y", _yield_multi("10ustb.b", "^TNX", "10 YR"))

    def _fetch_us30y():
        return ("us_30y", _yield_multi("30ustb.b", "^TYX", "30 YR"))

    def _fetch_spx():
        return ("spx", _yield_multi("^spx", "^GSPC"))

    def _fetch_jgb():
        """Run full JGB waterfall: stooq → MOF → Nasdaq → FRED → YF → worldgov → BOJ."""
        j10, j30 = None, None
        # stooq candidates
        for _sym in ("10jgbs.b", "10jgb.b", "jgbs10.b", "10jpb.b"):
            _v, _ = _stooq(_sym, _verbose=True)
            if _v is not None:
                j10 = _v
                print(f"[JGB] stooq 10Y OK: {_sym} = {_v}")
                break
        for _sym in ("30jgbs.b", "30jgb.b", "jgbs30.b", "30jpb.b"):
            _v, _ = _stooq(_sym, _verbose=True)
            if _v is not None:
                j30 = _v
                print(f"[JGB] stooq 30Y OK: {_sym} = {_v}")
                break
        if j10 is None or j30 is None:
            _j10, _j30 = _mof_jgb()
            print(f"[JGB] MOF CSV: 10Y={_j10} 30Y={_j30}")
            if j10 is None: j10 = _j10
            if j30 is None: j30 = _j30
        if j10 is None or j30 is None:
            _j10, _j30 = _jgb_nasdaq()
            print(f"[JGB] Nasdaq MOFJ: 10Y={_j10} 30Y={_j30}")
            if j10 is None: j10 = _j10
            if j30 is None: j30 = _j30
        if j10 is None:
            _j10, _ = _jgb_fred()
            print(f"[JGB] FRED: 10Y={_j10}")
            if _j10 is not None: j10 = _j10
        if j10 is None or j30 is None:
            _j10, _j30 = _jgb_yfinance_try()
            print(f"[JGB] Yahoo Finance: 10Y={_j10} 30Y={_j30}")
            if j10 is None: j10 = _j10
            if j30 is None: j30 = _j30
        if j10 is None or j30 is None:
            _j10, _j30 = _jgb_worldgov()
            print(f"[JGB] worldgovernmentbonds.com: 10Y={_j10} 30Y={_j30}")
            if j10 is None: j10 = _j10
            if j30 is None: j30 = _j30
        if j10 is None or j30 is None:
            _j10, _j30 = _jgb_boj()
            print(f"[JGB] BOJ stats: 10Y={_j10} 30Y={_j30}")
            if j10 is None: j10 = _j10
            if j30 is None: j30 = _j30
        print(f"[JGB] Final: 10Y={j10} 30Y={j30}")
        return ("jgb", (j10, j30))

    def _fetch_usdjpy():
        usdjpy_now, usdjpy_hist = _stooq("usdjpy", history=8)
        if usdjpy_now is None:
            usdjpy_now, usdjpy_hist = _yfinance("USDJPY=X", history=8)
        if usdjpy_now is None:
            usdjpy_now = _bitfinex_usdjpy()
            usdjpy_hist = [usdjpy_now] if usdjpy_now else []
        if usdjpy_now is None:
            usdjpy_now = _awesomeapi_usdjpy()
            usdjpy_hist = [usdjpy_now] if usdjpy_now else []
        return ("usdjpy", (usdjpy_now, usdjpy_hist))

    def _fetch_binance_fr():
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                             params={"symbol": "BTCUSDT"}, timeout=10)
            if r.status_code == 200:
                return ("btc_fr", round(float(r.json().get("lastFundingRate", 0)) * 100, 4))
        except Exception:
            pass
        return ("btc_fr", None)

    def _fetch_binance_oi():
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/openInterest",
                             params={"symbol": "BTCUSDT"}, timeout=10)
            if r.status_code == 200:
                oi_btc = float(r.json().get("openInterest", 0))
                _oi_prices = get_prices()
                btc_price = _oi_prices.get("BTC", 80000)
                return ("btc_oi", round(oi_btc * btc_price / 1e9, 2))
        except Exception:
            pass
        return ("btc_oi", None)

    _tasks = [_fetch_us10y, _fetch_us30y, _fetch_spx,
              _fetch_jgb, _fetch_usdjpy, _fetch_binance_fr, _fetch_binance_oi]

    with ThreadPoolExecutor(max_workers=len(_tasks)) as _pool:
        _futures = {_pool.submit(fn): fn.__name__ for fn in _tasks}
        for _fut in as_completed(_futures):
            try:
                _key, _val = _fut.result()
            except Exception as _exc:
                print(f"[MacroData] {_futures[_fut]} raised: {_exc}")
                continue
            if _key == "us_10y":
                result["us_10y"], _ = _val
            elif _key == "us_30y":
                result["us_30y"], _ = _val
            elif _key == "spx":
                result["spx"], _ = _val
            elif _key == "jgb":
                result["japan_10y"], result["japan_30y"] = _val
            elif _key == "usdjpy":
                _usdjpy_now, _usdjpy_hist = _val
                result["usdjpy"] = _usdjpy_now
                if len(_usdjpy_hist) >= 6:
                    result["usdjpy_5d_ago"] = _usdjpy_hist[-6]
            elif _key == "btc_fr":
                if _val is not None:
                    result["btc_funding_rate_pct"] = _val
            elif _key == "btc_oi":
                if _val is not None:
                    result["btc_oi_usd_bn"] = _val

    # Derived signals
    if result["us_10y"] and result["us_30y"]:
        spread = round(result["us_30y"] - result["us_10y"], 3)
        result["us_curve_10_30_spread"] = spread
        result["us_curve_status"] = (
            "INVERTED" if spread < 0 else
            "FLAT"     if spread < 0.3 else
            "STEEP"
        )

    jgb = result["japan_30y"]
    if jgb is not None:
        result["japan_stress"] = (
            "CRITICAL" if jgb > 2.8 else
            "HIGH"     if jgb > 2.5 else
            "ELEVATED" if jgb > 2.0 else
            "NORMAL"
        )

    # Japan yield curve steepness (10Y–30Y spread): widening = BOJ losing control of long end
    j10 = result["japan_10y"]
    j30 = result["japan_30y"]
    if j10 is not None and j30 is not None:
        result["japan_curve_spread"] = round(j30 - j10, 3)

    # Yen carry trade regime — USDJPY weekly change drives classification
    usdjpy = result["usdjpy"]
    usdjpy_5d = result["usdjpy_5d_ago"]
    if usdjpy is not None:
        carry_regime = "CARRY_STABLE"
        usdjpy_weekly_chg_pct = None
        if usdjpy_5d and usdjpy_5d > 0:
            usdjpy_weekly_chg_pct = round((usdjpy - usdjpy_5d) / usdjpy_5d * 100, 2)
            result["usdjpy_weekly_chg_pct"] = usdjpy_weekly_chg_pct
            if usdjpy_weekly_chg_pct <= -3.0 or usdjpy < 140:
                carry_regime = "CARRY_COLLAPSE"   # August-2024-style event
            elif usdjpy_weekly_chg_pct <= -1.5 or usdjpy < 145:
                carry_regime = "CARRY_UNWIND"     # active unwind in progress
            elif usdjpy_weekly_chg_pct <= -0.8:
                carry_regime = "CARRY_STRESS"     # early warning
        elif usdjpy < 145:
            carry_regime = "CARRY_UNWIND"
        result["carry_regime"] = carry_regime

        # Architecture shift flag: if USDJPY making lower-highs over prior readings
        # stored in state, Claude evaluates the multi-run trend; here we just flag
        # if we're meaningfully below the 155+ range that defined prior stable carry
        result["carry_architecture_alert"] = usdjpy < 148 and carry_regime != "CARRY_STABLE"

    fr = result["btc_funding_rate_pct"]
    if fr is not None:
        result["btc_leverage_signal"] = (
            "EXTREME_LONGS"  if fr >  0.05 else
            "ELEVATED_LONGS" if fr >  0.02 else
            "EXTREME_SHORTS" if fr < -0.02 else
            "NEUTRAL"
        )

    print(f"[MacroData] US10Y:{result['us_10y']} US30Y:{result['us_30y']} "
          f"JGB10Y:{result['japan_10y']} JGB30Y:{result['japan_30y']} "
          f"USDJPY:{result['usdjpy']} ({result.get('usdjpy_weekly_chg_pct','?')}%/wk) "
          f"Carry:{result.get('carry_regime','?')} "
          f"SPX:{result['spx']} BTC_FR:{result['btc_funding_rate_pct']}% OI:{result['btc_oi_usd_bn']}B")
    return result


# ─── Main aggregator ─────────────────────────────────────────────────────────

def get_all_whale_data(etherscan_key: str = "",
                       existing_wallets: List[Dict] = None) -> Dict:
    """
    Aggregate all on-chain whale data into a structured dict for Claude.
    Merges known wallets with any previously discovered profitable wallets.
    """
    print("[WhaleTracker] Fetching on-chain data + technical indicators...")

    # Macro liquidity regime data (yields, SPX, BTC derivatives)
    try:
        macro_data = get_macro_data()
    except Exception as e:
        print(f"[WhaleTracker] Macro data fetch failed: {e}")
        macro_data = {}

    try:
        market_globals = get_market_globals()
    except Exception as e:
        print(f"[WhaleTracker] Market globals fetch failed: {e}")
        market_globals = {}

    try:
        cycle_metrics = get_btc_cycle_metrics()
    except Exception as e:
        print(f"[WhaleTracker] Cycle metrics fetch failed: {e}")
        cycle_metrics = {}

    prices = get_prices()

    # 1. Large transfers across chains (pass cached prices to avoid repeat CoinGecko calls)
    btc_txs = get_btc_large_transfers(min_usd=2_000_000, prices=prices)
    eth_txs = get_eth_large_transfers(etherscan_key, min_usd=1_000_000, prices=prices)
    ondo_txs = get_ondo_large_transfers(etherscan_key, min_usd=300_000, prices=prices)
    xrp_txs = get_xrp_large_transfers(min_usd=500_000, prices=prices)
    sol_activity = get_sol_large_transfers(min_usd=1_000_000, prices=prices)

    print(f"[WhaleTracker] BTC:{len(btc_txs)} ETH:{len(eth_txs)} "
          f"ONDO:{len(ondo_txs)} XRP:{len(xrp_txs)} SOL:{len(sol_activity)}")

    # 2. Classify ETH transfers (exchange flow direction)
    for tx in eth_txs:
        tx["direction"] = classify_transfer_direction(
            tx.get("from", ""), tx.get("to", ""), "ETH")

    for tx in ondo_txs:
        tx["direction"] = classify_transfer_direction(
            tx.get("from", ""), tx.get("to", ""), "ETH")

    # 3. Discover new profitable wallets via early-buyer method
    print("[WhaleTracker] Scanning for profitable wallets (early-buyer method)...")
    try:
        new_profitable = discover_early_buyers(
            etherscan_key=etherscan_key,
            lookback_days=30,
            entry_window_days=7,
            min_profit_pct=20,
            min_buy_usd=MIN_BUY_USD,
        )
    except Exception as e:
        print(f"[WhaleTracker] Profitable wallet scan failed: {e}")
        new_profitable = []

    # 4. Merge with existing tracked wallets (persist across runs)
    tracked_wallets = list(existing_wallets or [])
    existing_addrs = {w.get("address", "").lower() for w in tracked_wallets}
    for w in new_profitable:
        if w["address"].lower() not in existing_addrs:
            tracked_wallets.append(w)
            print(f"[WhaleTracker] NEW profitable wallet found: {w['address']} "
                  f"avg +{w['avg_profit_pct']}% over {w['trade_count']} trades")

    # 5. Get what those wallets are buying RIGHT NOW (copy-trade signal)
    print(f"[WhaleTracker] Checking current activity for {min(len(tracked_wallets), 10)} profitable wallets...")
    try:
        profitable_signals = get_profitable_wallet_current_activity(
            wallets=tracked_wallets,
            etherscan_key=etherscan_key,
        )
    except Exception as e:
        print(f"[WhaleTracker] Current activity check failed: {e}")
        profitable_signals = []

    if profitable_signals:
        print(f"[WhaleTracker] {len(profitable_signals)} copy-trade signals from profitable wallets")

    # 6. Technical indicators (RSI, EMA20, BBands, ATR, MACD) for fixed list
    try:
        technicals = get_all_technicals()
    except Exception as e:
        print(f"[WhaleTracker] Technical indicators failed: {e}")
        technicals = {}

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "macro": macro_data,
        "prices": prices,
        "technicals": technicals,
        "large_transfers": {
            "BTC": btc_txs[:5],
            "ETH": eth_txs[:5],
            "ONDO": ondo_txs[:5],
            "XRP": xrp_txs[:5],
            "SOL": sol_activity[:5],
        },
        "market_globals":  market_globals,
        "cycle_metrics":   cycle_metrics,
        "known_wallets": KNOWN_WALLETS,
        "profitable_wallets_discovered": tracked_wallets,
        "profitable_wallet_signals": profitable_signals,
        "summary": {
            "btc_large_moves": len(btc_txs),
            "eth_large_moves": len(eth_txs),
            "ondo_large_moves": len(ondo_txs),
            "xrp_large_moves": len(xrp_txs),
            "sol_active_wallets": len(sol_activity),
            "profitable_wallets_tracked": len(tracked_wallets),
            "profitable_wallet_signals_today": len(profitable_signals),
        },
    }
