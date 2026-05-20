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

Image support:
  Send a screenshot of your trade (exchange fill, broker confirmation, chart
  with entry marked). The bot will read the image with Claude vision, show you
  what it found, and wait for you to reply YES or NO before queuing.

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

BASE_DIR     = Path(__file__).parent
PENDING_FILE = BASE_DIR / "pending_updates.json"
OFFSET_FILE  = BASE_DIR / ".tg_offset"
ENV_FILE     = BASE_DIR / ".env"
CONFIRM_FILE = BASE_DIR / ".pending_image_confirm.json"


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


# ─── Image confirmation persistence ──────────────────────────────────────────
# Stored between cron runs so YES/NO can arrive in a later invocation.

def load_image_confirm():
    if CONFIRM_FILE.exists():
        try:
            return json.loads(CONFIRM_FILE.read_text())
        except Exception:
            pass
    return None


def save_image_confirm(parsed):
    CONFIRM_FILE.write_text(json.dumps(parsed, indent=2))


def clear_image_confirm():
    if CONFIRM_FILE.exists():
        CONFIRM_FILE.unlink()


# ─── Claude vision image analysis ────────────────────────────────────────────

def download_telegram_photo(token, file_id):
    """Download a Telegram photo and return raw bytes, or None on failure."""
    resp = tg(token, "getFile", file_id=file_id)
    file_path = resp.get("result", {}).get("file_path", "")
    if not file_path:
        print(f"[TG] Could not get file_path for file_id={file_id}")
        return None, None
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else "jpg"
    media_type = "image/png" if ext == "png" else "image/jpeg"
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    try:
        r = requests.get(url, timeout=30)
        return r.content, media_type
    except Exception as e:
        print(f"[TG] Photo download error: {e}")
        return None, None


VISION_PROMPT = """You are reading a trading screenshot to extract an open position or trade fill.

Return ONLY a JSON object — no other text — with exactly these fields:
{
  "symbol":      "BTC",
  "direction":   "LONG" or "SHORT",
  "price":       103450.0,
  "size_usd":    500.0,
  "size_qty":    null,
  "market_type": "spot" or "futures",
  "confidence":  "HIGH" or "LOW",
  "notes":       "one-line description of what you read"
}

Rules:
- symbol: ticker only, uppercase (BTC not BTCUSDT, ETH not ETH/USD)
- direction: LONG for buy/long, SHORT for sell/short
- price: entry/fill price as a number, no commas or currency symbols
- size_usd: position size in USD if visible, else null
- size_qty: quantity in coins/contracts if visible and size_usd is null, else null
- market_type: "futures" if there is leverage, margin, or perpetual info; "spot" otherwise
- confidence: HIGH if all key fields are clearly readable, LOW if any are guessed
- If this is not a trade screenshot, return: {"error": "not a trade screenshot"}"""


def analyze_trade_image(image_bytes, media_type, api_key):
    """Send image to Claude Haiku vision and return parsed trade dict."""
    try:
        import anthropic
    except ImportError:
        return {"error": "anthropic library not installed"}

    try:
        client = anthropic.Anthropic(api_key=api_key)
        b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type":       "base64",
                            "media_type": media_type,
                            "data":       b64,
                        },
                    },
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }],
        )
        raw = message.content[0].text.strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start == -1 or end == 0:
            return {"error": f"No JSON in response: {raw[:120]}"}
        return json.loads(raw[start:end])
    except Exception as e:
        return {"error": str(e)}


def format_image_confirm_prompt(parsed):
    """Build the confirmation message shown to the user after image analysis."""
    direction   = parsed.get("direction", "?")
    symbol      = parsed.get("symbol", "?")
    price       = parsed.get("price")
    size_usd    = parsed.get("size_usd")
    size_qty    = parsed.get("size_qty")
    market_type = parsed.get("market_type", "spot")
    confidence  = parsed.get("confidence", "?")
    notes       = parsed.get("notes", "")

    price_str = _fmt_price(price)
    if size_usd is not None:
        size_str = f"Size: ${size_usd:,.0f}"
    elif size_qty is not None:
        size_str = f"Size: {size_qty} coins"
    else:
        size_str = "Size: not detected"

    conf_icon = "🟢" if confidence == "HIGH" else "🟡"

    return (
        f"📸 *Trade detected from image*\n\n"
        f"*{direction} {symbol}* ({market_type})\n"
        f"Entry: {price_str}\n"
        f"{size_str}\n"
        f"{conf_icon} Confidence: {confidence}\n"
        f"_\"{notes}\"_\n\n"
        f"Reply *YES* to queue this position, or *NO* to cancel."
    )


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
    if cmd == "close" and len(parts) >= 2:
        symbol    = parts[1].upper()
        modifiers = [p.lower() for p in parts[2:]]
        partial   = any(m == "partial" for m in modifiers)
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
                sym    = u.get("symbol", "")
                action = u.get("action", "?")
                price  = u.get("price")
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

📸 *Image:* send a screenshot of your trade fill and the bot will read it automatically.

