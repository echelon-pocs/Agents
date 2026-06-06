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
import time
from datetime import date, datetime, timezone
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
from data_fetcher import (get_all_portfolio_data, get_macro_data, get_crs_data,  # noqa: E402
                          get_wti_news, get_earnings_calendar)


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


# ── Setup lifecycle helpers ───────────────────────────────────────────────────

def expire_stale_setups(state, today_str):
    # type: (dict, str) -> tuple
    """Remove setups older than max_age_days (default 7). Returns (state, expired_list)."""
    active = state.get("active_setups", [])
    valid, expired_log = [], []
    today_d = date.fromisoformat(today_str[:10])
    for s in active:
        created = s.get("created_at")
        max_age = s.get("max_age_days", 7)
        if created:
            try:
                days_old = (today_d - date.fromisoformat(created[:10])).days
                if days_old > max_age:
                    expired_log.append(s)
                    continue
            except Exception:
                pass
        valid.append(s)
    state["active_setups"] = valid
    return state, expired_log


def compute_macro_delta(state, macro, crs_score, crs_regime):
    # type: (dict, dict, float, str) -> str
    """Human-readable delta of key macro values vs the prior run snapshot."""
    last = state.get("last_macro", {})
    if not last:
        return "No prior run data."
    lines = []
    for key, label in [("us_10y", "US10Y"), ("us_30y", "US30Y"), ("usdjpy", "USDJPY")]:
        prev = last.get(key)
        curr = macro.get(key)
        if prev is not None and curr is not None:
            chg  = curr - prev
            sign = "+" if chg >= 0 else ""
            lines.append(f"{label}: {curr:.2f} ({sign}{chg:.3f} vs last run)")
    prev_crs = last.get("crs_score")
    if prev_crs is not None:
        chg  = crs_score - prev_crs
        sign = "+" if chg >= 0 else ""
        lines.append(f"CRS: {crs_score} ({sign}{round(chg, 1)})")
    prev_crs_regime = last.get("crs_regime", "")
    if prev_crs_regime and prev_crs_regime != crs_regime:
        lines.append(f"CRS regime: {prev_crs_regime} -> {crs_regime}")
    prev_carry = last.get("carry_regime", "")
    curr_carry = macro.get("carry_regime", "")
    if prev_carry and curr_carry and prev_carry != curr_carry:
        lines.append(f"Carry: {prev_carry} -> {curr_carry}")
    return "\n".join(lines) if lines else "No significant changes since last run."


def log_setup_outcomes(prior_setups, updated_setups, today_str, base_dir):
    # type: (List[dict], List[dict], str, object) -> None
    """Append a record to setups_log.jsonl when a setup disappears or is invalidated."""
    prior_map   = {(s.get("symbol"), s.get("direction", "")): s for s in prior_setups}
    updated_map = {(s.get("symbol"), s.get("direction", "")): s for s in updated_setups}
    log_path    = base_dir / "setups_log.jsonl"
    for key, setup in prior_map.items():
        outcome = None
        if key not in updated_map:
            outcome = "REMOVED"
        elif (updated_map[key].get("status") == "INVALIDATED"
              and setup.get("status") != "INVALIDATED"):
            outcome = "INVALIDATED"
        if outcome:
            entry = {
                "date":         today_str,
                "symbol":       setup.get("symbol"),
                "direction":    setup.get("direction"),
                "prior_status": setup.get("status"),
                "outcome":      outcome,
                "range":        setup.get("range"),
                "stop":         setup.get("stop"),
                "target":       setup.get("target"),
                "conviction":   setup.get("conviction"),
                "created_at":   setup.get("created_at"),
            }
            try:
                with open(log_path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception as e:
                print(f"[Portfolio] setup log write error: {e}")


def _call_claude(client, max_retries=2, **kwargs):
    # type: (object, int, **object) -> object
    """Call client.messages.create with up to max_retries retries (exponential backoff)."""
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"[Portfolio] Claude API error (attempt {attempt + 1}/{max_retries + 1}): "
                      f"{e}. Retrying in {wait}s...")
                time.sleep(wait)
    raise last_err


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


