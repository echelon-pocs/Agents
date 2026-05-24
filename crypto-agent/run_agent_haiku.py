#!/usr/bin/env python3
"""
Crypto Market Intelligence Agent — Haiku 4.5 Runner

Flow:
  1. Fetch real on-chain whale data (whale_tracker.py)
  2. Pass data + state to Claude Haiku 4.5 for analysis
  3. Save report, update state, send email (email_sender.py)
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import anthropic

from whale_tracker import get_all_whale_data
from email_sender import send_report, build_subject

BASE_DIR = Path(__file__).parent


def sanitize_state(state):
    """
    Normalize state to a predictable structure regardless of what Claude wrote
    or what legacy format was on disk. Called after every load and after every
    save to ensure downstream code never sees malformed data.
    """
    if not isinstance(state, dict):
        state = {}

    # Lists that must contain only dicts with a 'symbol' key
    for key in ("open_positions", "active_setups"):
        raw = state.get(key, [])
        if not isinstance(raw, list):
            raw = []
        state[key] = [e for e in raw if isinstance(e, dict) and e.get("symbol")]

    # Lists that must simply be lists
    for key in ("alerted", "profitable_wallets_discovered"):
        if not isinstance(state.get(key), list):
            state[key] = []

    return state


def _slim_transfer(tx):
    return {k: v for k, v in tx.items()
            if k in ("chain", "value_usd", "direction", "amount_ondo",
                     "amount_xrp", "wallet_label", "slot", "err")}


def slim_whale_data(data):
    transfers = {
        chain: [_slim_transfer(tx) for tx in txs[:5]]   # top 5 not 10
        for chain, txs in data.get("large_transfers", {}).items()
    }
    # Drop known_wallet_labels entirely — Claude doesn't use them
    profitable = []
    for w in data.get("profitable_wallets_discovered", [])[:3]:   # top 3 not 8
        if not isinstance(w, dict):
            continue
        addr = w.get("address", "")
        bought_summary = ", ".join(
            f"{t.get('symbol','?')} +{t.get('profit_pct','?')}%"
            for t in w.get("tokens_bought", [])
            if isinstance(t, dict)
        )
        profitable.append({
            "addr":   (addr[:10] + "…") if addr else "?",
            "profit": f"+{w.get('avg_profit_pct', 0)}%",
            "trades": w.get("trade_count", 0),
            "bought": bought_summary or "—",
        })
    signals = [
        {k: v for k, v in s.items() if k != "wallet"}
        for s in data.get("profitable_wallet_signals", [])
        if isinstance(s, dict)
    ]
    return {
        "macro":              data.get("macro", {}),
        "market_globals":     data.get("market_globals", {}),
        "cycle_metrics":      data.get("cycle_metrics", {}),
        "prices":             data.get("prices", {}),
        "transfers":          transfers,
        "profitable_wallets": profitable,
        "profitable_signals": signals,
        "summary":            data.get("summary", {}),
    }


def load_instructions():
    with open(BASE_DIR / "CLAUDE.md") as f:
        return f.read()


def load_state():
    p = BASE_DIR / "state.json"
    if p.exists():
        try:
            with open(p) as f:
                return sanitize_state(json.load(f))
        except Exception as e:
            print(f"[Agent] WARNING: could not load state.json ({e}) — starting fresh")
    return sanitize_state({})


def save_state(state):
    with open(BASE_DIR / "state.json", "w") as f:
        json.dump(sanitize_state(state), f, indent=2)


def apply_pending_updates(state):
    pending_path = BASE_DIR / "pending_updates.json"
    if not pending_path.exists():
        return state, []
    try:
        updates = json.loads(pending_path.read_text())
        if not isinstance(updates, list):
            updates = []
    except Exception:
        return state, []
    if not updates:
        return state, []

    log = []
    positions = {p["symbol"]: p for p in state.get("open_positions", [])}
    setups    = {s["symbol"]: s for s in state.get("active_setups", [])}

    for u in updates:
        if not isinstance(u, dict):
            continue
        action = u.get("action")
        symbol = u.get("symbol", "")
        if not symbol and action not in ("STATUS", "HELP"):
            continue

        if action == "ENTER":
            price       = u.get("price", 0)
            size_usd    = u.get("size_usd")
            setup       = setups.get(symbol, {})
            # Telegram update direction takes precedence over the setup's direction
            direction   = u.get("direction") or setup.get("direction", "LONG")
            market_type = u.get("market_type", "spot")
            key = f"{symbol}_{direction}"  # allow both legs of a futures pair
            positions[key] = {
                "symbol":      symbol,
                "direction":   direction,
                "market_type": market_type,
                "entry_price": price,
                "entry_date":  u.get("timestamp", "")[:10],
                "stop_loss":   setup.get("stop_loss"),
                "target_1":    setup.get("target_1"),
                "target_2":    setup.get("target_2"),
                "size_usd":    size_usd,
                "tf":          u.get("tf") or setup.get("timeframe") or "UNKNOWN",
                "pnl_pct":     None,
                "notes":       "User confirmed via Telegram.",
            }
            log.append(f"ENTERED {direction} {symbol} ({market_type}) @ ${price:,}")

            # If no active setup exists for this symbol, create a placeholder so
            # Claude analyses it on the next run and fills in targets/stop/whale score.
            if symbol not in setups:
                setups[symbol] = {
                    "symbol":          symbol,
                    "direction":       direction,
                    "market_type":     market_type,
                    "conviction":      "UNKNOWN",
                    "entry_zone":      [price, price],
                    "stop_loss":       None,
                    "tp1":             None,
                    "tp2":             None,
                    "tp3":             None,
                    "r_r_ratio":       None,
                    "status":          "OPEN",
                    "whale_signal":    "UNKNOWN",
                    "composite_score": 0,
                    "rationale":       f"Opened via Telegram at ${price:,}. No prior setup — agent will analyse on next run.",
                    "timeframe":       "UNKNOWN",
                    "added":           u.get("timestamp", "")[:10],
                }
                log.append(f"AUTO-SETUP created for {symbol} (no prior setup found)")

        elif action == "CLOSE":
            # Build candidate keys: direction-specific first, then symbol-only fallback
            direction = u.get("direction")
            candidates = []
            if direction:
                candidates.append(f"{symbol}_{direction}")
            candidates.append(symbol)
            # Also match any key starting with symbol_ (covers both legs if no direction given)
            if not direction:
                candidates += [k for k in list(positions) if k.startswith(f"{symbol}_")]

            matched = next((k for k in candidates if k in positions), None)
            if matched:
                if u.get("partial"):
                    positions[matched]["notes"] = (
                        positions[matched].get("notes", "") + " | Partial close flagged."
                    )
                    log.append(f"PARTIAL CLOSE flagged: {matched}")
                else:
                    del positions[matched]
                    log.append(f"CLOSED {matched}")
            else:
                log.append(f"CLOSE {symbol}: not in open positions (ignored)")

        elif action == "NOTE":
            direction = u.get("direction")
            note_key  = f"{symbol}_{direction}" if direction else symbol
            if note_key not in positions:
                # fallback: first matching key
                note_key = next((k for k in positions if k == symbol or k.startswith(f"{symbol}_")), None)
            if note_key and note_key in positions:
                positions[note_key]["notes"] = (
                    positions[note_key].get("notes", "") + f" | {u.get('note', '')}"
                )
                log.append(f"NOTE added to {note_key}: {u.get('note', '')}")

    state["open_positions"] = list(positions.values())
    state["active_setups"]  = list(setups.values())
    try:
        pending_path.write_text("[]")
    except Exception as e:
        print(f"[Agent] WARNING: could not clear pending_updates.json: {e}")
    return state, log


def load_env():
    env_vars = {}
    p = BASE_DIR / ".env"
    if p.exists():
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
    return env_vars


def get_api_key(env):
    key = os.environ.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError(
            "ANTHROPIC_API_KEY not found.\n"
            "Add it to .env:  ANTHROPIC_API_KEY=sk-ant-..."
        )
    return key


def extract_state_from_response(text):
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    return {}


def extract_macro_bias(text):
    for bias in ["BIFURCATED", "BULLISH", "BEARISH", "NEUTRAL"]:
        if bias in text:
            return bias
    return "NEUTRAL"


def compute_position_analytics(positions, prices):
    """Pre-compute P&L, flags, and stop distances. Python is authoritative."""
    analytics = {}
    for pos in positions:
        sym       = pos.get("symbol", "")
        direction = pos.get("direction", "LONG")
        entry     = pos.get("entry_price")
        stop      = pos.get("stop_loss")
        t1        = pos.get("target_1")
        key       = f"{sym}_{direction}"
        current   = prices.get(sym)

        pnl = None
        if entry and current:
            pnl = (entry - current if direction == "SHORT" else current - entry) / entry * 100

        flags = []
        if pnl is not None:
            if pnl < -15:    flags.append("DANGER_LOSS")
            elif pnl < -10:  flags.append("HIGH_RISK_LOSS")
            elif pnl < -5:   flags.append("DRAWDOWN")
            if pnl > 20:     flags.append("TRAIL_10PCT")
            elif pnl > 10:   flags.append("TRAIL_5PCT")
            elif pnl > 5:    flags.append("TRAIL_BREAKEVEN")
        if stop is None:
            flags.append("NO_STOP_SET")
        elif current and stop:
            stop_dist_pct = abs(current - stop) / current * 100
            if stop_dist_pct < 2:
                flags.append("STOP_CLOSE")
        if t1 and current:
            if (direction == "LONG"  and current >= t1 * 0.97) or \
               (direction == "SHORT" and current <= t1 * 1.03):
                flags.append("T1_APPROACHING")

        analytics[key] = {
            "current_price": current,
            "pnl_pct":       round(pnl, 2) if pnl is not None else None,
            "flags":         flags,
        }
    return analytics


def compute_setup_statuses(setups, prices):
    """Pre-compute ENTER/APPROACHING/WAITING/INVALIDATED for each setup."""
    statuses = {}
    for setup in setups:
        sym       = setup.get("symbol", "")
        direction = setup.get("direction", "LONG")
        zone      = setup.get("entry_zone", [])
        stop      = setup.get("stop_loss")
        current   = prices.get(sym)

        if not current or not zone or len(zone) < 2:
            statuses[sym] = "WAITING"
            continue

        z_low, z_high = zone[0], zone[1]

        # Stop breach → invalidated
        if stop:
            if direction == "LONG"  and current < stop: statuses[sym] = "INVALIDATED"; continue
            if direction == "SHORT" and current > stop: statuses[sym] = "INVALIDATED"; continue

        if z_low <= current <= z_high:
            statuses[sym] = "ENTER"
        elif direction == "LONG":
            statuses[sym] = "APPROACHING" if current >= z_low * 0.97 else "WAITING"
        else:
            statuses[sym] = "APPROACHING" if current <= z_high * 1.03 else "WAITING"

    return statuses


def _update_usdjpy_history(history, current_usdjpy):
    """Keep last 4 weekly closes for carry architecture trend detection."""
    if not isinstance(history, list):
        history = []
    if current_usdjpy is not None:
        history = (history + [current_usdjpy])[-4:]
    return history


def merge_state_delta(prior_state, delta, macro_data, prices, profitable_wallets):
    """
    Claude outputs only analysis fields (delta). Python owns:
      last_run, btc_price, macro_snapshot, profitable_wallets_discovered, usdjpy_history.
    """
    if not isinstance(delta, dict):
        return prior_state

    new_state = dict(prior_state)

    # Python-managed
    new_state["last_run"]  = datetime.utcnow().isoformat()
    new_state["btc_price"] = prices.get("BTC") or prior_state.get("btc_price")
    new_state["profitable_wallets_discovered"] = profitable_wallets
    new_state["macro_snapshot"] = {
        "us_10y":                 macro_data.get("us_10y"),
        "us_30y":                 macro_data.get("us_30y"),
        "japan_10y":              macro_data.get("japan_10y"),
        "japan_30y":              macro_data.get("japan_30y"),
        "japan_curve_spread":     macro_data.get("japan_curve_spread"),
        "spx":                    macro_data.get("spx"),
        "btc_oi_usd_bn":          macro_data.get("btc_oi_usd_bn"),
        "btc_funding_rate_pct":   macro_data.get("btc_funding_rate_pct"),
        "us_curve_status":        macro_data.get("us_curve_status"),
        "japan_stress":           macro_data.get("japan_stress"),
        "usdjpy":                 macro_data.get("usdjpy"),
        "usdjpy_weekly_chg_pct":  macro_data.get("usdjpy_weekly_chg_pct"),
        "carry_regime":           macro_data.get("carry_regime"),
        "carry_architecture_alert": macro_data.get("carry_architecture_alert"),
        "usdjpy_history": _update_usdjpy_history(
            prior_state.get("macro_snapshot", {}).get("usdjpy_history", []),
            macro_data.get("usdjpy"),
        ),
    }

    # Claude-managed fields
    for field in (
        "macro_bias", "bias_short", "bias_long",
        "cycle_phase", "cycle_year", "cycle_thesis", "cycle_bias_impact",
        "btc_dominance", "altcoin_season_index", "fear_greed",
        "active_setups", "open_positions", "alerted",
        "whale_signals_today", "last_analysis",
    ):
        if field in delta:
            new_state[field] = delta[field]

    return sanitize_state(new_state)


def extract_state_delta(text):
    """Extract JSON from [STATE_DELTA]...[/STATE_DELTA] block."""
    start = text.find("[STATE_DELTA]")
    end   = text.find("[/STATE_DELTA]")
    if start == -1 or end == -1:
        return {}
    delta_text = text[start + 13:end]
    depth, json_start = 0, None
    for i, ch in enumerate(delta_text):
        if ch == "{":
            if depth == 0: json_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and json_start is not None:
                try:
                    return json.loads(delta_text[json_start:i + 1])
                except json.JSONDecodeError:
                    json_start = None
    return {}


def log_setup_snapshot(state: dict, run_date: str) -> None:
    """Append each active setup to setups_history.jsonl for monthly hit-rate analysis."""
    path = BASE_DIR / "setups_history.jsonl"
    btc_price = state.get("btc_price")
    for setup in state.get("active_setups", []):
        record = {
            "date":            run_date,
            "symbol":          setup.get("symbol"),
            "direction":       setup.get("direction"),
            "status":          setup.get("status"),
            "conviction":      setup.get("conviction"),
            "composite_score": setup.get("composite_score"),
            "entry_zone":      setup.get("entry_zone"),
            "stop_loss":       setup.get("stop_loss"),
            "target_1":        setup.get("target_1"),
            "target_2":        setup.get("target_2"),
            "r_r_ratio":       setup.get("r_r_ratio"),
            "timeframe":       setup.get("timeframe"),
            "btc_price_at_log": btc_price,
        }
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")


_FOMC_DATES = [
    # 2026
    datetime(2026, 1, 28), datetime(2026, 3, 18), datetime(2026, 4, 29),
    datetime(2026, 6, 10), datetime(2026, 7, 29), datetime(2026, 9, 16),
    datetime(2026, 10, 28), datetime(2026, 12, 9),
    # 2027
    datetime(2027, 1, 27), datetime(2027, 3, 17), datetime(2027, 4, 28),
    datetime(2027, 6, 9),  datetime(2027, 7, 28), datetime(2027, 9, 15),
    datetime(2027, 10, 27), datetime(2027, 12, 8),
]

def get_fomc_context() -> dict:
    now = datetime.utcnow()
    future = [d for d in _FOMC_DATES if d >= now]
    if not future:
        return {"days_to_fomc": None, "next_fomc": None, "pre_fomc_window": False}
    nxt = future[0]
    days = (nxt - now).days
    return {
        "days_to_fomc":    days,
        "next_fomc":       nxt.strftime("%Y-%m-%d"),
        "pre_fomc_window": days <= 3,
    }


def count_enter_setups(state):
    return sum(
        1 for s in state.get("active_setups", [])
        if isinstance(s, dict) and s.get("status") == "ENTER"
    )


def extract_email_body(text):
    start = text.find("[EMAIL]")
    end   = text.find("[/EMAIL]")
    if start != -1 and end != -1:
        return text[start + 7:end].strip()
    return text


def run():
    print(f"[{datetime.utcnow().isoformat()}] ═══ Crypto Market Intelligence Agent (Haiku 4.5) ═══")

    env = load_env()

    try:
        api_key = get_api_key(env)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    etherscan_key = env.get("ETHERSCAN_API_KEY", "")

    # ── Step 1: Load state + apply Telegram position updates ───────────────────────
    state = load_state()
    state, tg_log = apply_pending_updates(state)
    if tg_log:
        print(f"[{datetime.utcnow().isoformat()}] Telegram updates applied: {', '.join(tg_log)}")
        save_state(state)
    print(f"[{datetime.utcnow().isoformat()}] State loaded — "
          f"{len(state.get('active_setups', []))} setups, "
          f"{len(state.get('open_positions', []))} open positions")

    # ── Step 2: Fetch on-chain whale data ─────────────────────────────
    existing_profitable = state.get("profitable_wallets_discovered", [])
    whale_data = get_all_whale_data(
        etherscan_key=etherscan_key,
        existing_wallets=existing_profitable,
    )
    summary = whale_data.get("summary", {})
    print(f"[{datetime.utcnow().isoformat()}] Whale data fetched — "
          f"BTC moves:{summary.get('btc_large_moves', 0)} "
          f"ETH moves:{summary.get('eth_large_moves', 0)} "
          f"profitable wallets:{summary.get('profitable_wallets_tracked', 0)}")

    # ── Step 3: Build prompt for Claude ──────────────────────────────
    system_prompt = load_instructions()
    whale_slim    = slim_whale_data(whale_data)

    # Pre-compute authoritative analytics before building the prompt
    fomc = get_fomc_context()
    pre_computed = {
        "position_analytics": compute_position_analytics(
            state.get("open_positions", []),
            whale_slim.get("prices", {}),
        ),
        "setup_statuses": compute_setup_statuses(
            state.get("active_setups", []),
            whale_slim.get("prices", {}),
        ),
        "cycle_metrics":    whale_slim.get("cycle_metrics", {}),
        "market_globals":   whale_slim.get("market_globals", {}),
        "fomc":             fomc,
    }

    tg_section = (
        f"\n═══ POSITION UPDATES (received via Telegram before this run) ═══\n"
        + "\n".join(f"- {l}" for l in tg_log)
        if tg_log else ""
    )

    today_str = datetime.utcnow().strftime('%Y-%m-%d')

    # ── Build data-driven prefill ─────────────────────────────────────
    # MACRO REGIME and YEN CARRY are pre-filled from Python using the
    # freshly-fetched macro data. This makes it physically impossible for
    # Claude to skip those sections — they're already in the response before
    # Claude writes its first token. Claude only needs to fill analysis
    # sections (biases, cycle view, liquidity bullets, positions, setups).

    macro = whale_slim.get("macro", {})
    prices = whale_slim.get("prices", {})

    def _fv(v, suffix="", na="N/A"):
        return f"{v}{suffix}" if v is not None else na

    us_10y_raw  = macro.get("us_10y")
    us_30y_raw  = macro.get("us_30y")
    j_10y_raw   = macro.get("japan_10y")
    j_30y_raw   = macro.get("japan_30y")

    us_spread_v = (
        f"{(us_30y_raw - us_10y_raw):.2f}%"
        if us_10y_raw is not None and us_30y_raw is not None else "N/A"
    )
    j_spread_v  = _fv(macro.get("japan_curve_spread"), "%")

    us_10y_v    = _fv(us_10y_raw, "%")
    us_30y_v    = _fv(us_30y_raw, "%")
    j_10y_v     = _fv(j_10y_raw,  "%")
    j_30y_v     = _fv(j_30y_raw,  "%")
    us_status_v = macro.get("us_curve_status")   or "N/A"
    j_stress_v  = macro.get("japan_stress")      or "N/A"
    spx_v       = _fv(macro.get("spx"))
    oi_v        = _fv(macro.get("btc_oi_usd_bn"),        "B")
    fr_v        = _fv(macro.get("btc_funding_rate_pct"),  "%")
    lev_v       = macro.get("btc_leverage_signal") or "N/A"
    usdjpy_v    = _fv(macro.get("usdjpy"))
    usdjpy_chg  = _fv(macro.get("usdjpy_weekly_chg_pct"), "%/wk")
    carry_v     = macro.get("carry_regime") or "N/A"
    arch_alert  = macro.get("carry_architecture_alert", False)
    arch_line   = "\nArch  : ⚠️ lower-highs (arch alert)" if arch_alert else ""

    mg          = whale_slim.get("market_globals", {})
    cm          = whale_slim.get("cycle_metrics", {})

    btc_price_v = _fv(prices.get("BTC") or state.get("btc_price"))
    btc_dom_v   = _fv(mg.get("btc_dominance") or state.get("btc_dominance"))
    fg_v        = _fv(mg.get("fear_greed") or state.get("fear_greed"))

    # Cycle metrics for prefill
    cy_year     = cm.get("cycle_year") or state.get("cycle_year") or "?"
    cy_ma       = cm.get("btc_200w_ma")
    cy_ma_prem  = cm.get("btc_200w_ma_premium_pct")
    cy_mvrv     = cm.get("btc_mvrv_approx")
    cy_ma_v     = f"${cy_ma:,.0f}" if cy_ma is not None else "N/A"
    cy_prem_v   = f"({cy_ma_prem:+.1f}%)" if cy_ma_prem is not None else ""
    cy_mvrv_v   = f"{cy_mvrv}" if cy_mvrv is not None else "N/A"

    # FOMC for prefill
    fomc_days   = fomc.get("days_to_fomc")
    next_fomc   = fomc.get("next_fomc") or "N/A"
    pre_fomc_w  = fomc.get("pre_fomc_window", False)
    fomc_v      = f"{fomc_days}d to {next_fomc}" if fomc_days is not None else f"next {next_fomc}"
    fomc_flag   = " ⚠️ PRE-FOMC" if pre_fomc_w else ""

    # Prefill: MACRO REGIME and YEN CARRY are pre-filled from Python so
    # Claude cannot skip them. Claude continues from SHORT bias onwards.
    prefill = (
        f"[EMAIL]\n"
        f"CRYPTO DAILY BRIEF\n"
        f"{today_str} | "
    )

    # The macro card suffix is appended to whatever macro_bias Claude writes.
    # Because it's in the prefill it cannot be omitted.
    macro_card_suffix = (
        f"\n"
        f"BTC ${btc_price_v} | Dom {btc_dom_v}% | F&G {fg_v}\n"
        f"------------------------------\n"
        f"\n"
        f"MACRO REGIME\n"
        f"------------------------------\n"
        f"US 10Y: {us_10y_v}   30Y: {us_30y_v}\n"
        f"Curve : {us_spread_v} ({us_status_v})\n"
        f"JGB10Y: {j_10y_v}  30Y: {j_30y_v}\n"
        f"JGB   : {j_stress_v}\n"
        f"SPX   : {spx_v}\n"
        f"BTC OI: ${oi_v}  FR: {fr_v}\n"
        f"Lev   : {lev_v}\n"
        f"------------------------------\n"
        f"YEN CARRY\n"
        f"USDJPY: {usdjpy_v}  ({usdjpy_chg})\n"
        f"Regime: {carry_v}{arch_line}\n"
        f"------------------------------\n"
        f"FOMC  : {fomc_v}{fomc_flag}\n"
        f"Cycle : Y{cy_year}/4 | MA1000d: {cy_ma_v} {cy_prem_v} | MVRV≈{cy_mvrv_v}\n"
        f"------------------------------\n"
        f"SHORT bias:"
    )
    prefill = (prefill + macro_card_suffix).rstrip()

    user_prompt = f"""Today is {today_str}.

