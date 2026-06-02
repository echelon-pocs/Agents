"""
Thin Telegram Bot API helpers used by modal_app.py.

Deliberately minimal — raw requests, no async, no framework.
"""
import requests


def tg(token: str, method: str, **params) -> dict:
    """POST to a Telegram Bot API method; return parsed JSON or {}."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=params,
            timeout=20,
        )
        return r.json()
    except Exception as exc:
        print(f"[TG] {method} error: {exc}")
        return {}


def send_message(token: str, chat_id, text: str, parse_mode: str = "Markdown") -> dict:
    return tg(
        token, "sendMessage",
        chat_id=chat_id,
        text=text[:4000],
        parse_mode=parse_mode,
    )


def send_typing(token: str, chat_id) -> None:
    tg(token, "sendChatAction", chat_id=chat_id, action="typing")


def set_webhook(token: str, url: str, secret_token: str = None) -> dict:
    params = {"url": url, "allowed_updates": ["message"]}
    if secret_token:
        params["secret_token"] = secret_token
    return tg(token, "setWebhook", **params)


def delete_webhook(token: str) -> dict:
    return tg(token, "deleteWebhook", drop_pending_updates=True)


def extract_message(update: dict) -> dict | None:
    """Return the processable message from a Telegram Update, or None."""
    return update.get("message") or update.get("edited_message")
