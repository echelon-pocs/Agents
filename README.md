# Crypto Market Intelligence Agent

An autonomous Claude Code agent that runs every morning, performs a full crypto market analysis, dynamically discovers and updates trade setups (longs and shorts), and emails you a structured daily report with entry alerts.

## What it does daily

1. **Macro analysis** — BTC trend, dominance, Fear & Greed, Altcoin Season Index, DXY, Gold
2. **Price scan** — BTC, ETH, SOL, XRP, BNB + 15 Tier-2 altcoins
3. **Technical analysis** — MAs, key levels, funding rates, OI, long/short ratios
4. **Setup discovery** — finds new long/short opportunities with R/R ≥ 2:1
5. **Setup review** — updates existing setups, invalidates broken ones, adjusts levels
6. **Email report** — sends full daily report always; fires priority alert if entry zone reached
7. **Self-schedules** — installs its own cron job on first run

## Assets monitored

**Tier 1 (deep analysis):** BTC, ETH, SOL, XRP, BNB

**Tier 2 (opportunity scan):** DOGE, ADA, AVAX, LINK, DOT, MATIC, ATOM, LTC, BCH, UNI, AAVE, OP, ARB, SUI, APT, INJ, TIA, HYPE, TAO

## Setup

```bash
# 1. Install Claude Code
npm install -g @anthropic-ai/claude-code

# 2. Configure credentials
cp .env.example .env
nano .env   # fill in ALERT_EMAIL and SMTP credentials

# 3. First run (also installs cron)
cd crypto-agent-v2
claude --dangerously-skip-permissions -p "Run the crypto market intelligence agent"
```

After the first run, the cron job is installed and runs automatically at 08:00 UTC daily.

## Files

| File | Description |
|------|-------------|
| `CLAUDE.md` | Agent instructions — edit this to change behaviour |
| `state.json` | Live state: active setups, alerted list, last analysis |
| `report.log` | One-line summary per run |
| `cron.log` | Output from cron-triggered runs |
| `.env` | Your credentials |

## Customising

**Change which assets to scan:** Edit the Tier 1 / Tier 2 lists in `CLAUDE.md`

**Change report time:** Edit the cron expression (default `0 8 * * *` = 08:00 UTC)

**Change alert thresholds:** Edit the APPROACHING / ENTER criteria in `CLAUDE.md`

**Pre-load setups:** Edit the `active_setups` array in `state.json`

**Stop the agent:**
```bash
crontab -e  # delete the crypto-agent line
```

## Email types

| Email | When sent |
|-------|-----------|
| `📊 Daily Report` | Every morning regardless |
| `🔴 ENTRY ALERT + Daily Report` | When a setup reaches entry zone |
