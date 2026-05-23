#!/usr/bin/env python3
"""
Telegram position update listener for the Crypto Agent.

Runs via cron every 5 minutes. Reads messages from your private Telegram bot,
parses position commands, and writes them to pending_updates.json for the
main agent to process on next run.

Commands (send to your bot on Telegram):
  /enter BTC 103000              — entered BTC spot (long) at $103,000
  /enter BTC long 103000         — same, direction explicit
  /enter BTC short 103000        — entered BTC short (futures) at $103,000
  /enter ETH long 2450 500usd    — entered ETH long with $500 size
  /enter SOL short 165 2.5       — entered SOL short, 2.5 coins at $165
  /close ETH                     — closed ETH position
  /close BTC short               — closed BTC short leg (futures)
  /close BTC partial             — partial close
  /note ETH trail stop 2300      — add a note/action to open position
  /status                        — bot replies with current open positions
  /help                          — show command list

  Send a photo/screenshot        — Claude vision parses the exchange position
                                   automatically (symbol, direction, entry, stop)

Setup:
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Start your bot (message it once so Telegram knows your chat_id)
  3. Add to .env:
       TELEGRAM_BOT_TOKEN=123456:ABC-your-token
       TELEGRAM_CHAT_ID=your_chat_id   (optional — auto-detected on first message)
  4. Schedule this script via cron every 5 min (adjust path to your Agents dir):
       */5 * * * * python3 /volume1/homes/admin/Agents/telegram_bot.py >> /volume1/homes/admin/Agents/telegram.log 2>&1
"""

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
PENDING_FILE = BASE_DIR / "pending_updates.json"
OFFSET_FILE  = BASE_DIR / ".tg_offset"
ENV_FILE     = BASE_DIR / ".env"


# ─── Config ──────────────────────────────────────────────────────────────────

