#!/usr/bin/env python3
"""
Portfolio Intelligence Agent
Covers: WTI oil, Brent oil, SPX500 futures, 8PSB, VWCE, VWRL, 4GLD
Runs daily via cron. Sends HTML email with analysis + positions.

> Python 3.8 — no X|Y unions, no list[x]/dict[x] generics, no match statements.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import anthropic

BASE_DIR = Path(__file__).resolve().parent

# Re-use email_sender from crypto-agent (same repo)
_CRYPTO_AGENT = str(BASE_DIR.parent / "crypto-agent")
if _CRYPTO_AGENT not in sys.path:
    sys.path.insert(0, _CRYPTO_AGENT)

try:
    from email_sender import send_report, build_subject, render_html_email  # noqa: E402
except ImportError as _e:
    raise SystemExit(
        f"Cannot import email_sender from {_CRYPTO_AGENT}.\n"
        f"Make sure you ran 'git pull origin main' on the NAS.\n"
        f"Original error: {_e}"
    )

from data_fetcher import get_all_portfolio_data, get_macro_data  # noqa: E402


# ── .env loader ───────────────────────────────────────────────────────────────

def load_env():
    paths = [BASE_DIR / ".env", BASE_DIR.parent / "crypto-agent" / ".env"]
    cfg = {}
    for p in paths:
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg.setdefault(k.strip(), v.strip())
    return cfg


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state():
    p = BASE_DIR / "state.json"
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {
            "last_run": None, "macro_bias": "NEUTRAL",
            "bias_short": "NEUTRAL", "bias_long": "NEUTRAL",
            "open_positions": [], "active_setups": [],
            "alerted": [], "last_analysis": "",
        }


def save_state(state):
    with open(BASE_DIR / "state.json", "w") as f:
        json.dump(state, f, indent=2, default=str)


def load_pending():
    p = BASE_DIR / "pending_updates.json"
    if p.exists():
        try:
            return json.load(p.open())
        except Exception:
            pass
    return []


def apply_pending(state):
    pending = load_pending()
    if not pending:
        return state, []
    log = []
    positions = {(p["symbol"], p.get("direction", "LONG")): p
                 for p in state.get("open_positions", [])}

    for upd in pending:
        action = upd.get("action", "")
        sym    = upd.get("symbol", "").upper()

        if action == "ENTER":
            key = (sym, upd.get("direction", "LONG").upper())
            pos = {
                "symbol":      sym,
                "direction":   upd.get("direction", "LONG").upper(),
                "market_type": upd.get("market_type", "spot"),
                "tf":          upd.get("tf", "LONG_TERM"),
                "entry_price": upd.get("price"),
                "qty":         upd.get("qty") or upd.get("size_usd"),
                "stop_loss":   upd.get("stop_loss"),
                "tp1":         None,
                "status":      "OPEN",
            }
            positions[key] = pos
            log.append(f"ADOPTED: {sym} {pos['direction']} @ {pos['entry_price']}")

        elif action == "CLOSE":
            key_exact = (sym, upd.get("direction", "").upper())
            key_any   = next((k for k in positions if k[0] == sym), None)
            removed   = positions.pop(key_exact, None) or (
                positions.pop(key_any) if key_any else None
            )
            if removed:
                log.append(f"CLOSED: {sym}")

        elif action == "NOTE":
            key_any = next((k for k in positions if k[0] == sym), None)
            if key_any:
                positions[key_any].setdefault("notes", []).append(upd.get("note", ""))
                log.append(f"NOTE: {sym} — {upd.get('note', '')}")

    state["open_positions"] = list(positions.values())
    try:
        (BASE_DIR / "pending_updates.json").write_text("[]")
    except Exception:
        pass
    return state, log


# ── Analytics helpers ─────────────────────────────────────────────────────────

def compute_pnl(position, prices):
    sym   = position.get("symbol", "").upper()
    entry = position.get("entry_price")
    dirn  = position.get("direction", "LONG").upper()
    current = prices.get(sym, {}).get("price")
    if not entry or not current:
        return None
    pnl = (current - entry) / entry * 100
    return round(pnl if dirn == "LONG" else -pnl, 2)


def price_of(asset, prices):
    return prices.get(asset, {}).get("price")


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _fmt(v, decimals=2):
    if v is None:
        return "N/A"
    return f"{v:.{decimals}f}"


def _fmt_chg(v):
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def build_prices_section(prices):
    lines = []
    for asset in ["WTI", "BRENT", "SPX", "VWCE", "VWRL", "4GLD", "8PSB"]:
        d = prices.get(asset, {})
        p = _fmt(d.get("price"))
        c1 = _fmt_chg(d.get("chg_1d"))
        c5 = _fmt_chg(d.get("chg_5d"))
        c30 = _fmt_chg(d.get("chg_30d"))
        ma20 = _fmt(d.get("ma_20"))
        ma50 = _fmt(d.get("ma_50"))
        fr   = d.get("funding_rate")
        oi   = d.get("oi_usd_bn")
        line = (f"{asset}: {p} | 1d:{c1} 5d:{c5} 30d:{c30} "
                f"| MA20:{ma20} MA50:{ma50}")
        if fr is not None:
            line += f" | FR:{fr}% OI:${oi}B"
        lines.append(line)
    spread = prices.get("wti_brent_spread")
    if spread is not None:
        lines.append(f"WTI/Brent spread: {_fmt(spread)}")
    # Context indicators
    vix    = prices.get("_vix")
    eurusd = prices.get("_eurusd")
    dxy    = prices.get("_dxy")
    if vix    is not None: lines.append(f"VIX: {_fmt(vix, 1)}")
    if eurusd is not None: lines.append(f"EUR/USD: {_fmt(eurusd, 4)}")
    if dxy    is not None: lines.append(f"DXY: {_fmt(dxy, 2)}")
    return "\n".join(lines)


def build_positions_section(state, prices):
    positions = state.get("open_positions", [])
    if not positions:
        return "No open positions."
    lines = []
    for pos in positions:
        sym   = pos.get("symbol", "?")
        dirn  = pos.get("direction", "LONG")
        entry = pos.get("entry_price")
        qty   = pos.get("qty")
        stop  = pos.get("stop_loss")
        current = price_of(sym, prices)
        pnl   = compute_pnl(pos, prices)
        pnl_s = f"{pnl:+.2f}%" if pnl is not None else "N/A"
        pnl_flag = ""
        if pnl is not None:
            if pnl < -15:
                pnl_flag = "🚨 "
            elif pnl < -10:
                pnl_flag = "⚠️ "
        # Approximate EUR market value if qty known
        mkt_val = ""
        if qty and current:
            try:
                mkt_val = f" | Val:€{float(qty)*current:,.0f}"
            except Exception:
                pass
        lines.append(
            f"{pnl_flag}{sym} {dirn} | Entry:{_fmt(entry)} "
            f"Now:{_fmt(current)} P&L:{pnl_s}{mkt_val}"
            + (f" | Stop:{_fmt(stop)}" if stop else "")
        )
    return "\n".join(lines)


# ── State delta extraction ────────────────────────────────────────────────────

def extract_state_delta(text):
    m = re.search(r'\[STATE_DELTA\](.*?)\[/STATE_DELTA\]', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    return None


def extract_email_body(text):
    start = text.find("[EMAIL]")
    end   = text.find("[/EMAIL]")
    if start != -1 and end != -1:
        return text[start + 7:end].strip()
    return text


def merge_delta(prior, delta, prices):
    """Merge STATE_DELTA from Claude into full state."""
    updated = dict(prior)
    updated["last_run"] = datetime.now(timezone.utc).isoformat()

    # Claude-owned fields
    for field in ["macro_bias", "bias_short", "bias_long", "last_analysis",
                  "active_setups", "alerted"]:
        if field in delta:
            updated[field] = delta[field]

    # Merge positions: Claude updates P&L/action fields; Python owns entry/qty/stop
    if "open_positions" in delta:
        prior_map = {p["symbol"]: p for p in prior.get("open_positions", [])}
        merged = []
        for pos in delta["open_positions"]:
            sym = pos.get("symbol", "")
            base = dict(prior_map.get(sym, {}))
            base.update({k: v for k, v in pos.items()
                         if k not in ("entry_price", "qty")})
            merged.append(base)
        # Keep any positions Claude omitted
        delta_syms = {p.get("symbol") for p in delta["open_positions"]}
        for sym, pos in prior_map.items():
            if sym not in delta_syms:
                merged.append(pos)
        updated["open_positions"] = merged

    return updated


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    print(f"[{datetime.utcnow().isoformat()}] ═══ Portfolio Intelligence Agent ═══")

    env = load_env()
    api_key = (os.environ.get("ANTHROPIC_API_KEY")
               or env.get("ANTHROPIC_API_KEY", ""))
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not found in .env", file=sys.stderr)
        return 1

    # ── Step 1: Load state + apply pending Telegram updates ──
    state = load_state()
    state, pending_log = apply_pending(state)

    # ── Step 2: Fetch data ──
    print("[Portfolio] Fetching prices...")
    prices     = get_all_portfolio_data()
    print("[Portfolio] Fetching macro data...")
    macro      = get_macro_data()

    today_str  = datetime.utcnow().strftime("%Y-%m-%d")
    prices_txt = build_prices_section(prices)

    # ── Step 3: Build prompts ──
    with open(BASE_DIR / "CLAUDE.md") as f:
        system_prompt = f.read()

    macro_snapshot = json.dumps({
        k: macro.get(k) for k in [
            "us_10y", "us_30y", "japan_10y", "japan_30y",
            "spx", "usdjpy", "carry_regime", "japan_stress",
            "us_curve_status", "usdjpy_weekly_chg_pct",
            "carry_architecture_alert",
        ]
    }, default=str)

    # Pre-fill header + macro card
    us10  = _fmt(macro.get("us_10y"), 2)
    us30  = _fmt(macro.get("us_30y"), 2)
    j10   = _fmt(macro.get("japan_10y"), 2)
    j30   = _fmt(macro.get("japan_30y"), 2)
    usd   = _fmt(macro.get("usdjpy"), 2)
    carry = macro.get("carry_regime", "N/A")
    spx   = _fmt(macro.get("spx"), 0)

    prefill = (
        f"[EMAIL]\n"
        f"PORTFOLIO BRIEF\n"
        f"{today_str}\n"
        f"\n"
        f"MACRO REGIME\n"
        f"------------------------------\n"
        f"US 10Y: {us10}   30Y: {us30}\n"
        f"JGB10Y: {j10}  30Y: {j30}\n"
        f"SPX   : {spx}\n"
        f"USDJPY: {usd}  Carry: {carry}\n"
        f"------------------------------\n"
        f"SHORT bias:"
    ).rstrip()

    user_prompt = f"""Today is {today_str}.

