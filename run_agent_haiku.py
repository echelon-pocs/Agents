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
        chain: [_slim_transfer(tx) for tx in txs]
        for chain, txs in data.get("large_transfers", {}).items()
    }
    known = {
        chain: list(wallets.keys())
        for chain, wallets in data.get("known_wallets", {}).items()
    }
    profitable = []
    for w in data.get("profitable_wallets_discovered", [])[:8]:
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
        "prices":             data.get("prices", {}),
        "transfers":          transfers,
        "known_wallet_labels": known,
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

    btc_price_v = _fv(prices.get("BTC") or state.get("btc_price"))
    btc_dom_v   = _fv(state.get("btc_dominance"))
    fg_v        = _fv(state.get("fear_greed"))

    # Prefill ends just before SHORT bias so Claude writes the bias values
    # and all remaining analysis sections.
    prefill = (
        f"[EMAIL]\n"
        f"CRYPTO DAILY BRIEF\n"
        f"{today_str} | "
        # Claude fills in MACRO_BIAS after the pipe, then BTC line, then dashes
        # — but MACRO REGIME and YEN CARRY below are already hard-coded.
        # We stop the prefill after the first dashes so Claude writes the
        # macro bias and BTC header, then encounters the pre-filled cards.
    )

    # Full pre-filled block injected into the assistant turn AFTER Claude
    # writes the header. We use a two-message trick: start with an
    # incomplete header and let Claude finish it, but force everything after
    # the first separator by including the full card text in the prefill.
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
        f"SHORT bias: "
    )
    prefill = prefill + macro_card_suffix

    user_prompt = f"""Today is {today_str}.

═══ OUTPUT FORMAT — READ THIS FIRST ═══
Your response MUST contain two blocks: [EMAIL]...[/EMAIL] then [STATE_JSON]...[/STATE_JSON].
The [EMAIL] block MUST contain ALL of these sections IN THIS EXACT ORDER:
  1.  Header line  (date | bias | BTC price | dom | F&G)
  2.  MACRO REGIME card  ← REQUIRED even if all values are N/A
  3.  YEN CARRY card     ← REQUIRED even if USDJPY is N/A
  4.  SHORT bias / LONG bias lines
  5.  CYCLE VIEW (BTC 4-yr halving cycle position) ← REQUIRED
  6.  LIQUIDITY ANALYSIS (4-6 bullets) ← REQUIRED, never skip
  7.  OPEN POSITIONS (each card MUST show TF: SHORT/MED/LONG_TERM)
  8.  SHORT-TERM SETUPS (days–2wk)  ← always present, "None." if empty
  9.  LONG-TERM SETUPS  (weeks–months+) ← always present, "None." if empty
  10. WAITING (monitor only)
  11. CHANGES TODAY
NEVER rename, merge, reorder, or skip any section.
If a value is unavailable write N/A — do NOT remove the section or its header.
No markdown: no **, no ##, no _underscores_. Plain text only.
Max ~35 chars per line (mobile).

═══ REAL ON-CHAIN WHALE DATA (fetched this run) ═══
{json.dumps(whale_slim, separators=(',', ':'), default=str)}

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

5. LIQUIDITY ANALYSIS: always write 4-6 bullet points explaining what each
   macro signal means for crypto. This section is NOT optional.
   If macro data is all N/A, write what is known from prior state and BTC price action.

6. OPEN POSITIONS: every entry in open_positions MUST appear as a card.
   No exceptions. If none: write exactly "None confirmed."

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
<REQUIRED — write exactly 4 to 6 bullet points.
 Each bullet must name the signal and state its
 crypto impact. Cover in this order (skip only
 if value is truly N/A):
 1. US yield curve level/status
 2. US 30Y vs 5% threshold
 3. JGB 30Y stress level
 4. Yen carry regime + adjustment applied
 5. BTC OI trend and leverage signal
 6. How 1-5 combined to produce bias_short/long>
• <bullet 1>
• <bullet 2>
• <bullet 3>
• <bullet 4>
• <bullet 5>
• <bullet 6 if needed>
------------------------------

OPEN POSITIONS
------------------------------
<If open_positions is empty: write "None confirmed.">
<One card per position — NO EXCEPTIONS.
 TF MUST appear and drives the bias check:>
<SYM> <DIRECTION> (<market_type>)
  TF    : <SHORT_TERM|MEDIUM_TERM|LONG_TERM>
  Entry : $<entry_price>
  Now   : $<current>  P&L: <pnl>%
  Stop  : $<stop_loss>
  Bias  : <Aligned with bias_<tf>|CONFLICT>
  Action: <action respects TF — see rules>
------------------------------
<P&L < -10% or stop missing — prefix with ⚠️>
<P&L < -15% or stop breached — prefix with 🚨>

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

[STATE_JSON]
{{updated state.json as valid JSON}}
[/STATE_JSON]
"""

    # ── Step 4: Call Claude Haiku ───────────────────────────────────
    client = anthropic.Anthropic(api_key=api_key)
    print(f"[{datetime.utcnow().isoformat()}] Calling Claude Haiku 4.5...")

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=6000,
        system=system_prompt,
        messages=[
            {"role": "user",      "content": user_prompt},
            {"role": "assistant", "content": prefill},
        ],
    )

    # Prepend the prefilled text — the API does not echo it back in the response.
    response   = prefill + message.content[0].text
    tokens_in  = message.usage.input_tokens
    tokens_out = message.usage.output_tokens
    cost_usd   = (tokens_in * 0.80 + tokens_out * 4.00) / 1_000_000

    print(f"[{datetime.utcnow().isoformat()}] Response received — "
          f"in:{tokens_in} out:{tokens_out} cost:${cost_usd:.4f}")

    # ── Step 5: Extract and save updated state ────────────────────────────
    state_text = response
    sj_start = response.find("[STATE_JSON]")
    sj_end   = response.find("[/STATE_JSON]")
    if sj_start != -1 and sj_end != -1:
        state_text = response[sj_start + 12:sj_end]

    updated_state = extract_state_from_response(state_text)
    if updated_state:
        updated_state["profitable_wallets_discovered"] = \
            whale_data.get("profitable_wallets_discovered", [])
        save_state(updated_state)
        updated_state = sanitize_state(updated_state)
        print(f"[{datetime.utcnow().isoformat()}] state.json updated")
    else:
        print(f"[{datetime.utcnow().isoformat()}] WARNING: Could not extract state JSON")
        updated_state = state

    # ── Step 6: Save full response to file ────────────────────────────────
    date_str    = datetime.utcnow().strftime("%Y-%m-%d")
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
