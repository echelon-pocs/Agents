#!/usr/bin/env python3
"""
Telegram position update listener for the Crypto Agent.

Runs via cron every 5 minutes. Reads messages from your private Telegram bot,
parses position commands, and writes them to pending_updates.json for the
main agent to process on next run.

Commands (send to your bot on Telegram):
  /enter BTC 103000          — entered BTC at $103,000 (size optional)
  /enter ETH 2450 500usd     — entered ETH at $2,450 with $500 size
  /enter SOL 165 2.5         — entered SOL at $165, 2.5 coins
  /close ETH                 — closed ETH position
  /close BTC partial         — partially closed BTC
  /note ETH trail stop 2300  — add a note/action to open position
  /status                    — bot replies with current open positions
  /help                      — show command list

Setup:
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Start your bot (message it once so Telegram knows your chat_id)
  3. Add to .env:
       TELEGRAM_BOT_TOKEN=123456:ABC-your-token
       TELEGRAM_CHAT_ID=your_chat_id   (optional — auto-detected on first message)
  4. Schedule this script via cron every 5 min:
       */5 * * * * python3 /home/user/Agents/telegram_bot.py >> /home/user/Agents/telegram.log 2>&1
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
PENDING_FILE = BASE_DIR / "pending_updates.json"
OFFSET_FILE  = BASE_DIR / ".tg_offset"
ENV_FILE     = BASE_DIR / ".env"


# ─── Config ─────────────────────────────────────────────────────────────────

def load_env() -> dict:
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def save_env_value(key: str, value: str):
    """Append or update a key in .env (used to persist chat_id on first use)."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    new_lines = [l for l in lines if not l.startswith(f"{key}=")]
    new_lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n")


# ─── Telegram API helpers ────────────────────────────────────────────────────

def tg(token: str, method: str, **params) -> dict:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=params, timeout=15,
        )
        return r.json()
    except Exception as e:
        print(f"[TG] API error: {e}")
        return {}


def send(token: str, chat_id: str, text: str):
    tg(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="Markdown")


# ─── Pending updates file ────────────────────────────────────────────────────

def load_pending() -> list:
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except Exception:
            pass
    return []


def save_pending(updates: list):
    PENDING_FILE.write_text(json.dumps(updates, indent=2))


def load_state() -> dict:
    p = BASE_DIR / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


# ─── Command parser ───────────────────────────────────────────────────────