═══ OUTPUT FORMAT — READ THIS FIRST ═══
Your response MUST contain two blocks: [EMAIL]...[/EMAIL] then [STATE_DELTA]...[/STATE_DELTA].
The [EMAIL] block MUST contain ALL of these sections IN THIS EXACT ORDER:
  1.  Header line  (date | bias | BTC price | dom | F&G)
  2.  MACRO REGIME card  ← REQUIRED even if all values are N/A
  3.  YEN CARRY card     ← REQUIRED even if USDJPY is N/A
  4.  SHORT bias / LONG bias lines
  5.  CYCLE VIEW (BTC 4-yr halving cycle — 2 lines max)
  6.  LIQUIDITY ANALYSIS (3-4 bullets, 1 line each) ← REQUIRED
  7.  OPEN POSITIONS (each card MUST show TF: SHORT/MED/LONG_TERM)
  8.  SHORT-TERM SETUPS (days–2wk)  ← always present, "None." if empty
  9.  LONG-TERM SETUPS  (weeks–months+) ← always present, "None." if empty
  10. WAITING (monitor only)
  11. CHANGES TODAY
NEVER rename, merge, reorder, or skip any section.
If a value is unavailable write N/A — do NOT remove the section or its header.
No markdown: no **, no ##, no _underscores_. Plain text only.
Max ~35 chars per line (mobile).
Keep the email body tight — one idea per line, no padding sentences.

