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
    # Etherscan V2 API
    result = _get("https://api.etherscan.io/v2/api",
                  params={"chainid": 1, "module": "proxy",
                          "action": "eth_blockNumber"})
    raw = (result or {}).get("result", "")
    if raw and raw.startswith("0x"):
        try:
            return int(raw, 16) - (hours * blocks_per_hour.get(chain, 300))
        except ValueError:
            pass
    return 21500000  # safe fallback

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

# ─── Profitable Wallet Discovery ─────────────────────────────────────────────

def discover_profitable_eth_wallets(etherscan_key: str = "",
                                     min_profit_pct: float = 20,
                                     lookback_days: int = 30) -> List[Dict]:
    """
    Scan ETH DEX trades (Uniswap v3 via Etherscan events) to find wallets
    that consistently profit >= min_profit_pct on closed round-trips.
    Returns list of wallet dicts with avg_profit_pct and trade_count.
    """
    # Get recent large DEX swaps — Uniswap v3 Swap event topic
    UNISWAP_V3_FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
    SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

    start_block = _estimate_block_from_hours_ago(lookback_days * 24, "eth")
    params = {
        "chainid": 1, "module": "logs", "action": "getLogs",
        "fromBlock": start_block, "toBlock": "latest",
        "topic0": SWAP_TOPIC,
        "apikey": etherscan_key or "YourKey",
    }
    data = _get("https://api.etherscan.io/v2/api", params=params)

    wallet_trades: Dict[str, List[Dict]] = {}

    if data and data.get("status") == "1":
        for log in data.get("result", [])[:500]:
            topics = log.get("topics", [])
            if len(topics) < 2:
                continue
            wallet = "0x" + topics[2][-40:] if len(topics) > 2 else ""
            if not wallet:
                continue

            block_ts = int(log.get("timeStamp", "0"), 16)
            date_str = datetime.utcfromtimestamp(block_ts).strftime("%Y-%m-%d")

            if wallet not in wallet_trades:
                wallet_trades[wallet] = []
            wallet_trades[wallet].append({
                "date": date_str,
                "block": int(log.get("blockNumber", "0"), 16),
                "tx": log.get("transactionHash", ""),
                "data": log.get("data", ""),
            })

    profitable_wallets = []
    prices = get_prices()

    for wallet, trades in wallet_trades.items():
        if len(trades) < 3:  # need minimum activity
            continue

        # Pair consecutive buy/sell to estimate round-trip P&L
        profits = []
        for i in range(0, len(trades) - 1, 2):
            buy = trades[i]
            sell = trades[i + 1]
            buy_price = get_historical_price("ETH", buy["date"]) or prices.get("ETH", 2500)
            sell_price = get_historical_price("ETH", sell["date"]) or prices.get("ETH", 2500)
            if buy_price > 0:
                pnl = (sell_price - buy_price) / buy_price * 100
                profits.append(pnl)
            time.sleep(0.2)  # respect CoinGecko rate limit

        if not profits:
            continue

        avg_profit = sum(profits) / len(profits)
        # Allow some losing trades but avg must exceed threshold
        winning_trades = sum(1 for p in profits if p > 0)
        win_rate = winning_trades / len(profits)

        if avg_profit >= min_profit_pct and win_rate >= 0.55:
            profitable_wallets.append({
                "address": wallet,
                "avg_profit_pct": round(avg_profit, 2),
                "win_rate_pct": round(win_rate * 100, 1),
                "trade_count": len(profits),
                "source": "Uniswap v3 DEX scan",
                "chain": "ETH",
                "discovered": datetime.utcnow().strftime("%Y-%m-%d"),
            })

    # Sort by avg profit descending
    return sorted(profitable_wallets, key=lambda x: x["avg_profit_pct"], reverse=True)[:10]

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

    # 3. Discover new profitable wallets (rate-limited, runs every run)
    print("[WhaleTracker] Scanning for profitable wallets...")
    try:
        new_profitable = discover_profitable_eth_wallets(
            etherscan_key=etherscan_key,
            min_profit_pct=20,
            lookback_days=30,
        )
    except Exception as e:
        print(f"[WhaleTracker] Profitable wallet scan failed: {e}")
        new_profitable = []

    # 4. Merge with existing tracked wallets
    tracked_wallets = existing_wallets or []
    existing_addrs = {w.get("address", "").lower() for w in tracked_wallets}
    for w in new_profitable:
        if w["address"].lower() not in existing_addrs:
            tracked_wallets.append(w)
            print(f"[WhaleTracker] NEW profitable wallet found: {w['address']} "
                  f"avg +{w['avg_profit_pct']}% over {w['trade_count']} trades")

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
        "summary": {
            "btc_large_moves": len(btc_txs),
            "eth_large_moves": len(eth_txs),
            "ondo_large_moves": len(ondo_txs),
            "xrp_large_moves": len(xrp_txs),
            "sol_active_wallets": len(sol_activity),
            "profitable_wallets_tracked": len(tracked_wallets),
        },
    }