═══ ASSET PRICES (fetched this run) ═══
{prices_txt}

═══ MACRO SNAPSHOT ═══
{macro_snapshot}

═══ CURRENT STATE ═══
{json.dumps(state, separators=(',', ':'), default=str)}

Pending updates applied this run: {pending_log}

Analysis instructions:
- Use the pre-computed prices above. Do NOT recalculate P&L from scratch.
- WTI and SPX are Tier 1 (active trading). Run full deep 1-week analysis per CLAUDE.md.
- All other assets are Tier 2 (25-year long-term holdings). 3-5 lines max:
  macro regime check + position status + HOLD/ADD/TRIM. No short-term setups.
- 8PSB is the Invesco Physical Silver ETC (XETRA). It tracks physical silver price.
  Silver drivers: USD/DXY (inverse), gold/silver ratio, industrial demand (solar/EVs/electronics
  ~50% of demand), monetary safe-haven demand. More volatile than gold, more industrial beta.
  High gold/silver ratio (>80) historically signals silver is cheap vs gold.
- POSITIONS ARE EMBEDDED in each ticker section — not in a separate block.
  In every section where an open position exists, start the section body with:
    Line 1: LONG/SHORT | Entry:X.XX | Now:X.XX | P&L:±X.X%
    Line 2: Stop:X.XX (or N/A) | Action: <action>
  Then continue with the analysis below that.
  If no position exists for that ticker, skip these lines.
