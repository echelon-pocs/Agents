#!/usr/bin/env python3
"""
Setup hit-rate analysis for the crypto agent.

Reads setups_history.jsonl (written by log_setup_snapshot on each run) and
calculates what percentage of ENTER-triggered setups reached T1/T2 (COMPLETED)
vs. stopped out or reversed (INVALIDATED).

Usage:
  python3 hitrate.py                      # last 90 days
  python3 hitrate.py --since 2026-01-01   # custom start date
  python3 hitrate.py --symbol BTC         # single symbol
  python3 hitrate.py --all                # all-time
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR     = Path(__file__).parent
HISTORY_FILE = BASE_DIR / "setups_history.jsonl"


def load_records(since=None, symbol_filter=None):
    if not HISTORY_FILE.exists():
        print(f"⚠️  {HISTORY_FILE} not found. Run the crypto agent once to generate it.")
        return []
    records = []
    with open(HISTORY_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if since and r.get("date", "") < since:
                    continue
                if symbol_filter and r.get("symbol", "").upper() != symbol_filter.upper():
                    continue
                records.append(r)
            except json.JSONDecodeError:
                continue
    return records


def analyze(records):
    """
    Group records by (symbol, direction) and determine the outcome of each
    setup that was ENTER-triggered. Returns a list of result dicts.
    """
    groups = defaultdict(list)
    for r in records:
        key = (r.get("symbol", "?"), r.get("direction", "?"))
        groups[key].append(r)

    results = []
    for (sym, dirn), recs in sorted(groups.items()):
        recs.sort(key=lambda r: r.get("date", ""))
        statuses = [r.get("status") for r in recs]

        # Only count setups that were triggered (reached ENTER)
        if "ENTER" not in statuses:
            continue

        # Entry date: first record where status == ENTER
        entry_date = next(
            (r["date"] for r in recs if r.get("status") == "ENTER"),
            recs[0]["date"],
        )
        last = recs[-1]
        final = last.get("status")

        if final == "COMPLETED":
            outcome = "HIT"
        elif final == "INVALIDATED":
            outcome = "MISS"
        else:
            outcome = "PENDING"

        results.append({
            "symbol":      sym,
            "direction":   dirn,
            "entry_date":  entry_date,
            "final":       final,
            "outcome":     outcome,
            "conviction":  last.get("conviction", "?"),
            "timeframe":   last.get("timeframe", "?"),
            "r_r":         last.get("r_r_ratio"),
            "days_active": len(set(r["date"] for r in recs)),
        })

    return results


def print_report(results, since=None, symbol_filter=None):
    if not results:
        qualifier = f" for {symbol_filter}" if symbol_filter else ""
        print(f"\nNo ENTER-triggered setups found{qualifier} in the given period.")
        return

    hits    = [r for r in results if r["outcome"] == "HIT"]
    misses  = [r for r in results if r["outcome"] == "MISS"]
    pending = [r for r in results if r["outcome"] == "PENDING"]
    closed  = len(hits) + len(misses)
    rate    = f"{len(hits)/closed*100:.0f}%" if closed else "N/A"

    title = "Setup Hit-Rate Report"
    if since:
        title += f"  (since {since})"
    if symbol_filter:
        title += f"  [{symbol_filter}]"

    print(f"\n{'═'*56}")
    print(f"  {title}")
    print(f"{'═'*56}")
    print(f"  Triggered: {len(results):3d}  |  Closed: {closed:3d}  |  Pending: {len(pending):3d}")
    print(f"  Hit rate:  {len(hits)}/{closed} = {rate}")
    print(f"{'─'*56}")

    # Break down by conviction
    for conv in ("HIGH", "MEDIUM", "LOW"):
        c = [r for r in results if r["conviction"] == conv]
        c_closed = [r for r in c if r["outcome"] in ("HIT", "MISS")]
        c_hits   = [r for r in c if r["outcome"] == "HIT"]
        if c_closed:
            c_rate = f"{len(c_hits)/len(c_closed)*100:.0f}%"
            print(f"  {conv:6} conviction: {len(c_hits):3d}/{len(c_closed):3d} = {c_rate}")

    # Break down by timeframe
    print()
    for tf in ("SHORT_TERM", "MEDIUM_TERM", "LONG_TERM"):
        t = [r for r in results if r["timeframe"] == tf]
        t_closed = [r for r in t if r["outcome"] in ("HIT", "MISS")]
        t_hits   = [r for r in t if r["outcome"] == "HIT"]
        if t_closed:
            t_rate = f"{len(t_hits)/len(t_closed)*100:.0f}%"
            print(f"  {tf:12}: {len(t_hits):3d}/{len(t_closed):3d} = {t_rate}")

    # Detail table
    print(f"\n  {'Symbol':<8} {'Dir':<6} {'Entry':<11} {'Conv':<7} {'R:R':<5} {'Days':<5} Result")
    print(f"  {'─'*7} {'─'*5} {'─'*10} {'─'*6} {'─'*4} {'─'*4} {'─'*12}")
    for r in sorted(results, key=lambda x: x["entry_date"], reverse=True):
        icon = "✅ HIT" if r["outcome"] == "HIT" else "❌ MISS" if r["outcome"] == "MISS" else "⏳ PENDING"
        r_r  = f"{r['r_r']:.1f}" if r["r_r"] else "N/A"
        print(f"  {r['symbol']:<8} {r['direction']:<6} {r['entry_date']:<11} "
              f"{r['conviction']:<7} {r_r:<5} {r['days_active']:<5} {icon}")
    print()


def main():
    since         = None
    symbol_filter = None
    all_time      = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--since" and i + 1 < len(args):
            since = args[i + 1]
            i += 2
        elif arg == "--symbol" and i + 1 < len(args):
            symbol_filter = args[i + 1].upper()
            i += 2
        elif arg == "--all":
            all_time = True
            i += 1
        else:
            print(f"Unknown argument: {arg}")
            print(__doc__)
            sys.exit(1)

    if not all_time and since is None:
        since = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")

    records = load_records(since=since, symbol_filter=symbol_filter)
    results = analyze(records)
    print_report(results, since=since, symbol_filter=symbol_filter)


if __name__ == "__main__":
    main()
