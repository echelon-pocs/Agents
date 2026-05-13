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
