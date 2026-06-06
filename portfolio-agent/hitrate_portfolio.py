#!/usr/bin/env python3
"""
Portfolio Setup Hit-Rate Reporter
Reads setups_log.jsonl and prints win/loss/pending stats
by symbol, direction, and conviction level.

Usage:
    python3 hitrate_portfolio.py [--json]
"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional


BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "setups_log.jsonl"


def load_log():
    # type: () -> List[dict]
    if not LOG_PATH.exists():
        return []
    records = []
    with open(LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    return records


def _bucket_key(record, group_by):
    # type: (dict, str) -> str
    if group_by == "symbol":
        return record.get("symbol", "UNKNOWN")
    if group_by == "direction":
        sym  = record.get("symbol", "?")
        dirn = record.get("direction", "?")
        return f"{sym} {dirn}"
    if group_by == "conviction":
        return (record.get("conviction") or "UNKNOWN").upper()
    return "ALL"


def compute_stats(records, group_by="symbol"):
    # type: (List[dict], str) -> Dict[str, dict]
    """
    Aggregate outcomes by group_by key.
    Outcomes:
      REMOVED  — setup was present one run, gone the next (presumed expired or manually removed)
      INVALIDATED — setup explicitly hit its stop / invalidation condition
    We treat REMOVED as a neutral exit (pending / no data), not a win or loss,
    unless prior_status was ENTER (then it's a presumed closed trade — counted as pending).
    """
    buckets = {}  # type: Dict[str, dict]
    for r in records:
        key = _bucket_key(r, group_by)
        if key not in buckets:
            buckets[key] = {"win": 0, "loss": 0, "pending": 0, "total": 0, "records": []}
        b = buckets[key]
        b["total"] += 1
        b["records"].append(r)
        outcome     = r.get("outcome", "")
        prior_status = r.get("prior_status", "")
        if outcome == "INVALIDATED":
            b["loss"] += 1
        elif outcome == "REMOVED" and prior_status == "ENTER":
            # Was actively in the entry zone when removed — treat as pending close
            b["pending"] += 1
        else:
            # REMOVED while WAITING/APPROACHING — just expired, not a trade
            b["pending"] += 1
    return buckets


def hitrate(b):
    # type: (dict) -> Optional[float]
    decided = b["win"] + b["loss"]
    if decided == 0:
        return None
    return round(b["win"] / decided * 100, 1)


def print_report(records, as_json=False):
    # type: (List[dict], bool) -> None
    if not records:
        print("No setup log records found.")
        print(f"Expected log file: {LOG_PATH}")
        return

    total = len(records)
    date_range = ""
    dates = sorted(r.get("date", "") for r in records if r.get("date"))
    if dates:
        date_range = f"{dates[0]} to {dates[-1]}"

    by_sym   = compute_stats(records, "symbol")
    by_dir   = compute_stats(records, "direction")
    by_conv  = compute_stats(records, "conviction")
    overall  = compute_stats(records, "all")["ALL"]

    if as_json:
        out = {
            "total_records": total,
            "date_range":    date_range,
            "overall":       {
                "win":     overall["win"],
                "loss":    overall["loss"],
                "pending": overall["pending"],
                "hitrate": hitrate(overall),
            },
            "by_symbol":     {k: {"win": v["win"], "loss": v["loss"],
                                  "pending": v["pending"], "hitrate": hitrate(v)}
                              for k, v in sorted(by_sym.items())},
            "by_conviction": {k: {"win": v["win"], "loss": v["loss"],
                                  "pending": v["pending"], "hitrate": hitrate(v)}
                              for k, v in sorted(by_conv.items())},
        }
        print(json.dumps(out, indent=2))
        return

    print("=" * 50)
    print("PORTFOLIO SETUP HIT-RATE REPORT")
    print(f"Log   : {LOG_PATH}")
    if date_range:
        print(f"Period: {date_range}")
    print(f"Total records: {total}")
    print("=" * 50)

    hr = hitrate(overall)
    hr_str = f"{hr}%" if hr is not None else "N/A (no decided trades)"
    print(f"\nOVERALL")
    print(f"  Win: {overall['win']}  Loss: {overall['loss']}  "
          f"Pending/expired: {overall['pending']}")
    print(f"  Hit rate: {hr_str}")

    print("\nBY SYMBOL")
    for sym, b in sorted(by_sym.items()):
        hr = hitrate(b)
        hr_s = f"{hr}%" if hr is not None else "N/A"
        print(f"  {sym:<10} W:{b['win']} L:{b['loss']} P:{b['pending']} "
              f"=> hitrate:{hr_s}")

    print("\nBY SYMBOL + DIRECTION")
    for key, b in sorted(by_dir.items()):
        hr = hitrate(b)
        hr_s = f"{hr}%" if hr is not None else "N/A"
        print(f"  {key:<16} W:{b['win']} L:{b['loss']} P:{b['pending']} "
              f"=> hitrate:{hr_s}")

    print("\nBY CONVICTION")
    for conv, b in sorted(by_conv.items()):
        hr = hitrate(b)
        hr_s = f"{hr}%" if hr is not None else "N/A"
        print(f"  {conv:<10} W:{b['win']} L:{b['loss']} P:{b['pending']} "
              f"=> hitrate:{hr_s}")

    # Recent invalidations / losses
    losses = [r for r in records if r.get("outcome") == "INVALIDATED"]
    if losses:
        print(f"\nRECENT INVALIDATIONS (last 5)")
        for r in losses[-5:]:
            print(f"  {r.get('date','')} {r.get('symbol','')} "
                  f"{r.get('direction','')} conv:{r.get('conviction','?')} "
                  f"stop:{r.get('stop','?')}")

    print("=" * 50)


if __name__ == "__main__":
    as_json = "--json" in sys.argv
    records = load_log()
    print_report(records, as_json=as_json)