═══ REAL ON-CHAIN WHALE DATA (fetched this run) ═══
{json.dumps(whale_slim, separators=(',', ':'), default=str)}

═══ PRE-COMPUTED ANALYTICS (Python-verified — use these, do NOT recalculate) ═══
{json.dumps(pre_computed, separators=(',', ':'), default=str)}

═══ CURRENT STATE (from last run) ═══
{json.dumps(state, separators=(',', ':'), default=str)}
{tg_section}
Analysis instructions:
- Use the real on-chain data above for Steps 3 (whale signals) and 4 (prices).
- large_transfers shows actual large moves today — classify as bullish/bearish via direction field.
- profitable_wallets_discovered are real wallets with >20% avg profit — treat as high-weight signals.
- Execute all steps internally (macro, whale scoring, TA, composite scoring, setup updates).
- No positions are open unless listed in current state open_positions.
- ALL open positions must appear in the email with P&L, stop status, and a specific action.
- Positions with status=OPEN and conviction=UNKNOWN were opened outside analysis — run full whale+TA on them and adopt them into active_setups with real levels.
- SPX / SP500 / US500 / S&P 500 is a PORTFOLIO AGENT asset. In the crypto email it appears only as a number in the pre-filled MACRO REGIME card. Do NOT write SPX setups, SPX position cards, or any SPX analysis section. If SPX appears in open_positions, flag it as a routing error in CHANGES TODAY only.
- All crypto perpetual positions are labeled as "perp" (not "spot") unless market_type is explicitly "spot" AND the user confirmed a spot purchase. When in doubt, use "perp".
- pre_computed.position_analytics contains Python-verified P&L and flags for each position. Use these values exactly — do NOT recalculate P&L. Keys are "SYMBOL_DIRECTION" (e.g. "BTC_LONG").
- pre_computed.setup_statuses contains Python-verified ENTER/APPROACHING/WAITING/INVALIDATED for each setup. Use these, do NOT re-derive from price.
- pre_computed.cycle_metrics contains btc_mvrv_approx: MVRV>3.0 = historically expensive (cycle top risk), MVRV<1.0 = historically cheap (bottom zone). Use this for cycle analysis.
- pre_computed.market_globals contains fresh fear_greed and btc_dominance — use these values, not stale state values.
- pre_computed.fomc: if pre_fomc_window=true, a Fed decision is within 3 days — suppress new SHORT_TERM setups and flag elevated volatility risk. If days_to_fomc < 7, note in CHANGES TODAY as catalyst risk.
- Flag any position with P&L < -10% or no stop_loss as high risk. Flag P&L < -15% as DANGER.
- macro.japan_stress HIGH/CRITICAL = liquidity tightening risk, increase bearish weight on risk assets.
- macro.us_curve_status INVERTED = recession signal, favour defensive bias_long = BEARISH.
- macro.btc_leverage_signal EXTREME_LONGS = crowded, reversion risk; EXTREME_SHORTS = squeeze risk.
- bias_short covers days-to-weeks setups (timeframe=SHORT_TERM).
- bias_long covers months+ setups (timeframe=MEDIUM_TERM or LONG_TERM).
- A setup whose direction conflicts with its matching bias gets conviction downgraded one level and flagged.
- macro.carry_regime: CARRY_STABLE=no adjustment | CARRY_STRESS=add -0.1 to all risk longs | CARRY_UNWIND=bias_short BEARISH override, add -0.2 | CARRY_COLLAPSE=both biases BEARISH, -0.35, flag SYSTEMIC.
- macro.carry_architecture_alert=true means USDJPY is below stable-carry range: add -0.1 to bias_long and note structural concern in email.
- macro.japan_curve_spread narrowing run-over-run = BOJ losing long-end control; amplifies japan_stress signal.
- Track macro.usdjpy across runs in macro_snapshot.usdjpy_history (list of last 4 weekly closes); flag if making lower-highs.
- If carry_regime is CARRY_UNWIND or COLLAPSE and user holds a long position → always flag ⚠️ CARRY RISK regardless of P&L.
- BTC CYCLE: today is in the 2024-04 halving cycle. Y1=2024, Y2=2025, Y3=2026 (now, bear/bottom year), Y4=2027 (pre-halving accumulation). Historical Y3 drawdown is 70–85% from cycle peak — multi-month longs in Y3 have been wrong every prior cycle. Always state `cycle_phase`, `cycle_year`, `cycle_thesis`, `cycle_bias_impact` in state and in the CYCLE VIEW email section.
- TIMEFRAME-RESPECTING ACTION RULE: every open position has a `tf` field (SHORT_TERM, MEDIUM_TERM, LONG_TERM). Evaluate each position ONLY against the bias of its own timeframe:
    • SHORT_TERM position → vs bias_short
    • MEDIUM_TERM / LONG_TERM position → vs bias_long + cycle_phase
  NEVER recommend closing a MEDIUM/LONG_TERM position because of opposing SHORT_TERM whale flow or TA. Note such conflicts as "ST noise — hold thesis", but the action must respect the long-term thesis unless stop breached or cycle/bias_long has actually flipped.
