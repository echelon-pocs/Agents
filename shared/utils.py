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