def build_prices_section(prices, portfolio_value_eur=None):
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
        if asset in ("WTI", "SPX"):
            atr  = d.get("atr_14")
            r20h = d.get("range_20_high")
            r20l = d.get("range_20_low")
            recent = d.get("closes_10d", [])
            if atr is not None:
                lines.append(f"  ATR14:{_fmt(atr)} | 20dHi:{_fmt(r20h)} 20dLo:{_fmt(r20l)}")
            if recent:
                lines.append("  Recent5d: " + " ".join(_fmt(c) for c in recent[-5:]))
            weekly = d.get("weekly", {})
            if weekly:
                w52h = _fmt(weekly.get("w52_high"))
                w52l = _fmt(weekly.get("w52_low"))
                watr = _fmt(weekly.get("weekly_atr"))
                lines.append(f"  52wk:{w52l}-{w52h} | wATR:{watr}")
            if portfolio_value_eur and atr is not None and atr > 0:
                stop_d   = atr * 1.5
                qty_est  = (portfolio_value_eur * 0.01) / stop_d
                px       = float(d.get("price") or 0)
                notional = qty_est * px
                lines.append(f"  Sizing(1%): qty≈{qty_est:.2f} ~${notional:,.0f}")
        if asset == "WTI":
            inv_l = prices.get("_crude_inv_level_kb")
            inv_c = prices.get("_crude_inv_chg_kb")
            if inv_l is not None:
                if inv_c is not None:
                    sign = "+" if inv_c >= 0 else ""
                    lines.append(f"  EIA Stocks: {inv_l:,}kb | Chg:{sign}{inv_c:,}kb")
                else:
                    lines.append(f"  EIA Stocks: {inv_l:,}kb")
            rig = prices.get("_rig_count")
            if rig is not None:
                lines.append(f"  Rig Count: {int(rig)}")
    spread = prices.get("wti_brent_spread")
    if spread is not None:
        lines.append(f"WTI/Brent spread: {_fmt(spread)}")
    gsr = prices.get("gold_silver_ratio")
    if gsr is not None:
        lines.append(f"Gold/Silver Ratio: {gsr}")
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


def merge_delta(prior, delta, prices, crs_score=None, crs_regime=None, crs_comp=None,
                today_str=None):
    """Merge STATE_DELTA from Claude into full state."""
    updated = dict(prior)
    updated["last_run"] = datetime.now(timezone.utc).isoformat()

    # Python-computed CRS fields (not delegated to Claude)
    if crs_score is not None:
        updated["crash_risk_score"]  = crs_score
        updated["crash_risk_regime"] = crs_regime
        updated["crs_components"]    = crs_comp

    # Claude-owned scalar fields
    for field in ["macro_bias", "bias_short", "bias_long", "last_analysis", "alerted"]:
        if field in delta:
            updated[field] = delta[field]

    # active_setups: preserve created_at from prior state for unchanged setups
    if "active_setups" in delta:
        prior_setup_map = {
            (s.get("symbol"), s.get("direction", "")): s
            for s in prior.get("active_setups", [])
        }
        merged_setups = []
        for s in delta["active_setups"]:
            key   = (s.get("symbol"), s.get("direction", ""))
            prior_s = prior_setup_map.get(key, {})
            ms = dict(s)
            if not ms.get("created_at"):
                ms["created_at"] = prior_s.get("created_at") or today_str or ""
            if not ms.get("max_age_days") and prior_s.get("max_age_days"):
                ms["max_age_days"] = prior_s["max_age_days"]
            merged_setups.append(ms)
        updated["active_setups"] = merged_setups

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


# ── Portfolio heat ────────────────────────────────────────────────────────────