- SETUP TIMEFRAMES: every setup MUST have a `timeframe` field. Place SHORT_TERM setups under "SHORT-TERM SETUPS" section, MEDIUM_TERM/LONG_TERM under "LONG-TERM SETUPS". If a section has no setups write "None.".

═══ EMAIL FORMAT — MANDATORY RULES ═══
VIOLATION OF ANY RULE BELOW = WRONG OUTPUT.

1. SECTIONS ARE FIXED. Copy the exact section names below, in this exact order.
   Never rename, merge, skip, or add sections.

2. NO MARKDOWN. No **, no *, no ##, no _underscores_. Plain text only.
   Section headers are written as-is (e.g. "MACRO REGIME", not "**MACRO REGIME**").

3. EVERY SECTION IS REQUIRED IN EVERY EMAIL.
   If a value is unavailable write N/A — never omit the section.

4. MACRO REGIME card: fill every field with real values from macro data.
   If a fetch failed, write "N/A (fetch failed)". Never skip the card.

5. LIQUIDITY ANALYSIS: write 3-4 bullets. Keep each to ONE line.
   Format: "INDICATOR VALUE → brief impact on crypto"
   Example: "US10Y 4.52% → high opp cost, mild BTC headwind"
   If macro data is N/A, use prior state + BTC context.
   Body bullets must be self-contained — no multi-line explanations.