- Bias check: SHORT_TERM positions vs bias_short; LONG_TERM vs bias_long.

═══ EMAIL FORMAT ═══
No markdown. Max ~35 chars/line. Plain text.
Sections IN THIS EXACT ORDER (exact names):

  1. Header (already written)
  2. MACRO REGIME (already written — continue from prefill)
  3. SHORT bias / LONG bias (continue from prefill)
  4. MACRO COMMENTARY
     3-4 lines covering: yield curve direction, carry regime,
     USD direction, net risk-on/off signal for this portfolio.

  5. WTI  [TIER 1 — DEEP ANALYSIS]
     — Position block first (if open position exists)
     — Then cover in order:
       Price, MA20/50, trend + % distance
       Geopolitical premium: active risks + LOW/MED/HIGH
       OPEC+ stance: RESTRICTIVE/NEUTRAL/LOOSENING
       USD/DXY direction + oil correlation
       Demand: China + US + seasonal (one line)
       MEXC funding rate + OI if available
       WTI/Brent spread
       1-week base case: dominant driver, key level to watch

  6. BRENT  [TIER 2 — 3-5 lines]
     — Position block first (if open position exists)
     — Brent/WTI spread. Macro regime. Action.

  7. SPX  [TIER 1 — DEEP ANALYSIS]
     — Position block first (if open position exists)
     — Then cover in order:
       Price, MA20/50, trend, distance from ATH
       US 10Y/30Y level + direction + real yield signal
       Curve shape + recession flag if inverted
       JPY carry: USDJPY trend + carry regime risk
       Liquidity: INJECTING/NEUTRAL/DRAINING
       Earnings: season status + mega-cap tech pulse
       Inflation/employment → Fed reaction in one line
       VIX: level + complacency/normal/fear signal
       MEXC funding rate + OI if available
       1-week base case: dominant driver, key events this week

  8. VWCE / VWRL  [TIER 2 — 3-5 lines]
     — Position block first (if open position exists)
     — EUR/USD impact. Macro regime. Structural flag if present. Action.

  9. GOLD  [TIER 2 — 3-5 lines]
     — Position block first (if open position exists)
     — DXY direction. Real yield proxy. Action: HOLD_CORE/ADD/TRIM.

  10. SILVER  [TIER 2 — 3-5 lines]
      (8PSB = Invesco Physical Silver ETC, tracks physical silver)
      — Position block first (if open position exists)
      — DXY direction. Gold/silver ratio (high >80 = silver cheap).
        Industrial demand pulse (solar/EVs). Action: HOLD_CORE/ADD/TRIM.

  11. SETUPS (Tier 1 only — WTI and SPX; write "None." if empty)
  12. CHANGES TODAY