def compute_portfolio_heat(positions, prices):
    # type: (List[dict], dict) -> tuple
    """Return (total_eur_val, avg_pnl_pct_or_None, stressed_count)."""
    total_val = 0.0
    pnls      = []  # type: List[float]
    stressed  = 0
    for pos in positions:
        qty   = pos.get("qty")
        curr  = price_of(pos.get("symbol", "").upper(), prices)
        pnl   = compute_pnl(pos, prices)
        if qty is not None and curr is not None:
            try:
                total_val += float(qty) * float(curr)
            except Exception:
                pass
        if pnl is not None:
            pnls.append(pnl)
            if pnl < -10:
                stressed += 1
    avg_pnl = round(sum(pnls) / len(pnls), 1) if pnls else None
    return round(total_val), avg_pnl, stressed


# ── Prior analysis verdicts ───────────────────────────────────────────────────

def parse_last_analysis_verdicts(state):
    # type: (dict) -> dict
    """Parse last_analysis from state — accepts dict (new) or JSON string (legacy)."""
    la = state.get("last_analysis")
    if isinstance(la, dict):
        return la
    if isinstance(la, str) and la.strip().startswith("{"):
        try:
            return json.loads(la)
        except Exception:
            pass
    return {}


# ── Telegram alerting ─────────────────────────────────────────────────────────