6. OPEN POSITIONS: every entry in open_positions MUST appear as a card.
   No exceptions. If none: write exactly "None confirmed."
   The OPEN POSITIONS section contains ONLY confirmed open positions.
   Setup cards (🔴 🟣) NEVER appear inside OPEN POSITIONS.

   SECTION BOUNDARY RULE — CRITICAL:
   If a symbol is in open_positions, it MUST appear in OPEN POSITIONS
   and MUST NOT appear again in SHORT-TERM SETUPS or LONG-TERM SETUPS.
   SHORT-TERM SETUPS = SHORT_TERM setups whose symbol is NOT in open_positions.
   LONG-TERM SETUPS  = MEDIUM/LONG_TERM setups whose symbol is NOT in open_positions.
   A position already tracked in OPEN POSITIONS needs no duplicate setup card.
   Violating this rule = duplicate content = wrong output.

7. Max ~35 characters per line (mobile screen). No wide lines.


[NOTE: The email has already been started for you.
 The MACRO REGIME and YEN CARRY cards are pre-filled.
 Your response will be appended after "SHORT bias: "
 Continue from there. Write ONLY what comes after
 the prefill — do not repeat sections already written.]

Your output must continue as:
<BIAS>  (weeks)
LONG  bias: <bias_long>   (months+)
------------------------------

