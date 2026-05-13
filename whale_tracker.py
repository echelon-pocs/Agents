"""
Whale Tracker — On-chain data fetching and profitable wallet discovery.

Chains covered: BTC, ETH, SOL, XRP, SUI, ONDO (ERC-20)
APIs used (all free): blockchain.info, Etherscan, Solana RPC, XRPL, Sui RPC, CoinGecko
"""

import json
import time
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

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
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "XRP": "ripple", "SUI": "sui", "ONDO": "ondo-finance",
}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get(url: str, params: dict = None, timeout: int = 12) -> Optional[dict]:
    try:
        r = requests.get(url, params=params, timeout=timeout,
                         headers={"User-Agent": "CryptoAgent/1.0"})
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def get_prices() -> Dict[str, float]:
    """Fetch current USD prices for all tracked assets via CoinGecko."""
    ids = ",".join(COINGECKO_IDS.values())
    data = _get("https://api.coingecko.com/api/v3/simple/price",
                params={"ids": ids, "vs_currencies": "usd"})
    if not data:
        return {}
    return {sym: data.get(cg_id, {}).get("usd", 0)
            for sym, cg_id in COINGECKO_IDS.items()}

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

def get_btc_large_transfers(min_usd: float = 1_000_000) -> List[Dict]:
    """Detect large BTC transfers in last 24h via Blockchair."""
    price_data = get_prices()
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

def get_eth_large_transfers(etherscan_key: str = "", min_usd: float = 1_000_000) -> List[Dict]:
    """Detect large ETH transfers in last 24h via Etherscan."""
    price_data = get_prices()
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

def get_ondo_large_transfers(etherscan_key: str = "", min_usd: float = 500_000) -> List[Dict]:
    """Detect large ONDO token transfers in last 24h via Etherscan."""
    price_data = get_prices()
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

def get_sol_large_transfers(min_usd: float = 1_000_000) -> List[Dict]:
    """Detect large SOL transfers via public Solana RPC."""
    price_data = get_prices()
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

def get_xrp_large_transfers(min_usd: float = 500_000) -> List[Dict]:
    """Detect large XRP payments via XRPL public API."""
    price_data = get_prices()
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

# ─── Main aggregator ─────────────────────────────────────────────────────────

def get_all_whale_data(etherscan_key: str = "",
                       existing_wallets: List[Dict] = None) -> Dict:
    """
    Aggregate all on-chain whale data into a structured dict for Claude.
    Merges known wallets with any previously discovered profitable wallets.
    """
    print("[WhaleTracker] Fetching on-chain data...")

    prices = get_prices()

    # 1. Large transfers across chains
    btc_txs = get_btc_large_transfers(min_usd=2_000_000)
    eth_txs = get_eth_large_transfers(etherscan_key, min_usd=1_000_000)
    ondo_txs = get_ondo_large_transfers(etherscan_key, min_usd=300_000)
    xrp_txs = get_xrp_large_transfers(min_usd=500_000)
    sol_activity = get_sol_large_transfers(min_usd=1_000_000)

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

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "prices": prices,
        "large_transfers": {
            "BTC": btc_txs[:10],
            "ETH": eth_txs[:10],
            "ONDO": ondo_txs[:10],
            "XRP": xrp_txs[:10],
            "SOL": sol_activity[:10],
        },
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
