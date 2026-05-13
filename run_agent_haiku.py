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


def _slim_transfer(tx: dict) -> dict:
    """Keep only what Claude needs for signal scoring — drop hash, raw addresses, native amounts."""
    return {k: v for k, v in tx.items()
            if k in ("chain", "value_usd", "direction", "amount_ondo",
                     "amount_xrp", "wallet_label", "slot", "err")}


def slim_whale_data(data: dict) -> dict:
    """
    Strip fields Claude can't use for analysis:
    - tx hashes and raw from/to addresses (direction already encodes exchange flows)
    - known_wallets addresses (just send label list per chain)
    - profitable_wallets tokens_bought detail (send summary string instead)
    - native token amounts where value_usd is present
    Result: ~50-60% fewer tokens in the JSON dump.
    """
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
        bought_summary = ", ".join(
            f"{t['symbol']} +{t['profit_pct']}%"
            for t in w.get("tokens_bought", [])
        )
        profitable.append({
            "addr":    w["address"][:10] + "…",
            "profit":  f"+{w['avg_profit_pct']}%",
            "trades":  w["trade_count"],
            "bought":  bought_summary or "—",
        })

    signals = [
        {k: v for k, v in s.items() if k != "wallet"}
        for s in data.get("profitable_wallet_signals", [])
    ]

    return {
        "prices":    data.get("prices", {}),
        "transfers": transfers,
        "known_wallet_labels": known,
        "profitable_wallets": profitable,
        "profitable_signals": signals,
        "summary":   data.get("summary", {}),
    }


def load_instructions() -> str:
    with open(BASE_DIR / "CLAUDE.md") as f:
        return f.read()


def load_state() -> dict:
    p = BASE_DIR / "state.json"
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(BASE_DIR / "state.json", "w") as f:
        json.dump(state, f, indent=2)


def apply_pending_updates(state: dict) -> tuple[dict, list[str]]:
    """
    Read pending_updates.json (written by telegram_bot.py), apply position
    changes to state, clear the file, and return (updated_state, log_lines).
    """
    pending_path = BASE_DIR / "pending_updates.json"
    if not pending_path.exists():
        return state, []

    try:
        updates = json.loads(pending_path.read_text())
    except Exception:
        return state, []

    if not updates:
        return state, []

    log = []
    positions = {p["symbol"]: p for p in state.get("open_positions", [])}
    setups    = {s["symbol"]: s for s in state.get("active_setups", [])}

    for u in updates:
        action = u.get("action")
        symbol = u.get("symbol", "")

        if action == "ENTER":
            price    = u.get("price", 0)
            size_usd = u.get("size_usd")
            # Find matching setup for direction/targets, fallback to LONG
            setup = setups.get(symbol, {})
            positions[symbol] = {
                "symbol":      symbol,
                "direction":   setup.get("direction", "LONG"),
                "entry_price": price,
                "entry_date":  u.get("timestamp", "")[:10],
                "stop_loss":   setup.get("stop_loss"),
                "target_1":    setup.get("target_1"),
                "target_2":    setup.get("target_2"),
                "size_usd":    size_usd,
                "pnl_pct":     None,
                "notes":       "User confirmed via Telegram.",
            }
            log.append(f"ENTERED {symbol} @ ${price:,}")

        elif action == "CLOSE":
            if symbol in positions:
                if u.get("partial"):
                    positions[symbol]["notes"] = (
                        positions[symbol].get("notes", "") + " | Partial close flagged."
                    )
                    log.append(f"PARTIAL CLOSE flagged: {symbol}")
                else:
                    del positions[symbol]
                    log.append(f"CLOSED {symbol}")
            else:
                log.append(f"CLOSE {symbol}: not in open positions (ignored)")

        elif action == "NOTE":
            if symbol in positions:
                positions[symbol]["notes"] = (
                    positions[symbol].get("notes", "") + f" | {u['note']}"
                )
                log.append(f"NOTE added to {symbol}: {u['note']}")

    state["open_positions"] = list(positions.values())

    # Clear the queue
    pending_path.write_text("[]")

    return state, log


def load_env() -> dict:
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