def load_env():
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def save_env_value(key, value):
    """Append or update a key in .env (used to persist chat_id on first use)."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    new_lines = [l for l in lines if not l.startswith(f"{key}=")]
    new_lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n")


# ─── State sanitization (mirrors run_agent_haiku.py) ─────────────────────────

def sanitize_state(state):
    """
    Normalize state to a predictable structure regardless of what Claude wrote
    or what legacy format was on disk. Called after every load.
    """
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


# ─── Telegram API helpers ─────────────────────────────────────────────────────

def tg(token, method, **params):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=params, timeout=15,
        )
        return r.json()
    except Exception as e:
        print(f"[TG] API error: {e}")
        return {}


def send(token, chat_id, text):
    tg(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="Markdown")


# ─── Pending updates file ─────────────────────────────────────────────────────

def load_pending():
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except Exception:
            pass
    return []


def save_pending(updates):
    PENDING_FILE.write_text(json.dumps(updates, indent=2))


def load_state():
    p = BASE_DIR / "state.json"
    if p.exists():
        try:
            return sanitize_state(json.loads(p.read_text()))
        except Exception as e:
            print(f"[TG] WARNING: could not load state.json ({e}) — using empty state")
    return sanitize_state({})


# ─── Command parser ───────────────────────────────────────────────────────────

def parse_command(text):
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

    # /enter SYM [long|short|spot|buy|sell] PRICE [SIZE[usd]]
    # Direction keyword is optional; default is LONG/spot.
    # Examples:
    #   /enter BTC 103000
    #   /enter BTC long 103000
    #   /enter BTC short 103000 500usd
    #   /enter ETH spot 2450 1.5
    DIRECTION_MAP = {
        "long":    ("LONG",  "spot"),
        "buy":     ("LONG",  "spot"),
        "spot":    ("LONG",  "spot"),
        "short":   ("SHORT", "futures"),
        "sell":    ("SHORT", "futures"),
        "futures": ("LONG",  "futures"),
    }
    if cmd == "enter" and len(parts) >= 3:
        symbol = parts[1].upper()

        # Detect optional direction keyword at parts[2]
        direction   = "LONG"
        market_type = "spot"
        price_idx   = 2
        if parts[2].lower() in DIRECTION_MAP:
            direction, market_type = DIRECTION_MAP[parts[2].lower()]
            price_idx = 3

        if len(parts) <= price_idx:
            return {"error": "Missing price. Usage: /enter SYM [long|short] PRICE [SIZE]"}

        try:
            price = float(parts[price_idx].replace(",", "").replace("$", ""))
        except ValueError:
            return {"error": f"Invalid price: {parts[price_idx]}"}

        size_usd = None
        size_qty = None
        size_idx = price_idx + 1
        if len(parts) > size_idx:
            raw = parts[size_idx].lower().replace(",", "")
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
            "action":      "ENTER",
            "symbol":      symbol,
            "direction":   direction,
            "market_type": market_type,
            "price":       price,
            "size_usd":    size_usd,
            "size_qty":    size_qty,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }

    # /close SYM [long|short|spot|partial|full]
    # Examples:
    #   /close ETH
    #   /close BTC short
    #   /close BTC partial
    if cmd == "close" and len(parts) >= 2:
        symbol = parts[1].upper()
        modifiers = [p.lower() for p in parts[2:]]
        partial = any(m in ("partial",) for m in modifiers)
        # Direction hint (helps agent identify which leg to close in futures)
        direction = None
        for m in modifiers:
            if m in ("long", "buy", "spot"):
                direction = "LONG"
                break
            if m in ("short", "sell"):
                direction = "SHORT"
                break
        result = {
            "action":    "CLOSE",
            "symbol":    symbol,
            "partial":   partial,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if direction:
            result["direction"] = direction
        return result

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


def _fmt_price(price):
    """Format a price value safely — returns a string regardless of input type."""
    try:
        return f"${float(price):,.2f}"
    except (TypeError, ValueError):
        return str(price) if price is not None else "?"


def _fmt_pnl(pnl):
    """Format a P&L value safely — returns empty string if not a number."""
    try:
        return f" | P&L {float(pnl):+.1f}%"
    except (TypeError, ValueError):
        return ""


def format_status(state):
    try:
        positions = state.get("open_positions", [])
        setups    = state.get("active_setups", [])
        pending   = load_pending()

        lines = ["*Crypto Agent Status*\n"]

        if positions:
            lines.append("*Open Positions:*")
            for p in positions:
                if not isinstance(p, dict):
                    continue
                pnl_str = _fmt_pnl(p.get("pnl_pct"))
                lines.append(
                    f"  {p.get('symbol', '?')} {p.get('direction', '')} "
                    f"@ {_fmt_price(p.get('entry_price'))}{pnl_str}"
                )
        else:
            lines.append("*Open Positions:* None confirmed")

        enter_setups = [s for s in setups if isinstance(s, dict) and s.get("status") == "ENTER"]
        if enter_setups:
            lines.append(f"\n*ENTER Alerts ({len(enter_setups)}):*")
            for s in enter_setups:
                lines.append(
                    f"  {s.get('symbol', '?')} {s.get('direction', '')} "
                    f"— {s.get('conviction', '')} conviction"
                )

        approaching = [s for s in setups if isinstance(s, dict) and s.get("status") == "APPROACHING"]
        if approaching:
            lines.append(
                f"\n*Approaching ({len(approaching)}):* " +
                ", ".join(s.get("symbol", "?") for s in approaching)
            )

        if pending:
            lines.append(f"\n*Pending updates ({len(pending)}):*")
            for u in pending:
                if not isinstance(u, dict):
                    continue
                sym = u.get("symbol", "")
                action = u.get("action", "?")
                price = u.get("price")
                if price is not None:
                    lines.append(f"  {action} {sym} @ {_fmt_price(price)}")
                else:
                    lines.append(f"  {action} {sym}")

        last = state.get("last_run", "never")
        lines.append(f"\n_Last run: {last}_")
        return "\n".join(lines)

    except Exception as e:
        print(f"[TG] ERROR in format_status: {e}")
        return f"⚠️ Could not render status: {e}"


HELP_TEXT = """*Crypto Agent — Commands*

