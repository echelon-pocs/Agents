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


def run():
    print(f"[{datetime.utcnow().isoformat()}] ═══ Crypto Market Intelligence Agent (Haiku 4.5) ═══")

    env = load_env()

    try:
        api_key = get_api_key(env)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    etherscan_key = env.get("ETHERSCAN_API_KEY", "")

    # ── Step 1: Load state ────────────────────────────────────────────────────
    state = load_state()
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

    user_prompt = f"""Today is {datetime.utcnow().strftime('%Y-%m-%d')}.

═══ REAL ON-CHAIN WHALE DATA (fetched this run) ═══
{json.dumps(whale_data, indent=2, default=str)}

═══ CURRENT STATE (from last run) ═══
{json.dumps(state, indent=2, default=str)}

Instructions:
- Use the real on-chain data above for Steps 3 (whale signals) and 4 (prices).
- The whale_data.large_transfers shows actual large moves today — classify as bullish/bearish based on direction field.
- The whale_data.profitable_wallets_discovered are real wallets with verified >20% avg profit — treat them as high-weight signals.
- Execute Steps 2 (macro analysis), 5 (TA), 6 (opportunity scoring), 7 (update setups), 8 (alerts) fully.
- Output format: first the complete daily report (Steps 9 format), then a JSON block for the updated state.json.
- Mark open_positions P&L accurately. No positions are open unless listed in current state open_positions.
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
    updated_state = extract_state_from_response(response)
    if updated_state:
        # Preserve profitable wallets list from whale tracker
        updated_state["profitable_wallets_discovered"] = \
            whale_data["profitable_wallets_discovered"]
        save_state(updated_state)
        print(f"[{datetime.utcnow().isoformat()}] state.json updated")
    else:
        print(f"[{datetime.utcnow().isoformat()}] WARNING: Could not extract state JSON from response")
        updated_state = state

    # ── Step 6: Save report to file ───────────────────────────────────────────
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    report_path = BASE_DIR / f"daily_report_{date_str}.txt"
    with open(report_path, "w") as f:
        f.write(response)
    print(f"[{datetime.utcnow().isoformat()}] Report saved: {report_path}")

    # ── Step 7: Send email ────────────────────────────────────────────────────
    macro_bias   = extract_macro_bias(response)
    setup_count  = len(updated_state.get("active_setups", []))
    enter_count  = count_enter_setups(updated_state)
    subject      = build_subject(macro_bias, setup_count, enter_count, date_str)

    email_ok = send_report(subject=subject, body=response, is_alert=enter_count > 0)

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
