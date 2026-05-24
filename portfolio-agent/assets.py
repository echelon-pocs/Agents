"""
Portfolio asset universe — single source of truth.
Add a new ticker here; all other files pick it up automatically.
"""

# Canonical ordered asset list used for display and iteration
PORTFOLIO_ASSETS = ["WTI", "BRENT", "SPX", "VWCE", "VWRL", "4GLD", "8PSB"]

# Yahoo Finance ticker for each asset
YF_SYMBOLS = {
    "WTI":   "CL=F",       # WTI crude oil futures
    "BRENT": "BZ=F",       # Brent crude oil futures
    "SPX":   "^GSPC",      # S&P 500 index
    "VWCE":  "VWCE.DE",    # Vanguard FTSE All-World (acc) - XETRA
    "VWRL":  "VWRL.AS",    # Vanguard FTSE All-World (dist) - Euronext AMS
    "4GLD":  "4GLD.DE",    # Xetra-Gold ETP
    "8PSB":  "8PSB.DE",    # Invesco Physical Silver ETC - XETRA
}

# MEXC perpetual candidates (first working symbol used)
MEXC_SYMBOLS = {
    "WTI":   ["WTI_USDT", "CRUDE_USDT", "OIL_USDT", "USOIL_USDT"],
    "BRENT": ["BRENT_USDT", "UKOIL_USDT", "BRNT_USDT"],
    "SPX":   ["SPX_USDT", "SP500_USDT", "US500_USDT", "SPX500_USDT"],
}

# Telegram routing — symbols routed to portfolio-agent pending_updates.json.
# Includes user-friendly aliases (SPX500, US500, etc.)
PORTFOLIO_ROUTING_SYMBOLS = {
    "VWCE", "VWRL", "4GLD", "8PSB",
    "WTI", "BRENT", "OIL", "CRUDE",
    "SPX", "SPX500", "SP500", "ES", "US500",
}