[/EMAIL]

[STATE_DELTA]
{{Only these Claude-owned fields:
  macro_bias, bias_short, bias_long, last_analysis,
  active_setups, open_positions (P&L/action only — no entry_price/qty override), alerted}}
[/STATE_DELTA]
"""

    # ── Step 4: Call Claude ──
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

    response = prefill + message.content[0].text
    tokens_in          = message.usage.input_tokens
    tokens_cache_read  = getattr(message.usage, "cache_read_input_tokens", 0)
    tokens_cache_write = getattr(message.usage, "cache_creation_input_tokens", 0)
    tokens_out         = message.usage.output_tokens
    cost_usd = (
        (tokens_in          * 0.80) +
        (tokens_cache_read  * 0.08) +
        (tokens_cache_write * 1.00) +
        (tokens_out         * 4.00)
    ) / 1_000_000
    print(f"[Portfolio] Tokens: in={tokens_in} cache_read={tokens_cache_read} "
          f"out={tokens_out} cost=${cost_usd:.4f}")

    # ── Step 5: Update state ──
    delta = extract_state_delta(response)
    if delta:
        updated_state = merge_delta(state, delta, prices)
        save_state(updated_state)
        print(f"[{datetime.utcnow().isoformat()}] State updated via delta")
    else:
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        updated_state = state
        print(f"[{datetime.utcnow().isoformat()}] WARNING: no STATE_DELTA found")

    # ── Step 6: Save full report ──
    report_path = BASE_DIR / f"portfolio_report_{today_str}.txt"
    report_path.write_text(response)
    print(f"[{datetime.utcnow().isoformat()}] Report saved: {report_path}")

    # ── Step 7: Send email ──
    email_body   = extract_email_body(response)
    macro_bias   = updated_state.get("macro_bias", "NEUTRAL")
    setup_count  = len(updated_state.get("active_setups", []))
    enter_count  = sum(1 for s in updated_state.get("active_setups", [])
                       if s.get("status") == "ENTER")
    subject = (f"🔴 PORTFOLIO ENTRY — {today_str} | {macro_bias} | {enter_count} ENTER"
               if enter_count > 0
               else f"📈 Portfolio Brief — {today_str} | {macro_bias} | {setup_count} setups")

    email_ok = send_report(
        subject=subject,
        body=email_body,
        attachment=response,
        attachment_filename=f"portfolio_full_{today_str}.txt",
    )

    # ── Step 8: Log ──
    log_line = (
        f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC | "
        f"{macro_bias} | {setup_count} setups | {enter_count} ENTER | "
        f"email:{'OK' if email_ok else 'FAIL'} | cost:${cost_usd:.4f}\n"
    )
    with open(BASE_DIR / "report.log", "a") as f:
        f.write(log_line)

    print(f"[{datetime.utcnow().isoformat()}] Done. {log_line.strip()}")
    print("\n" + "=" * 60)
    print(response)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(run())