def send_telegram_alert(env, setups_entering):
    # type: (dict, List[dict]) -> None
    """Send a Telegram message for each newly-ENTER setup."""
    token   = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    import requests as _req
    for s in setups_entering:
        sym       = s.get("symbol", "?")
        dirn      = s.get("direction", "?")
        conviction = s.get("conviction", "?")
        rng       = s.get("range", "?")
        stop      = s.get("stop", "?")
        target    = s.get("target", "?")
        note      = (s.get("note") or "").strip()
        msg = (
            f"\U0001f534 *PORTFOLIO ENTER: {sym} {dirn}*\n"
            f"Range: {rng}\n"
            f"Stop: {stop} | Target: {target}\n"
            f"Conviction: {conviction}"
            + (f"\n_{note}_" if note else "")
        )
        try:
            _req.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                timeout=10,
            )
            print(f"[Portfolio] Telegram ENTER alert sent: {sym} {dirn}")
        except Exception as e:
            print(f"[Portfolio] Telegram alert error: {e}")


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

    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    state, expired_setups = expire_stale_setups(state, today_str)
    if expired_setups:
        print(f"[Portfolio] Expired {len(expired_setups)} stale setup(s): "
              + ", ".join(s.get("symbol", "?") for s in expired_setups))

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

    if not fred_key:
        print("[Portfolio] WARNING: FRED_API_KEY not set — HY OAS, 2s10s, TIPS, ISM, "
              "SLOOS, SOFR, TGA, RRP, EIA crude missing. CRS based on carry+Japan+VIX only. "
              "Free key: https://fred.stlouisfed.org/docs/api/api_key.html")
    else:
        missing_crs = [k for k in ("hy_credit_regime", "curve_2s10s_status")
                       if crs_comp.get(k) == "NO_DATA"]
        if missing_crs:
            print(f"[Portfolio] WARNING: CRS partial data — missing: {missing_crs}")

    # Inject EIA crude inventory + rig count into prices dict
    prices["_crude_inv_level_kb"] = crs_data.get("crude_inv_level_kb")
    prices["_crude_inv_chg_kb"]   = crs_data.get("crude_inv_chg_kb")
    prices["_rig_count"]          = crs_data.get("rig_count")

    # Implied Fed Funds cuts: 2Y yield vs Fed Funds upper bound
    dgs2        = crs_data.get("us_2y_yield")
    dfed        = crs_data.get("fed_funds_upper")
    cuts_priced = None  # type: Optional[int]
    if dgs2 is not None and dfed is not None:
        cuts_priced = round((dfed - dgs2) / 0.25)

    is_friday = datetime.utcnow().weekday() == 4

    # Portfolio sizing: read base portfolio value from env
    portfolio_value_eur = None  # type: Optional[float]
    try:
        _pv = float(env.get("PORTFOLIO_VALUE_EUR", "0") or "0")
        if _pv > 0:
            portfolio_value_eur = _pv
    except Exception:
        pass

    print("[Portfolio] Fetching WTI news headlines...")
    wti_headlines = get_wti_news(n_headlines=6)

    print("[Portfolio] Fetching earnings calendar...")
    earnings_upcoming = get_earnings_calendar(days_ahead=14)

    prices_txt = build_prices_section(prices, portfolio_value_eur=portfolio_value_eur)

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
        tga_balance_bn=crs_data.get("tga_balance"),
        rrp_balance_bn=crs_data.get("rrp_balance"),
        cuts_priced_next3=cuts_priced,
        rig_count=crs_data.get("rig_count"),
    ), default=str)

    macro_delta_txt = compute_macro_delta(state, macro, crs_score, crs_regime)
    prior_short = state.get("bias_short", "NEUTRAL")
    prior_long  = state.get("bias_long",  "NEUTRAL")
    prior_macro = state.get("macro_bias", "NEUTRAL")
    prior_crs_s = state.get("crash_risk_score", "N/A")

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

    # Portfolio heat line
    _positions = state.get("open_positions", [])
    _heat_val, _heat_pnl, _heat_stressed = compute_portfolio_heat(_positions, prices)
    _heat_line = ""
    if _positions:
        _heat_parts = [f"€{_heat_val:,.0f}"] if _heat_val > 0 else []
        if _heat_pnl is not None:
            _sign = "+" if _heat_pnl >= 0 else ""
            _heat_parts.append(f"AvgP&L:{_sign}{_heat_pnl}%")
        if _heat_stressed > 0:
            _heat_parts.append(f"⚠️{_heat_stressed} stressed")
        if _heat_parts:
            _heat_line = "Heat  : " + " | ".join(_heat_parts) + "\n"

    # Prior analysis verdicts
    la_verdicts = parse_last_analysis_verdicts(state)
    if la_verdicts:
        _wti_was   = la_verdicts.get("wti_bias", "N/A")
        _spx_was   = la_verdicts.get("spx_bias", "N/A")
        _wti_lvl   = la_verdicts.get("wti_key_level", "N/A")
        _spx_lvl   = la_verdicts.get("spx_key_level", "N/A")
        _macro_v   = la_verdicts.get("macro_verdict", "N/A")
        _dom_risk  = la_verdicts.get("dominant_risk", "N/A")
        prior_analysis_txt = (
            f"WTI bias was: {_wti_was} | Key level: {_wti_lvl}\n"
            f"SPX bias was: {_spx_was} | Key level: {_spx_lvl}\n"
            f"Macro verdict: {_macro_v} | Dominant risk: {_dom_risk}"
        )
    else:
        prior_analysis_txt = "No prior analysis data."

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
        f"{_heat_line}"
        f"------------------------------\n"
        f"SHORT bias:"
    ).rstrip()

    _friday_note = (
        "\n═══ FRIDAY DEEP-DIVE MODE ═══\n"
        "Today is Friday. Inside WTI and SPX, add a\n"
        "WEEKLY LOOKBACK block (max 4 lines):\n"
        "- Main driver this week vs prior base case\n"
        "- Key level hits or misses this week\n"
        "- Weekend event risk (geopolitical, data)\n"
        "- Positioning bias into weekend close\n"
        "Do not shorten the rest of the analysis.\n"
    ) if is_friday else ""

    user_prompt = f"""Today is {today_str}.

═══ ASSET PRICES (fetched this run) ═══
{prices_txt}

═══ MACRO SNAPSHOT ═══
{macro_snapshot}

═══ WTI RECENT HEADLINES ═══
{chr(10).join(wti_headlines) if wti_headlines else "(No headlines fetched.)"}

═══ MEGA-CAP EARNINGS (next 14 days) ═══
{chr(10).join(earnings_upcoming) if earnings_upcoming else "None scheduled in next 14 days."}

═══ MACRO CHANGES SINCE LAST RUN ═══
{macro_delta_txt}

═══ PRIOR BIASES (last run) ═══
Short: {prior_short} | Long: {prior_long} | Macro: {prior_macro} | CRS: {prior_crs_s}/10

═══ PRIOR ANALYSIS VERDICTS (last run) ═══
{prior_analysis_txt}

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
    Conviction: HIGH/MEDIUM/LOW
    Note: one short context line

    SPX SHORT
    Status: APPROACHING
    Range: 7500-7550
    Stop: 7650
    Target: 7200
    Conviction: HIGH/MEDIUM/LOW
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
Conviction: HIGH/MEDIUM/LOW
Note: <one line>
------------------------------

CHANGES TODAY
------------------------------
<one bullet per change>
[/EMAIL]

[STATE_DELTA]
{{Only these Claude-owned fields:
  macro_bias, bias_short, bias_long,
  last_analysis (MUST be a JSON object — not a string — with these exact keys:
    wti_bias, spx_bias, wti_key_level, spx_key_level, dominant_risk, macro_verdict),
  active_setups (each setup must include conviction:HIGH/MEDIUM/LOW and created_at:YYYY-MM-DD),
  open_positions (P&L/action only — no entry_price/qty override), alerted}}
[/STATE_DELTA]
{_friday_note}"""

    # ── Step 4: Call Claude ──
    client = anthropic.Anthropic(api_key=api_key)
    print(f"[{datetime.utcnow().isoformat()}] Calling Claude Sonnet 4.6...")

    message = _call_claude(
        client,
        model="claude-sonnet-4-6",
        max_tokens=10000 if is_friday else 8192,
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
        (tokens_in          *  3.00) +
        (tokens_cache_read  *  0.30) +
        (tokens_cache_write *  3.75) +
        (tokens_out         * 15.00)
    ) / 1_000_000
    print(f"[Portfolio] Tokens: in={tokens_in} cache_read={tokens_cache_read} "
          f"out={tokens_out} cost=${cost_usd:.4f}")

    # ── Step 5: Update state ──
    prior_setups = list(state.get("active_setups", []))
    delta = extract_state_delta(response)
    if delta:
        updated_state = merge_delta(state, delta, prices,
                                    crs_score=crs_score,
                                    crs_regime=crs_regime,
                                    crs_comp=crs_comp,
                                    today_str=today_str)
        save_state(updated_state)
        log_setup_outcomes(prior_setups,
                           updated_state.get("active_setups", []),
                           today_str, BASE_DIR)
        updated_state["last_macro"] = {
            "us_10y":       macro.get("us_10y"),
            "us_30y":       macro.get("us_30y"),
            "usdjpy":       macro.get("usdjpy"),
            "carry_regime": macro.get("carry_regime"),
            "crs_score":    crs_score,
            "crs_regime":   crs_regime,
        }
        save_state(updated_state)
        # Telegram: alert on setups newly moved to ENTER
        _prior_status_map = {
            (s.get("symbol"), s.get("direction", "")): s.get("status")
            for s in prior_setups
        }
        newly_entering = [
            s for s in updated_state.get("active_setups", [])
            if (s.get("status") == "ENTER"
                and _prior_status_map.get(
                    (s.get("symbol"), s.get("direction", "")), "") != "ENTER")
        ]
        if newly_entering:
            send_telegram_alert(env, newly_entering)
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
    high_conv_enters = sum(
        1 for s in updated_state.get("active_setups", [])
        if s.get("status") == "ENTER" and s.get("conviction") == "HIGH"
    )
    subject = (f"🔴 PORTFOLIO ENTRY — {today_str} | {macro_bias} | {enter_count} ENTER"
               if high_conv_enters > 0
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