CYCLE VIEW
------------------------------
Phase  : <cycle_phase> (Y<cycle_year>/4)
Halving: 2024-04 → next ~2028-04
Thesis : <cycle_thesis>
Impact : <cycle_bias_impact>
------------------------------

LIQUIDITY ANALYSIS
------------------------------
<REQUIRED — 4 to 6 bullets. Each bullet MUST:
 (a) name the signal with its current value,
 (b) state what it means right now (not generic),
 (c) state the direct crypto impact.
 Format: "• Signal X%/level: what it means → crypto impact"
 Cover in order (skip only if value is N/A):
 1. US 10Y yield — level vs historical norm,
    rising/falling trend, what it means for
    risk-asset valuations right now
 2. US 30Y vs 5% threshold — funding cost
    pressure on leveraged players, credit stress
 3. US yield curve (normal/flat/inverted) —
    recession signal or growth signal
 4. JGB 30Y / Japan stress — BOJ policy stress,
    yen carry trade fragility, global liquidity
 5. Yen carry regime — USDJPY trend, carry
    adjustment applied to composites, risk
 6. BTC OI + funding rate — leverage positioning,
    forced-liquidation risk or squeeze risk
 7. Combined verdict: how 1-6 produced
    bias_short and bias_long values above>
• <bullet 1 with value, meaning, crypto impact>
• <bullet 2 with value, meaning, crypto impact>
• <bullet 3 with value, meaning, crypto impact>
• <bullet 4 with value, meaning, crypto impact>
• <bullet 5 with value, meaning, crypto impact>
• <bullet 6 or 7 if needed>
------------------------------