def parse_command(text: str) -> dict | None:
    """
    Parse a Telegram message into a structured update dict.
    Returns None if not a recognised command.
    """
    text = text.strip()
    if not text.startswith("/"):
        return None

    parts = text.lstrip("/").split()
    if not parts:
        return None

    cmd = parts[0].lower()

    # /enter SYM PRICE [SIZE[usd]]
    if cmd == "enter" and len(parts) >= 3:
        symbol = parts[1].upper()
        try:
            price = float(parts[2].replace(",", "").replace("$", ""))
        except ValueError:
            return {"error": f"Invalid price: {parts[2]}"}

        size_usd = None
        size_qty = None
        if len(parts) >= 4:
            raw = parts[3].lower().replace(",", "")
            if raw.endswith("usd"):
                try:
                    size_usd = float(raw[:-3])
                except ValueError:
                    pass
            else:
                try:
                    size_qty = float(raw)
                    size_usd = size_qty * price
                except ValueError:
                    pass

        return {
            "action":    "ENTER",
            "symbol":    symbol,
            "price":     price,
            "size_usd":  size_usd,
            "size_qty":  size_qty,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # /close SYM [partial|full]
    if cmd == "close" and len(parts) >= 2:
        symbol = parts[1].upper()
        partial = len(parts) >= 3 and "partial" in parts[2].lower()
        return {
            "action":    "CLOSE",
            "symbol":    symbol,
            "partial":   partial,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # /note SYM free text...
    if cmd == "note" and len(parts) >= 3:
        symbol = parts[1].upper()
        note   = " ".join(parts[2:])
        return {
            "action":    "NOTE",
            "symbol":    symbol,
            "note":      note,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    if cmd in ("status", "help"):
        return {"action": cmd.upper()}

    return {"error": f"Unknown command: /{cmd}"}


def format_status(state: dict) -> str:
    positions = state.get("open_positions", [])
    setups    = state.get("active_setups", [])
    pending   = load_pending()

    lines = ["*Crypto Agent Status*\n"]

    if positions:
        lines.append("*Open Positions:*")
        for p in positions:
            pnl = p.get("pnl_pct")
            pnl_str = f" | P&L {pnl:+.1f}%" if pnl is not None else ""
            lines.append(
                f"  {p['symbol']} {p.get('direction','')} "
                f"@ ${p.get('entry_price', '?'):,}{pnl_str}"
            )
    else:
        lines.append("*Open Positions:* None confirmed")

    enter_setups = [s for s in setups if s.get("status") == "ENTER"]
    if enter_setups:
        lines.append(f"\n*ENTER Alerts ({len(enter_setups)}):*")
        for s in enter_setups:
            lines.append(f"  {s['symbol']} {s.get('direction','')} — {s.get('conviction','')} conviction")

    approaching = [s for s in setups if s.get("status") == "APPROACHING"]
    if approaching:
        lines.append(f"\n*Approaching ({len(approaching)}):* " +
                     ", ".join(s["symbol"] for s in approaching))

    if pending:
        lines.append(f"\n*Pending updates ({len(pending)}):*")
        for u in pending:
            lines.append(f"  {u.get('action')} {u.get('symbol','')} "
                         f"@ ${u.get('price',''):,}" if u.get('price') else
                         f"  {u.get('action')} {u.get('symbol','')}")

    last = state.get("last_run", "never")
    lines.append(f"\n_Last run: {last}_")
    return "\n".join(lines)


HELP_TEXT = """*Crypto Agent — Commands*

`/enter BTC 103000` — entered BTC at $103,000
`/enter ETH 2450 500usd` — entered with $500 size
`/enter SOL 165 2.5` — entered 2.5 SOL at $165
`/close ETH` — closed ETH position fully
`/close BTC partial` — partially closed BTC
`/note ETH trailing stop to $2300` — add action note
`/status` — show open positions & active setups
`/help` — this message

Updates are queued and applied on the next daily run."""


# ─── Main polling loop ─────────────────────────────────────────────────────

def run():
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("[TG] ERROR: TELEGRAM_BOT_TOKEN not in .env — add it and retry.")
        sys.exit(1)

    chat_id = env.get("TELEGRAM_CHAT_ID", "")

    # Load last processed update offset
    offset = 0
    if OFFSET_FILE.exists():
        try:
            offset = int(OFFSET_FILE.read_text().strip())
        except ValueError:
            pass

    updates_resp = tg(token, "getUpdates", offset=offset, timeout=10)
    updates = updates_resp.get("result", [])

    if not updates:
        print(f"[TG] No new messages (offset={offset})")
        return

    pending = load_pending()
    new_offset = offset

    for upd in updates:
        new_offset = max(new_offset, upd["update_id"] + 1)
        msg = upd.get("message", {})
        text = msg.get("text", "")
        from_id = str(msg.get("chat", {}).get("id", ""))

        if not text or not from_id:
            continue

        # Auto-register chat_id on first message
        if not chat_id:
            chat_id = from_id
            save_env_value("TELEGRAM_CHAT_ID", chat_id)
            print(f"[TG] Registered chat_id: {chat_id}")

        # Only accept messages from the registered chat
        if from_id != chat_id:
            send(token, from_id, "⛔ Unauthorised.")
            continue

        print(f"[TG] Message: {text!r}")
        parsed = parse_command(text)

        if parsed is None:
            send(token, chat_id,
                 "Not a command. Send /help to see available commands.")
            continue

        if "error" in parsed:
            send(token, chat_id, f"⚠️ {parsed['error']}")
            continue

        action = parsed.get("action")

        if action == "HELP":
            send(token, chat_id, HELP_TEXT)
            continue

        if action == "STATUS":
            state = load_state()
            send(token, chat_id, format_status(state))
            continue

        # Queue the update
        pending.append(parsed)
        save_pending(pending)

        if action == "ENTER":
            size_note = (f", size ${parsed['size_usd']:,.0f}"
                         if parsed.get("size_usd") else "")
            send(token, chat_id,
                 f"✅ Queued: *ENTER {parsed['symbol']}* @ "
                 f"${parsed['price']:,.2f}{size_note}\n"
                 f"_Will be applied on next daily run._")

        elif action == "CLOSE":
            kind = "partial close" if parsed.get("partial") else "close"
            send(token, chat_id,
                 f"✅ Queued: *{kind.upper()} {parsed['symbol']}*\n"
                 f"_Will be applied on next daily run._")

        elif action == "NOTE":
            send(token, chat_id,
                 f"✅ Queued note for *{parsed['symbol']}*: _{parsed['note']}_")

    OFFSET_FILE.write_text(str(new_offset))
    print(f"[TG] Processed {len(updates)} update(s), new offset={new_offset}, "
          f"pending queue={len(pending)}")


if __name__ == "__main__":
    run()
