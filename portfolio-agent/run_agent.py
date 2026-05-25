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

_SHARED = str(BASE_DIR.parent / "shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

try:
    from email_sender import send_report, build_subject, render_html_email  # noqa: E402
except ImportError as _e:
    raise SystemExit(
        f"Cannot import email_sender from {_CRYPTO_AGENT}.\n"
        f"Make sure you ran 'git pull origin main' on the NAS.\n"
        f"Original error: {_e}"
    )

from utils import load_env as _load_env, _fmt, avg_into_position, reduce_position  # noqa: E402
from assets import PORTFOLIO_ASSETS  # noqa: E402
from data_fetcher import get_all_portfolio_data, get_macro_data, get_crs_data  # noqa: E402


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
            direction   = upd.get("direction", "LONG").upper()
            key         = (sym, direction)
            price       = upd.get("price")
            size_usd    = upd.get("size_usd")
            size_qty    = upd.get("size_qty") or upd.get("qty")

            if key in positions and price is not None:
                # Existing position — average in the new entry
                avg_into_position(positions[key], price,
                                  new_qty=size_qty, new_size_usd=size_usd)
                new_avg = positions[key]["entry_price"]
                log.append(f"AVERAGED IN: {sym} {direction} @ {price} "
                            f"→ new avg {new_avg}")
            else:
                pos = {
                    "symbol":      sym,
                    "direction":   direction,
                    "market_type": upd.get("market_type", "spot"),
                    "tf":          upd.get("tf", "LONG_TERM"),
                    "entry_price": price,
                    "qty":         size_qty,
                    "size_usd":    size_usd,
                    "stop_loss":   upd.get("stop_loss"),
                    "tp1":         None,
                    "status":      "OPEN",
                }
                positions[key] = pos
                log.append(f"ADOPTED: {sym} {direction} @ {price}")

        elif action == "CLOSE":
            direction = upd.get("direction", "").upper()
            key_exact = (sym, direction) if direction else None
            key_any   = next((k for k in positions if k[0] == sym), None)

            close_qty = upd.get("close_qty")
            close_pct = upd.get("close_pct")
            close_usd = upd.get("close_usd")
            has_size  = any(v is not None for v in (close_qty, close_pct, close_usd))

            matched = (key_exact if key_exact and key_exact in positions
                       else key_any)

            if matched:
                if upd.get("partial") and has_size:
                    updated = reduce_position(positions[matched],
                                             close_qty=close_qty,
                                             close_pct=close_pct,
                                             close_usd=close_usd)
                    if updated is None:
                        del positions[matched]
                        log.append(f"CLOSED: {sym} (partial consumed remaining qty)")
                    else:
                        positions[matched] = updated
                        log.append(f"PARTIAL CLOSE: {sym} "
                                   f"→ remaining qty {updated.get('qty', '?')}")
                elif upd.get("partial"):
                    positions[matched]["notes"] = (
                        (positions[matched].get("notes") or "") +
                        " | Partial close flagged."
                    )
                    log.append(f"PARTIAL CLOSE flagged: {sym}")
                else:
                    del positions[matched]
                    log.append(f"CLOSED: {sym}")
            else:
                log.append(f"CLOSE {sym}: not found in open positions (ignored)")

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


# ── Crash Risk Score ─────────────────────────────────────────────────────────

def compute_crash_risk_score(crs_data, macro):
    # type: (dict, dict) -> tuple
    """
    0-10 composite Crash Risk Score from 120-year cross-cycle indicator research.
    Returns (float score, dict components, str regime).

    Weights (sum to 100%):
      HY credit spread  25%  — highest cross-crash hit rate
      Yield curve 2s10s 20%  — recession predictor (replaces less-reliable 10/30 spread)
      VIX term structure15%  — near-term stress gauge
      Carry regime      15%  — existing variable
      Real yield (TIPS) 10%  — P/E multiple driver
      ISM PMI            7.5% — economic cycle
      Bank lending std   7.5% — credit cycle turning point
      Japan stress       5%  — existing variable
      Modifier bonuses:  Cu/Gold ratio + SOFR stress (capped +2.0)
    """
    score = 0.0
    comp  = {}

    # 1. HY Credit Spread — max 2.5 pts
    hy = crs_data.get("hy_oas")
    if hy is not None:
        if   hy < 350:  c1, cr = 0.0, "CREDIT_TIGHT"
        elif hy < 450:  c1, cr = 0.5, "CREDIT_NORMAL"
        elif hy < 600:  c1, cr = 1.5, "CREDIT_ELEVATED"
        elif hy < 900:  c1, cr = 2.0, "CREDIT_STRESS"
        else:           c1, cr = 2.5, "CREDIT_CRISIS"
        score += c1
        comp["hy_credit_regime"] = cr
        comp["hy_oas_bps"]       = int(round(hy))
    else:
        comp["hy_credit_regime"] = "NO_DATA"

    # 2. Yield curve (2s10s + 3m10y) — max 2.0 pts
    c2s10s = crs_data.get("curve_2s10s")
    c3m10y = crs_data.get("curve_3m10y")
    valid  = [v for v in (c2s10s, c3m10y) if v is not None]
    if valid:
        n_inv   = sum(1 for v in valid if v < -0.25)
        deepest = min(valid)
        if n_inv == 0:
            c2, crv = 0.0, "CURVE_NORMAL"
        elif n_inv == 1:
            c2, crv = 0.5, "CURVE_FLAT"
        elif deepest > -0.5:
            c2, crv = 1.0, "CURVE_INVERTED"
        else:
            c2, crv = 1.5, "CURVE_DEEP_INVERTED"
        score += c2
        comp["curve_2s10s_status"] = crv
        if c2s10s is not None:
            comp["curve_2s10s_bps"] = int(round(c2s10s * 100))
        if c3m10y is not None:
            comp["curve_3m10y_bps"] = int(round(c3m10y * 100))
    else:
        comp["curve_2s10s_status"] = "NO_DATA"

    # 3. VIX level + term structure — max 1.5 pts
    vix   = crs_data.get("vix_spot") or macro.get("vix")
    vix9d = crs_data.get("vix_9d")
    if vix is not None:
        if   vix < 15:  c3, vts = 0.0, "VTS_COMPLACENCY"
        elif vix < 20:  c3, vts = 0.3, "VTS_CONTANGO"
        elif vix < 25:  c3, vts = 0.6, "VTS_FLAT"
        elif vix < 35:  c3, vts = 1.0, "VTS_BACKWARDATION"
        else:           c3, vts = 1.5, "VTS_DEEP_BACKWARDATION"
        # VIX9D > VIX = near-term fear above far-term = backwardation premium
        if vix9d is not None and vix9d > vix * 1.05 and c3 < 1.5:
            c3  = min(c3 + 0.3, 1.5)
            vts = "VTS_BACKWARDATION"
        score += c3
        comp["vix_term_structure"] = vts
        comp["vix_spot"]           = round(vix, 1)
        if vix9d is not None:
            comp["vix_9d"] = round(vix9d, 1)
    else:
        comp["vix_term_structure"] = "NO_DATA"

    # 4. Carry regime (existing agent variable) — max 1.5 pts
    carry = macro.get("carry_regime", "CARRY_STABLE")
    c4 = {"CARRY_STABLE": 0.0, "CARRY_STRESS": 0.5,
          "CARRY_UNWIND": 1.0, "CARRY_COLLAPSE": 1.5}.get(carry, 0.0)
    score += c4
    comp["carry_regime_crs"] = carry

    # 5. Real yield 10Y TIPS — max 1.0 pt
    tips = crs_data.get("tips_10y")
    if tips is not None:
        if   tips < -0.5: c5, ry = 0.0, "REAL_YIELD_VERY_NEGATIVE"
        elif tips < 0.0:  c5, ry = 0.2, "REAL_YIELD_NEGATIVE"
        elif tips < 1.0:  c5, ry = 0.5, "REAL_YIELD_POSITIVE"
        elif tips < 2.0:  c5, ry = 0.7, "REAL_YIELD_HIGH"
        else:             c5, ry = 1.0, "REAL_YIELD_EXTREME"
        score += c5
        comp["real_yield_regime"] = ry
        comp["tips_10y"]          = tips
    else:
        # Fallback: estimate from us_10y - 2.5%
        us10 = macro.get("us_10y")
        if us10 is not None:
            est = us10 - 2.5
            if   est < 0.0: c5, ry = 0.2, "EST_REAL_NEGATIVE"
            elif est < 1.0: c5, ry = 0.5, "EST_REAL_POSITIVE"
            elif est < 2.0: c5, ry = 0.7, "EST_REAL_HIGH"
            else:           c5, ry = 1.0, "EST_REAL_EXTREME"
            score += c5
            comp["real_yield_regime"] = ry

    # 6. ISM Manufacturing PMI — max 0.75 pts
    pmi = crs_data.get("ism_pmi")
    if pmi is not None:
        if   pmi > 52: c6, pr = 0.00, "PMI_EXPANDING"
        elif pmi > 50: c6, pr = 0.15, "PMI_MODERATE"
        elif pmi > 48: c6, pr = 0.35, "PMI_STALL"
        elif pmi > 46: c6, pr = 0.55, "PMI_CONTRACTING"
        else:          c6, pr = 0.75, "PMI_DEEP_CONTRACTION"
        score += c6
        comp["pmi_regime"] = pr
        comp["ism_pmi"]    = pmi

    # 7. Bank lending standards (SLOOS) — max 0.75 pts
    sloos = crs_data.get("lending_std")
    if sloos is not None:
        if   sloos < 0:  c7, lr = 0.00, "LENDING_EASING"
        elif sloos < 10: c7, lr = 0.15, "LENDING_NEUTRAL"
        elif sloos < 25: c7, lr = 0.40, "LENDING_TIGHTENING"
        elif sloos < 50: c7, lr = 0.60, "LENDING_SHARPLY_TIGHTENING"
        else:            c7, lr = 0.75, "LENDING_CRISIS_TIGHTENING"
        score += c7
        comp["lending_regime"] = lr
        comp["sloos_pct"]      = sloos

    # 8. Japan stress (existing) — max 0.5 pts
    jstress = macro.get("japan_stress", "NORMAL")
    c8 = {"NORMAL": 0.0, "ELEVATED": 0.15,
          "HIGH": 0.3,   "CRITICAL": 0.5}.get(jstress, 0.0)
    score += c8
    comp["japan_stress_crs"] = jstress

    # ── Modifier bonuses (cap +2.0) ───────────────────────────────────────────
    bonuses = 0.0

    cu = crs_data.get("copper_price")
    gp = crs_data.get("gold_price")
    if cu is not None and gp is not None and gp > 0:
        cu_gold = cu / gp
        comp["copper_gold_ratio"] = round(cu_gold, 5)
        # Stress: copper cheap vs gold (industrial demand fear dominating)
        # Historical threshold ~0.0013–0.0017 (copper ~$4/lb, gold ~$2500/oz)
        if cu_gold < 0.0015:
            bonuses += 0.5
            comp["copper_gold_signal"] = "STRESS"
        else:
            comp["copper_gold_signal"] = "NORMAL"

    sofr = crs_data.get("sofr")
    ffu  = crs_data.get("fed_funds_upper")
    if sofr is not None and ffu is not None:
        spread_bps = round((sofr - ffu) * 100, 1)
        comp["sofr_spread_bps"] = spread_bps
        if spread_bps > 25:
            bonuses += 0.5
            comp["repo_stress"] = "REPO_STRESS"
        elif spread_bps > 10:
            bonuses += 0.25
            comp["repo_stress"] = "REPO_ELEVATED"
        else:
            comp["repo_stress"] = "REPO_NORMAL"

    score = min(round(score + min(bonuses, 2.0), 1), 10.0)

    if   score <= 3.9: regime = "LOW"
    elif score <= 5.9: regime = "MODERATE"
    elif score <= 7.4: regime = "ELEVATED"
    elif score <= 8.9: regime = "HIGH"
    else:              regime = "CRITICAL"

    comp["crs_regime"] = regime
    return score, comp, regime


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

def _fmt_chg(v):
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def build_prices_section(prices):
    lines = []
    for asset in PORTFOLIO_ASSETS:
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


def merge_delta(prior, delta, prices, crs_score=None, crs_regime=None, crs_comp=None):
    """Merge STATE_DELTA from Claude into full state."""
    updated = dict(prior)
    updated["last_run"] = datetime.now(timezone.utc).isoformat()

    # Python-computed CRS fields (not delegated to Claude)
    if crs_score is not None:
        updated["crash_risk_score"]  = crs_score
        updated["crash_risk_regime"] = crs_regime
        updated["crs_components"]    = crs_comp

    # Claude-owned fields
    for field in ["macro_bias", "bias_short", "bias_long", "last_analysis",
                  "active_setups", "alerted"]:
        if field in delta:
            updated[field] = delta[field]

    # Merge positions: Claude updates P&L/action fields; Python owns entry/qty/stop.
    # Key is (symbol, direction) so BTC LONG and BTC SHORT can coexist.
    if "open_positions" in delta:
        prior_map = {(p["symbol"], p.get("direction", "LONG")): p
                     for p in prior.get("open_positions", [])}
        merged = []
        for pos in delta["open_positions"]:
            key = (pos.get("symbol", ""), pos.get("direction", "LONG"))
            base = dict(prior_map.get(key, {}))
            base.update({k: v for k, v in pos.items()
                         if k not in ("entry_price", "qty")})
            merged.append(base)
        # Keep any positions Claude omitted
        delta_keys = {(p.get("symbol"), p.get("direction", "LONG"))
                      for p in delta["open_positions"]}
        for key, pos in prior_map.items():
            if key not in delta_keys:
                merged.append(pos)
        updated["open_positions"] = merged

    return updated


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    print(f"[{datetime.utcnow().isoformat()}] ═══ Portfolio Intelligence Agent ═══")

    env = _load_env(BASE_DIR / ".env", BASE_DIR.parent / "crypto-agent" / ".env")
    api_key = (os.environ.get("ANTHROPIC_API_KEY")
               or env.get("ANTHROPIC_API_KEY", ""))
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not found in .env", file=sys.stderr)
        return 1

    # ── Step 1: Load state + apply pending Telegram updates ──
    state = load_state()
    state, pending_log = apply_pending(state)

    fred_key = env.get("FRED_API_KEY", "")

    # ── Step 2: Fetch data ──
    print("[Portfolio] Fetching prices...")
    prices = get_all_portfolio_data()
    print("[Portfolio] Fetching macro data...")
    macro  = get_macro_data()
    print("[Portfolio] Fetching CRS data...")
    crs_data = get_crs_data(fred_key=fred_key or None,
                             vix_spot=prices.get("_vix"))
    crs_score, crs_comp, crs_regime = compute_crash_risk_score(crs_data, macro)
    print(f"[Portfolio] CRS: {crs_score}/10 ({crs_regime})")

    today_str  = datetime.utcnow().strftime("%Y-%m-%d")
    prices_txt = build_prices_section(prices)

    # ── Step 3: Build prompts ──
    with open(BASE_DIR / "CLAUDE.md") as f:
        system_prompt = f.read()

    macro_snapshot = json.dumps(dict(
        {k: macro.get(k) for k in [
            "us_10y", "us_30y", "japan_10y", "japan_30y",
            "spx", "usdjpy", "carry_regime", "japan_stress",
            "us_curve_status", "usdjpy_weekly_chg_pct",
            "carry_architecture_alert",
        ]},
        crash_risk_score=crs_score,
        crash_risk_regime=crs_regime,
        crs_components=crs_comp,
    ), default=str)

    # Pre-fill header + macro card
    us10  = _fmt(macro.get("us_10y"), 2)
    us30  = _fmt(macro.get("us_30y"), 2)
    j10   = _fmt(macro.get("japan_10y"), 2)
    j30   = _fmt(macro.get("japan_30y"), 2)
    usd   = _fmt(macro.get("usdjpy"), 2)
    carry = macro.get("carry_regime", "N/A")
    spx   = _fmt(macro.get("spx"), 0)

    hy_str  = (f"{crs_comp['hy_oas_bps']}bps"
               if crs_comp.get("hy_oas_bps") is not None else "N/A")
    crv_str = crs_comp.get("curve_2s10s_status", "N/A")

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
        f"CRS   : {crs_score}/10 ({crs_regime})"
        f"  HY:{hy_str} Curve:{crv_str}\n"
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
- CRASH RISK SCORE (CRS={crs_score}/10, {crs_regime}): Use this composite to calibrate
  the urgency of warnings. CRS ≤ 4 = low systemic risk (normal sizing). CRS 5-6 = note
  in MACRO COMMENTARY. CRS 7-7.9 = flag WARNING, review Tier 2 positions. CRS ≥ 8 =
  equivalent to CARRY_COLLAPSE — trigger TRIM review for VWCE/VWRL, flag SPX cautiously.
  CRS components: {json.dumps(crs_comp, separators=(',',':'))}

═══ EMAIL FORMAT — MANDATORY ═══
No markdown. Max ~35 chars/line. Plain text.
RULE: Each section header is a BARE LINE — the section name ONLY,
nothing else. Immediately follow every header with a ------ divider.
NEVER append [TIER...], (8PSB), or any annotation to a section name.

Section order and EXACT header names to write:

  MACRO COMMENTARY
  ← 3-4 lines: yield curve, carry regime, USD, net signal

  WTI
  ← Position block first (if open position exists, write:
      LONG/SHORT | Entry:X.XX | Now:X.XX | P&L:±X.X%
      Stop:X.XX | Action: <action>
    Then: 8-12 lines covering geo risk, OPEC+, USD/DXY,
    demand pulse, technical (MA20/50, key levels), funding
    rate/OI, WTI/Brent spread, 1-week base case.

  BRENT
  ← Position block if open. Brent/WTI spread. Macro. Action.
    3-5 lines max.

  SPX
  ← Position block if open.
    8-12 lines: yields (10Y/30Y level+direction), real yield,
    JPY carry risk, liquidity, earnings pulse,
    inflation/employment → Fed reaction, VIX,
    technical (MA20/50, ATH distance), funding/OI,
    1-week base case + key events.

  VWCE
  ← VWCE position block if open (own entry/P&L/stop/action).
    EUR/USD. Macro regime. CRS impact if elevated. Action.
    3-5 lines max.

  VWRL
  ← VWRL position block if open (own entry/P&L/stop/action).
    Same macro drivers as VWCE. Dividend note if applicable.
    3-5 lines max.

  GOLD
  ← Position block if open. DXY. Real yield. Action.
    3-5 lines max.

  SILVER
  ← (8PSB = Invesco Physical Silver ETC, tracks physical silver)
    Position block if open. DXY. Gold/silver ratio.
    Industrial demand pulse. Action: HOLD_CORE/ADD/TRIM.
    3-5 lines max.

  SETUPS
  ← Tier 1 (WTI, SPX) only. Write "None." if empty.
    Each setup MUST start with EXACTLY "SYMBOL LONG" or "SYMBOL SHORT"
    on its own line (no extra text on that first line). Example:

    WTI LONG
    Status: WAITING
    Range: 88.00-92.00
    Stop: 84.00
    Target: 100.00
    Note: one short context line

    SPX SHORT
    Status: APPROACHING
    Range: 7500-7550
    Stop: 7650
    Target: 7200
    Note: one short context line

  CHANGES TODAY
  ← One bullet per change. Tags: NEW / ENTER / REVISED /
    HOLD / ADD / TRIM / ADOPTED

[NOTE: The email has already been started for you.
 MACRO REGIME is pre-filled. Continue from SHORT bias.
 Your response will be appended after "SHORT bias: "
 Write ONLY what comes after — do NOT repeat MACRO REGIME.]

CRITICAL FORMAT RULE: Use key: value rows for ALL analysis content.
No prose paragraphs. Every line must be "Key : Value" or a single
short sentence. Max ~35 chars per line. No line wraps.

Your output must continue as:
<bias_short>  (days–weeks)
LONG  bias: <bias_long>   (months+)
------------------------------

MACRO COMMENTARY
------------------------------
Curve : <shape> — <signal>
Carry : <carry_regime> — <impact>
USD   : <DXY trend> — <effect>
Signal: <net risk-on/off verdict>
------------------------------

WTI
------------------------------
<If open position:>
Dir   : LONG/SHORT | Entry:X.XX
Now   : X.XX | P&L: ±X.X%
Stop  : X.XX | Action: <action>
<Analysis — use kv rows:>
Price : $XX.XX | MA20:$XX MA50:$XX
Trend : <above/below MAs>
Geo   : LOW/MED/HIGH — <reason>
OPEC+ : RESTRICTIVE/NEUTRAL/LOOSENING
Demand: <China+US+seasonal, 1 line>
FR    : X.XX% | OI: <rising/falling>
Spread: WTI/Brent $X.XX (<normal/wide>)
1-wk  : <dominant driver — key level>
------------------------------

BRENT
------------------------------
<If open position: same Dir/Now/Stop/Action block>
Spread: $X.XX vs WTI (<normal/wide>)
Regime: <follow WTI / macro signal>
Action: HOLD/ADD/TRIM
------------------------------

SPX
------------------------------
<If open position:>
Dir   : LONG/SHORT | Entry:X,XXX
Now   : X,XXX | P&L: ±X.X%
Stop  : X,XXX | Action: <action>
<Analysis — use kv rows:>
Price : X,XXX | MA20:X,XXX MA50:X,XXX
Trend : <above/below MAs — ATH dist>
10Y   : X.XX% <rising/falling> — <effect>
30Y   : X.XX% — <funding pressure?>
Carry : USDJPY X.XX CARRY_<REGIME>
Liquid: INJECTING/NEUTRAL/DRAINING
Earn  : <season status / beat rate>
VIX   : XX.X → <complacent/normal/fear>
FR    : X.XX% | OI: <rising/falling>
1-wk  : <dominant driver — key events>
------------------------------

VWCE
------------------------------
<VWCE position block if open, then 3-5 lines>
------------------------------

VWRL
------------------------------
<VWRL position block if open, then 3-5 lines>
------------------------------

GOLD
------------------------------
<position block if open, then 3-5 lines>
------------------------------

SILVER
------------------------------
<position block if open, then 3-5 lines>
------------------------------

SETUPS
------------------------------
<One card per setup. FIRST LINE must be exactly "SYMBOL LONG" or "SYMBOL SHORT" — nothing else.>
WTI LONG
Status: WAITING
Range: X.XX-X.XX
Stop: X.XX
Target: X.XX
Note: <one line>
------------------------------

CHANGES TODAY
------------------------------
<one bullet per change>
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
        updated_state = merge_delta(state, delta, prices,
                                    crs_score=crs_score,
                                    crs_regime=crs_regime,
                                    crs_comp=crs_comp)
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