*Enter a position:*
`/enter BTC 103000` — spot long at $103,000
`/enter BTC long 103000` — spot long (explicit)
`/enter BTC short 103000` — futures short at $103,000
`/enter ETH long 2450 500usd` — long with $500 size
`/enter SOL short 165 2.5` — short 2.5 SOL at $165

*Close a position:*
`/close ETH` — close ETH (all)
`/close BTC short` — close BTC short leg
`/close BTC partial` — flag partial close

*Other:*
`/note ETH trailing stop to $2300` — add note to position
`/status` — show open positions & active setups
`/help` — this message

Updates are queued and applied on the next daily run."""


# ─── Vision: parse exchange position screenshots ─────────────────────────────

def _detect_media_type(data: bytes) -> str:
    if data[:4] == b'\x89PNG':
        return "image/png"
    if data[:2] == b'\xff\xd8':
        return "image/jpeg"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    return "image/jpeg"  # safe default for Telegram photos


def download_tg_file(token: str, file_id: str) -> bytes | None:
    """Download a Telegram file by file_id, return raw bytes or None."""
    try:
        meta = tg(token, "getFile", file_id=file_id)
        file_path = meta.get("result", {}).get("file_path")
        if not file_path:
            return None
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        r = requests.get(url, timeout=20)
        return r.content if r.status_code == 200 else None
    except Exception as e:
        print(f"[TG] File download error: {e}")
        return None


def parse_position_image(image_bytes: bytes, api_key: str) -> dict | None:
    """Use Claude Haiku vision to extract position data from an exchange screenshot."""
    try:
        import anthropic
    except ImportError:
        print("[TG] anthropic package not available — cannot parse image")
        return None

    media_type = _detect_media_type(image_bytes)
    b64 = base64.standard_b64encode(image_bytes).decode()

    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "This is a trading exchange position screenshot. "
                            "Extract the position and return ONLY a JSON object:\n"
                            '{"symbol":"BTC","direction":"short","entry_price":103250.0,'
                            '"current_price":103000.0,"stop_loss":null,"size_usd":null,'
                            '"market_type":"perpetual","pnl_pct":0.24,"exchange":"Binance"}\n'
                            "Rules:\n"
                            "- symbol: coin ticker only (no USDT suffix)\n"
                            "- direction: 'long' or 'short'\n"
                            "- null for any field not visible\n"
                            "- market_type: 'spot', 'futures', or 'perpetual'\n"
                            "If this is not a position screenshot return: "
                            '{"error":"not a position"}\n'
                            "Return ONLY the JSON, no other text."
                        ),
                    },
                ],
            }],
        )
        text = resp.content[0].text.strip()
        # Strip markdown code fences if present
        text = text.strip('`').strip()
        if text.startswith('json'):
            text = text[4:].strip()
        return json.loads(text)
    except json.JSONDecodeError:
        import re
        m = re.search(r'\{.*\}', resp.content[0].text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return None
    except Exception as e:
        print(f"[TG] Vision parse error: {e}")
        return None


def _position_from_vision(parsed: dict) -> dict | None:
    """Convert Claude vision output to a pending-update dict (ENTER action)."""
    if not parsed or "error" in parsed:
        return None
    sym = parsed.get("symbol", "").upper()
    if not sym:
        return None
    direction  = (parsed.get("direction") or "long").upper()
    price      = parsed.get("entry_price") or parsed.get("current_price")
    stop       = parsed.get("stop_loss")
    size_usd   = parsed.get("size_usd")
    mtype      = parsed.get("market_type", "perpetual")

    update = {
        "action":      "ENTER",
        "symbol":      sym,
        "direction":   direction,
        "market_type": mtype,
        "source":      "image",
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }
    if price is not None:
        update["price"] = float(price)
    if stop is not None:
        update["stop_loss"] = float(stop)
    if size_usd is not None:
        update["size_usd"] = float(size_usd)
    return update


# ─── Main polling loop ────────────────────────────────────────────────────────

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

    api_key = env.get("ANTHROPIC_API_KEY", "")

    for upd in updates:
        try:
            new_offset = max(new_offset, upd.get("update_id", new_offset - 1) + 1)
            msg = upd.get("message", {})
            if not isinstance(msg, dict):
                continue

            text    = msg.get("text", "") or msg.get("caption", "")
            from_id = str(msg.get("chat", {}).get("id", ""))
            photos  = msg.get("photo", [])
            doc     = msg.get("document", {})

            has_image = bool(photos) or bool(doc and (doc.get("mime_type", "").startswith("image/")))

            if not from_id or (not text and not has_image):
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

            # ── Image message: parse position screenshot with Claude vision ──
            if has_image and not text:
                if not api_key:
                    send(token, chat_id,
                         "⚠️ ANTHROPIC_API_KEY missing — cannot parse image.")
                    continue
                file_id = (photos[-1].get("file_id") if photos
                           else doc.get("file_id"))
                send(token, chat_id, "🔍 Analysing screenshot...")
                image_bytes = download_tg_file(token, file_id)
                if not image_bytes:
                    send(token, chat_id, "⚠️ Could not download image.")
                    continue
                vision_result = parse_position_image(image_bytes, api_key)
                if not vision_result or "error" in vision_result:
                    send(token, chat_id,
                         "❓ Could not find a position in that screenshot.\n"
                         "Send a text command instead: `/enter BTC short 103000`")
                    continue
                update = _position_from_vision(vision_result)
                if not update:
                    send(token, chat_id,
                         "❓ Parsed image but could not extract symbol/price.\n"
                         "Send manually: `/enter BTC short 103000`")
                    continue
                pending.append(update)
                save_pending(pending)
                sym   = update["symbol"]
                dirn  = update["direction"]
                price = update.get("price")
                stop  = update.get("stop_loss")
                exch  = vision_result.get("exchange") or "exchange"
                pnl   = vision_result.get("pnl_pct")
                lines = [f"✅ Position detected from {exch} screenshot:",
                         f"*{dirn} {sym}*"
                         + (f" @ {_fmt_price(price)}" if price else ""),
                         f"Stop: {_fmt_price(stop)}" if stop else "Stop: not found",
                         f"P&L: {pnl:+.2f}%" if pnl is not None else ""]
                lines.append("\n_If wrong, use /enter or /close to correct._")
                send(token, chat_id, "\n".join(l for l in lines if l))
                continue

            # ── Text command ──
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
                size_usd    = parsed.get("size_usd")
                size_note   = f", size ${size_usd:,.0f}" if size_usd is not None else ""
                direction   = parsed.get("direction", "LONG")
                market_type = parsed.get("market_type", "spot")
                send(token, chat_id,
                     f"✅ Queued: *{direction} {parsed['symbol']}* ({market_type}) @ "
                     f"{_fmt_price(parsed.get('price'))}{size_note}\n"
                     f"_Will be applied on next daily run._")

            elif action == "CLOSE":
                kind = "PARTIAL CLOSE" if parsed.get("partial") else "CLOSE"
                dir_note = f" {parsed['direction']}" if parsed.get("direction") else ""
                send(token, chat_id,
                     f"✅ Queued: *{kind} {parsed['symbol']}{dir_note}*\n"
                     f"_Will be applied on next daily run._")

            elif action == "NOTE":
                send(token, chat_id,
                     f"✅ Queued note for *{parsed['symbol']}*: _{parsed.get('note', '')}_")

        except Exception as e:
            print(f"[TG] ERROR processing update {upd.get('update_id', '?')}: {e}")
            try:
                if chat_id:
                    send(token, chat_id, f"⚠️ Internal error: {e}")
            except Exception:
                pass

    OFFSET_FILE.write_text(str(new_offset))
    print(f"[TG] Processed {len(updates)} update(s), new offset={new_offset}, "
          f"pending queue={len(pending)}")


if __name__ == "__main__":
    run()
