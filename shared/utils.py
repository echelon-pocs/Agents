"""Shared utilities for all agents in this repo."""
from pathlib import Path


# Browser-like headers used for scraping financial data endpoints
CHROME_HDR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def load_env(*paths):
    """
    Parse one or more .env files in order.
    First file wins — later files do NOT override earlier values (setdefault).
    """
    cfg = {}
    for path in paths:
        p = Path(path)
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg.setdefault(k.strip(), v.strip())
    return cfg


def _fmt(v, decimals=2):
    if v is None:
        return "N/A"
    return f"{v:.{decimals}f}"


def avg_into_position(pos, new_price, new_qty=None, new_size_usd=None):
    """
    Merge a new entry into an existing position, updating entry_price as a
    weighted average and qty as the new total.  Mutates and returns pos.

    Weight priority: explicit new_qty > derived from new_size_usd > equal-weight.
    """
    old_entry = float(pos.get("entry_price") or new_price)
    old_qty   = pos.get("qty")
    old_size  = pos.get("size_usd")

    # Derive new_qty from USD size when not directly known
    if new_qty is None and new_size_usd is not None and new_price:
        new_qty = new_size_usd / new_price
    # Derive old_qty from old USD size when not stored as units
    if old_qty is None and old_size is not None and old_entry:
        old_qty = old_size / old_entry

    if new_qty is not None and old_qty is not None and old_qty > 0:
        total_qty         = old_qty + new_qty
        avg               = (old_entry * old_qty + new_price * new_qty) / total_qty
        pos["qty"]        = round(total_qty, 8)
    elif new_qty is not None:
        pos["qty"]        = round(new_qty, 8)
        avg               = new_price
    else:
        # No qty on either side — equal-weight running average
        count             = int(pos.get("_entry_count", 1))
        avg               = (old_entry * count + new_price) / (count + 1)
        pos["_entry_count"] = count + 1

    pos["entry_price"] = round(avg, 8)

    # Accumulate USD size
    if new_size_usd is not None:
        pos["size_usd"] = round((pos.get("size_usd") or 0.0) + new_size_usd, 2)
    elif new_qty is not None and new_price:
        total_q = pos.get("qty") or new_qty
        pos["size_usd"] = round(total_q * pos["entry_price"], 2)

    return pos


def reduce_position(pos, close_qty=None, close_pct=None, close_usd=None):
    """
    Reduce a position for a partial close.  entry_price (average cost) is
    unchanged.  Returns the mutated pos, or None when the position is fully
    closed.
    """
    old_qty   = pos.get("qty")
    old_entry = float(pos.get("entry_price") or 1)

    if close_pct is not None:
        if old_qty is not None:
            closed_qty = old_qty * close_pct / 100.0
        else:
            pos["notes"] = ((pos.get("notes") or "") +
                            f" | Partial close {close_pct:.0f}% flagged.")
            return pos
    elif close_qty is not None:
        closed_qty = float(close_qty)
    elif close_usd is not None and old_entry:
        closed_qty = close_usd / old_entry
    else:
        pos["notes"] = (pos.get("notes") or "") + " | Partial close flagged."
        return pos

    if old_qty is not None:
        remaining = old_qty - closed_qty
        if remaining <= 1e-8:
            return None  # fully consumed
        pos["qty"]      = round(remaining, 8)
        pos["size_usd"] = round(remaining * old_entry, 2)

    return pos


def sanitize_state(state):
    """Normalize state dict structure. Call only in load_state() and save_state()."""
    if not isinstance(state, dict):
        state = {}
    for key in ("open_positions", "active_setups"):
        raw = state.get(key, [])
        if not isinstance(raw, list):
            raw = []
        state[key] = [e for e in raw if isinstance(e, dict) and e.get("symbol")]
    for key in ("alerted", "profitable_wallets_discovered"):
        if not isinstance(state.get(key), list):
            state[key] = []
    return state