Updates are queued and applied on the next daily run."""


# ─── Main polling loop ────────────────────────────────────────────────────────

def run():
    env   = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("[TG] ERROR: TELEGRAM_BOT_TOKEN not in .env — add it and retry.")
        sys.exit(1)

    chat_id   = env.get("TELEGRAM_CHAT_ID", "")
    api_key   = env.get("ANTHROPIC_API_KEY", "")

    offset = 0
    if OFFSET_FILE.exists():
        try:
            offset = int(OFFSET_FILE.read_text().strip())
        except ValueError:
            pass

    updates_resp = tg(token, "getUpdates", offset=offset, timeout=10)
    updates      = updates_resp.get("result", [])

    if not updates:
        print(f"[TG] No new messages (offset={offset})")
        return

    pending    = load_pending()
    new_offset = offset

    for upd in updates:
        try:
            new_offset = max(new_offset, upd.get("update_id", new_offset - 1) + 1)
            msg     = upd.get("message", {})
            if not isinstance(msg, dict):
                continue
            text    = msg.get("text", "")
            photo   = msg.get("photo")
            from_id = str(msg.get("chat", {}).get("id", ""))

            if not from_id:
                continue

            # Auto-register chat_id on first message
            if not chat_id and (text or photo):
                chat_id = from_id
                save_env_value("TELEGRAM_CHAT_ID", chat_id)
                print(f"[TG] Registered chat_id: {chat_id}")

            # Only accept messages from the registered chat
            if from_id != chat_id:
                send(token, from_id, "⛔ Unauthorised.")
                continue

            # ── Photo message: analyze with Claude vision ─────────────────
            if photo:
                print(f"[TG] Photo received ({len(photo)} sizes)")
                if not api_key:
                    send(token, chat_id,
                         "⚠️ ANTHROPIC_API_KEY not set — cannot analyse images.")
                    continue

                # Pick largest available size
                largest = max(photo, key=lambda p: p.get("file_size", 0))
                file_id  = largest.get("file_id", "")

                send(token, chat_id, "🔍 Analysing your screenshot…")

                image_bytes, media_type = download_telegram_photo(token, file_id)
                if not image_bytes:
                    send(token, chat_id, "⚠️ Could not download the image. Please try again.")
                    continue

                parsed = analyze_trade_image(image_bytes, media_type, api_key)
                print(f"[TG] Vision result: {parsed}")

                if "error" in parsed:
                    send(token, chat_id,
                         f"⚠️ Could not read trade: _{parsed['error']}_\n"
                         f"Try a clearer screenshot or use a `/enter` command.")
                    continue

                # Require the minimum fields
                if not parsed.get("symbol") or not parsed.get("price"):
                    send(token, chat_id,
                         "⚠️ Could not read symbol or price from the image.\n"
                         "Try a clearer screenshot or use a `/enter` command.")
                    continue

                # Save pending confirmation and ask the user
                parsed["_source"] = "image"
                save_image_confirm(parsed)
                send(token, chat_id, format_image_confirm_prompt(parsed))
                continue

            # ── Text message ──────────────────────────────────────────────
            if not text:
                continue

            print(f"[TG] Message: {text!r}")

            # YES / NO reply for image confirmation
            reply = text.strip().lower()
            if reply in ("yes", "y", "confirm"):
                confirm = load_image_confirm()
                if not confirm:
                    send(token, chat_id, "No pending image trade to confirm.")
                    continue
                clear_image_confirm()
                # Build a proper ENTER update from the vision result
                entry = {
                    "action":      "ENTER",
                    "symbol":      confirm.get("symbol", "?").upper(),
                    "direction":   confirm.get("direction", "LONG"),
                    "market_type": confirm.get("market_type", "spot"),
                    "price":       confirm.get("price"),
                    "size_usd":    confirm.get("size_usd"),
                    "size_qty":    confirm.get("size_qty"),
                    "timestamp":   datetime.now(timezone.utc).isoformat(),
                    "source":      "image",
                }
                pending.append(entry)
                save_pending(pending)
                direction   = entry["direction"]
                market_type = entry["market_type"]
                size_usd    = entry.get("size_usd")
                size_note   = f", size ${size_usd:,.0f}" if size_usd is not None else ""
                send(token, chat_id,
                     f"✅ Queued: *{direction} {entry['symbol']}* ({market_type}) @ "
                     f"{_fmt_price(entry['price'])}{size_note}\n"
                     f"_Will be applied on next daily run._")
                continue

            if reply in ("no", "n", "cancel"):
                confirm = load_image_confirm()
                if not confirm:
                    send(token, chat_id, "No pending image trade to cancel.")
                    continue
                clear_image_confirm()
                send(token, chat_id, "❌ Image trade cancelled.")
                continue

            # Regular slash command
            parsed = parse_command(text)

            if parsed is None:
                # Check if there's a pending image confirmation they might have missed
                confirm = load_image_confirm()
                hint = "\n\n_Tip: reply YES or NO to confirm the pending image trade._" \
                       if confirm else ""
                send(token, chat_id,
                     "Not a command. Send /help to see available commands." + hint)
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
                kind     = "PARTIAL CLOSE" if parsed.get("partial") else "CLOSE"
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