OPEN POSITIONS
------------------------------
<If open_positions is empty: write "None confirmed.">
<One card per position. Use exactly this layout.
 NEVER put setup entries (🔴 🟣) in this section.>
<SYM> <DIRECTION>
  Type  : <perp|spot>
  TF    : <SHORT_TERM|MEDIUM_TERM|LONG_TERM>
  Entry : $<entry_price>
  Now   : $<current>  P&L: <pnl>%
  Stop  : $<stop_loss or N/A>
  Bias  : <Aligned|CONFLICT>
  Action: <specific action>
------------------------------
<P&L < -10% or stop missing — open card with ⚠️ SYM DIRECTION>
<P&L < -15% or stop breached — open card with 🚨 SYM DIRECTION>

SHORT-TERM SETUPS (days–2wk)
------------------------------
<Setups with timeframe=SHORT_TERM only.
 If none: write "None.">
🔴 <SYM> <DIR> — <CONVICTION>
  Status: <ENTER|APPROACHING|WAITING>
  Zone  : $<low>–$<high>
  Stop  : $<stop>
  T1    : $<t1>  T2: $<t2>
  R/R   : <ratio>x | Whale: <whale_signal>
------------------------------

LONG-TERM SETUPS (weeks–months+)
------------------------------
<Setups with timeframe=MEDIUM_TERM or LONG_TERM.
 If none: write "None.">