def get_api_key(env: dict) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError(
            "ANTHROPIC_API_KEY not found.\n"
            "Add it to .env:  ANTHROPIC_API_KEY=sk-ant-..."
        )
    return key


def extract_state_from_response(text: str) -> dict:
    """Pull the first valid JSON object out of Claude's response."""
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


def extract_macro_bias(text: str) -> str:
    for bias in ["BIFURCATED", "BULLISH", "BEARISH", "NEUTRAL"]:
        if bias in text:
            return bias
    return "NEUTRAL"


def count_enter_setups(state: dict) -> int:
    return sum(
        1 for s in state.get("active_setups", [])
        if s.get("status") == "ENTER"
    )


def extract_email_body(text: str) -> str:
    """Extract only the [EMAIL]...[/EMAIL] section from Claude's response."""
    start = text.find("[EMAIL]")
    end   = text.find("[/EMAIL]")
    if start != -1 and end != -1:
        return text[start + 7:end].strip()
    return text  # fallback: send full response if markers missing


def run():
    print(f"[{datetime.utcnow().isoformat()}] ═══ Crypto Market Intelligence Agent (Haiku 4.5) ═══")

    env = load_env()

    try:
        api_key = get_api_key(env)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    etherscan_key = env.get("ETHERSCAN_API_KEY", "")

    # ── Step 1: Load state + apply Telegram position updates ─────────────────
    state = load_state()
    state, tg_log = apply_pending_updates(state)
    if tg_log:
        print(f"[{datetime.utcnow().isoformat()}] Telegram updates applied: {', '.join(tg_log)}")
        save_state(state)
    print(f"[{datetime.utcnow().isoformat()}] State loaded — "
          f"{len(state.get('active_setups', []))} setups, "
          f"{len(state.get('open_positions', []))} open positions")

    # ── Step 2: Fetch on-chain whale data ─────────────────────────────────────
    existing_profitable = state.get("profitable_wallets_discovered", [])
    whale_data = get_all_whale_data(
        etherscan_key=etherscan_key,
        existing_wallets=existing_profitable,
    )
    print(f"[{datetime.utcnow().isoformat()}] Whale data fetched — "
          f"BTC moves:{whale_data['summary']['btc_large_moves']} "
          f"ETH moves:{whale_data['summary']['eth_large_moves']} "
          f"profitable wallets:{whale_data['summary']['profitable_wallets_tracked']}")

    # ── Step 3: Build prompt for Claude ──────────────────────────────────────
    system_prompt = load_instructions()

    whale_slim = slim_whale_data(whale_data)

    tg_section = (
        f"\n═══ POSITION UPDATES (received via Telegram before this run) ═══\n"
        + "\n".join(f"- {l}" for l in tg_log)
        if tg_log else ""
    )

    user_prompt = f"""Today is {datetime.utcnow().strftime('%Y-%m-%d')}.

═══ REAL ON-CHAIN WHALE DATA (fetched this run) ═══
{json.dumps(whale_slim, separators=(',', ':'), default=str)}

═══ CURRENT STATE (from last run) ═══
{json.dumps(state, separators=(',', ':'), default=str)}
{tg_section}
Instructions:
- Use the real on-chain data above for Steps 3 (whale signals) and 4 (prices).
- large_transfers shows actual large moves today — classify as bullish/bearish via direction field.
- profitable_wallets_discovered are real wallets with >20% avg profit — treat as high-weight signals.
- Execute all steps internally (macro, whale scoring, TA, composite scoring, setup updates).
- No positions are open unless listed in current state open_positions.

Output EXACTLY this structure — nothing else:

[EMAIL]
═══════════════════════════════════════════════════════════
CRYPTO DAILY BRIEF — {{DATE}} | {{MACRO_BIAS}}
═══════════════════════════════════════════════════════════

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 1 — STATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Last run : {{last_run}}
Positions: {{N open confirmed}} | Setups: {{N active}}
Alerted  : {{alerted symbols or none}}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 2 — MACRO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BTC       : ${{price}} | {{trend}} | {{% from 200d MA}}
BTC Dom   : {{%}} — {{above/below 60% interpretation}}
Alt Season: {{index}}/100 — {{interpretation}}
Fear&Greed: {{index}} — {{label}}
DXY/Gold  : {{one line}}
Macro Bias: {{BULLISH|BEARISH|NEUTRAL|BIFURCATED}}

{{2-3 sentences: what is driving the market, key risk or catalyst this week.}}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OPEN POSITIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[If no open positions, write: None confirmed.]
[If open positions exist, one row per position:]
SYM  DIR    ENTRY     NOW       P&L%   STOP      ACTION
---  -----  --------  --------  -----  --------  --------------------------
ETH  SHORT  $2,650    $2,520    +4.9%  $2,820    Trail stop to $2,600

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ACTIONABLE SETUPS  (ENTER and APPROACHING only)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SYM   DIR    STATUS    ENTRY ZONE       STOP      T1        T2        R/R  CONV    WHALE
----  -----  --------  ---------------  --------  --------  --------  ---  ------  ----------
[One row per ENTER or APPROACHING setup. Skip WAITING setups.]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WAITING (monitor only)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[Comma-separated list: SYM DIR — reason in 5 words]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHANGES TODAY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[Bullet list: NEW / ENTER / INVALIDATED / REVISED setups only. One line each.]
═══════════════════════════════════════════════════════════
[/EMAIL]

[STATE_JSON]
{{updated state.json as valid JSON}}
[/STATE_JSON]
"""

    # ── Step 4: Call Claude Haiku ─────────────────────────────────────────────
    client = anthropic.Anthropic(api_key=api_key)
    print(f"[{datetime.utcnow().isoformat()}] Calling Claude Haiku 4.5...")

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=6000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response = message.content[0].text
    tokens_in  = message.usage.input_tokens
    tokens_out = message.usage.output_tokens
    cost_usd   = (tokens_in * 0.80 + tokens_out * 4.00) / 1_000_000

    print(f"[{datetime.utcnow().isoformat()}] Response received — "
          f"in:{tokens_in} out:{tokens_out} cost:${cost_usd:.4f}")

    # ── Step 5: Extract and save updated state ────────────────────────────────
    # Extract state JSON — try [STATE_JSON] marker first, fall back to bare JSON
    state_text = response
    sj_start = response.find("[STATE_JSON]")
    sj_end   = response.find("[/STATE_JSON]")
    if sj_start != -1 and sj_end != -1:
        state_text = response[sj_start + 12:sj_end]

    updated_state = extract_state_from_response(state_text)
    if updated_state:
        updated_state["profitable_wallets_discovered"] = \
            whale_data["profitable_wallets_discovered"]
        save_state(updated_state)
        print(f"[{datetime.utcnow().isoformat()}] state.json updated")
    else:
        print(f"[{datetime.utcnow().isoformat()}] WARNING: Could not extract state JSON")
        updated_state = state

    # ── Step 6: Save full response to file ────────────────────────────────────
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    report_path = BASE_DIR / f"daily_report_{date_str}.txt"
    with open(report_path, "w") as f:
        f.write(response)
    print(f"[{datetime.utcnow().isoformat()}] Report saved: {report_path}")

    # ── Step 7: Send concise email ────────────────────────────────────────────
    email_body   = extract_email_body(response)
    macro_bias   = extract_macro_bias(email_body)
    setup_count  = len(updated_state.get("active_setups", []))
    enter_count  = count_enter_setups(updated_state)
    subject      = build_subject(macro_bias, setup_count, enter_count, date_str)

    email_ok = send_report(
        subject=subject,
        body=email_body,
        is_alert=enter_count > 0,
        attachment=response,
        attachment_filename=f"crypto_full_report_{date_str}.txt",
    )

    # ── Step 8: Update report.log ─────────────────────────────────────────────
    log_line = (f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC | "
                f"{macro_bias} | {setup_count} setups | {enter_count} ENTER | "
                f"email:{'OK' if email_ok else 'FAIL'} | "
                f"cost:${cost_usd:.4f} | Haiku 4.5\n")
    with open(BASE_DIR / "report.log", "a") as f:
        f.write(log_line)

    print(f"[{datetime.utcnow().isoformat()}] Done. {log_line.strip()}")

    # Print report to stdout for terminal review
    print("\n" + "=" * 80)
    print(response)
    print("=" * 80)

    return 0


if __name__ == "__main__":
    sys.exit(run())
