"""
Hermes Agent — Modal serverless backend.

── Deploy ─────────────────────────────────────────────────────────────────────
  modal deploy hermes-agent/modal_app.py

── First-time setup ───────────────────────────────────────────────────────────
  1. Create Modal secrets (once):
       modal secret create hermes-secrets \\
         OPENROUTER_API_KEY=sk-or-... \\
         TELEGRAM_BOT_TOKEN=123456:ABC... \\
         TELEGRAM_CHAT_ID=your_numeric_id

  2. Deploy the app:
       modal deploy hermes-agent/modal_app.py

  3. Register the Telegram webhook (requires TELEGRAM_BOT_TOKEN in your env):
       TELEGRAM_BOT_TOKEN=... modal run hermes-agent/modal_app.py::setup_webhook

── Webhook URL form ───────────────────────────────────────────────────────────
  https://<modal-username>--hermes-agent-webhook.modal.run

── Cost ───────────────────────────────────────────────────────────────────────
  Modal free tier: $30/month credit. The function only runs when a Telegram
  message arrives, so idle cost is ~$0.
"""
import sys
from pathlib import Path

import modal

# ── Modal primitives ───────────────────────────────────────────────────────────

app    = modal.App("hermes-agent")
volume = modal.Volume.from_name("hermes-data", create_if_missing=True)

_HERE = Path(__file__).parent

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(["openai>=1.0.0", "pyyaml", "requests"])
)

# Mount the local hermes-agent/ directory into the container at /app.
# This means code changes are picked up on every invocation without
# rebuilding the image.
_mount = modal.Mount.from_local_dir(_HERE, remote_path="/app")

_secrets = modal.Secret.from_name("hermes-secrets")


# ── Webhook endpoint ───────────────────────────────────────────────────────────

@app.function(
    image=image,
    volumes={"/data": volume},
    mounts=[_mount],
    secrets=[_secrets],
    timeout=90,
)
@modal.web_endpoint(method="POST")
def webhook(body: dict) -> dict:
    """Receive a Telegram Update, run the Hermes agent, reply."""
    sys.path.insert(0, "/app")

    import os
    from agent.hermes import process
    from telegram_bot import extract_message, send_message, send_typing

    token  = os.environ["TELEGRAM_BOT_TOKEN"]
    db     = "/data/hermes.db"

    msg = extract_message(body)
    if not msg:
        return {"ok": True}

    chat_id = msg.get("chat", {}).get("id")
    text    = (msg.get("text") or "").strip()

    if not chat_id or not text:
        return {"ok": True}

    # Restrict to owner when TELEGRAM_CHAT_ID is configured
    allowed = os.environ.get("TELEGRAM_CHAT_ID", "")
    if allowed and str(chat_id) not in {x.strip() for x in allowed.split(",")}:
        send_message(token, chat_id, "You're not authorised to use this bot.")
        return {"ok": True}

    if text in ("/start", "/help"):
        send_message(token, chat_id, _HELP)
        return {"ok": True}

    send_typing(token, chat_id)

    try:
        reply = process(text, str(chat_id), db)
    except Exception as exc:
        print(f"[hermes] unhandled error: {exc}")
        reply = f"Something went wrong on my end: {exc}"

    send_message(token, chat_id, reply)
    volume.commit()      # flush SQLite writes to the Modal volume
    return {"ok": True}


_HELP = (
    "*Hermes* — your persistent AI assistant\n\n"
    "Send me any message. I remember our conversations across sessions.\n\n"
    "I can store and recall facts, answer questions, and help with tasks.\n\n"
    "_Running on Modal · DeepSeek Chat v3 via OpenRouter_"
)


# ── Setup helpers ──────────────────────────────────────────────────────────────

@app.local_entrypoint()
def setup_webhook():
    """
    Register the Modal webhook URL with Telegram.
    Run after deploying:  modal run hermes-agent/modal_app.py::setup_webhook
    Requires TELEGRAM_BOT_TOKEN in your local environment.
    """
    import os
    import requests

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in your local environment first.")

    url = webhook.web_url
    print(f"Registering webhook: {url}")

    r = requests.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        json={"url": url, "allowed_updates": ["message"]},
        timeout=15,
    )
    data = r.json()
    if data.get("ok"):
        print(f"Webhook set successfully.")
    else:
        print(f"Telegram error: {data}")


@app.local_entrypoint()
def clear_webhook():
    """Remove the Telegram webhook (switches bot back to polling mode)."""
    import os
    import requests

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in your local environment first.")

    r = requests.post(
        f"https://api.telegram.org/bot{token}/deleteWebhook",
        json={"drop_pending_updates": True},
        timeout=15,
    )
    print(r.json())