🟣 <SYM> <DIR> — <CONVICTION>
  Status: <ENTER|APPROACHING|WAITING>
  Zone  : $<low>–$<high>
  Stop  : $<stop>
  T1    : $<t1>  T2: $<t2>
  R/R   : <ratio>x | Cycle: <aligned|against>
------------------------------

WAITING (monitor only)
------------------------------
<One line each: SYM DIR — reason in <7 words>
 If none: write "None.">

CHANGES TODAY
------------------------------
<One bullet per change. Tags: NEW / ENTER /
 INVALIDATED / REVISED / ADOPTED / COMPLETED>
[/EMAIL]

[STATE_DELTA]
{{JSON with ONLY these fields — Python manages the rest:
  macro_bias, bias_short, bias_long,
  cycle_phase, cycle_year, cycle_thesis, cycle_bias_impact,
  btc_dominance, altcoin_season_index, fear_greed,
  active_setups, open_positions, alerted,
  whale_signals_today, last_analysis}}
[/STATE_DELTA]
"""

    # ── Step 4: Call Claude Haiku ───────────────────────────────────
    client = anthropic.Anthropic(api_key=api_key)
    print(f"[{datetime.utcnow().isoformat()}] Calling Claude Haiku 4.5...")

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=6000,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[
            {"role": "user",      "content": user_prompt},
            {"role": "assistant", "content": prefill},
        ],
    )

    # Prepend the prefilled text — the API does not echo it back in the response.
    response          = prefill + message.content[0].text
    tokens_in         = message.usage.input_tokens
    tokens_cache_read  = getattr(message.usage, "cache_read_input_tokens", 0)
    tokens_cache_write = getattr(message.usage, "cache_creation_input_tokens", 0)
    tokens_out        = message.usage.output_tokens
    cost_usd = (
        (tokens_in         * 0.80) +
        (tokens_cache_read  * 0.08) +
        (tokens_cache_write * 1.00) +
        (tokens_out        * 4.00)
    ) / 1_000_000

    print(f"[{datetime.utcnow().isoformat()}] Response received — "
          f"in:{tokens_in} cache_read:{tokens_cache_read} "
          f"cache_write:{tokens_cache_write} out:{tokens_out} cost:${cost_usd:.4f}")

    # ── Step 5: Extract state delta and merge ──────────────────────────────
    date_str = datetime.utcnow().strftime("%Y-%m-%d")

    delta = extract_state_delta(response)
    if delta:
        updated_state = merge_state_delta(
            prior_state=state,
            delta=delta,
            macro_data=whale_data.get("macro", {}),
            prices=whale_data.get("prices", {}),
            profitable_wallets=whale_data.get("profitable_wallets_discovered", []),
        )
        save_state(updated_state)
        log_setup_snapshot(updated_state, date_str)
        print(f"[{datetime.utcnow().isoformat()}] state.json updated via delta merge")
    else:
        # Fallback: try old-style full STATE_JSON extraction
        sj_start = response.find("[STATE_JSON]")
        sj_end   = response.find("[/STATE_JSON]")
        state_text = response[sj_start + 12:sj_end] if sj_start != -1 and sj_end != -1 else response
        fallback = extract_state_from_response(state_text)
        if fallback:
            fallback["profitable_wallets_discovered"] = whale_data.get("profitable_wallets_discovered", [])
            updated_state = sanitize_state(fallback)
            save_state(updated_state)
            log_setup_snapshot(updated_state, date_str)
            print(f"[{datetime.utcnow().isoformat()}] state.json updated via fallback full-JSON")
        else:
            print(f"[{datetime.utcnow().isoformat()}] WARNING: Could not extract state — state unchanged")
            updated_state = state

    # ── Step 6: Save full response to file ────────────────────────────────
    report_path = BASE_DIR / f"daily_report_{date_str}.txt"
    with open(report_path, "w") as f:
        f.write(response)
    print(f"[{datetime.utcnow().isoformat()}] Report saved: {report_path}")

    # ── Step 7: Send email ────────────────────────────────────────────
    email_body  = extract_email_body(response)
    macro_bias  = extract_macro_bias(email_body)
    setup_count = len(updated_state.get("active_setups", []))
    enter_count = count_enter_setups(updated_state)
    subject     = build_subject(macro_bias, setup_count, enter_count, date_str)

    email_ok = send_report(
        subject=subject,
        body=email_body,
        is_alert=enter_count > 0,
        attachment=response,
        attachment_filename=f"crypto_full_report_{date_str}.txt",
    )

    # ── Step 8: Update report.log ─────────────────────────────────────────
    log_line = (f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC | "
                f"{macro_bias} | {setup_count} setups | {enter_count} ENTER | "
                f"email:{'OK' if email_ok else 'FAIL'} | "
                f"cost:${cost_usd:.4f} | Haiku 4.5\n")
    with open(BASE_DIR / "report.log", "a") as f:
        f.write(log_line)

    print(f"[{datetime.utcnow().isoformat()}] Done. {log_line.strip()}")

    print("\n" + "=" * 80)
    print(response)
    print("=" * 80)

    return 0


if __name__ == "__main__":
    sys.exit(run())
