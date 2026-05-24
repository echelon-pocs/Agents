# Hermes Agent

Serverless AI assistant: Modal backend · OpenRouter LLMs · Telegram UI · SQLite memory.

## Stack

| Layer | Component | Notes |
|---|---|---|
| Runtime | Modal serverless | Hibernates when idle; free $30/mo credit |
| LLM | OpenRouter free tier | DeepSeek Chat v3 primary, Llama 4 Maverick fallback |
| Gateway | Telegram Bot API | Webhook (event-driven, no polling) |
| Memory | SQLite on Modal Volume | Persists across container restarts |

## File layout

```
hermes-agent/
├── SOUL.md          — system prompt / agent persona
├── config.yaml      — all tuneable parameters
├── modal_app.py     — Modal app, webhook endpoint, setup helpers
├── telegram_bot.py  — thin Telegram API helpers
├── agent/
│   ├── hermes.py    — core LLM→tool→LLM loop
│   ├── tools.py     — tool definitions + execution
│   └── memory.py    — SQLite memory layer
└── requirements.txt
```

## First-time setup

### 1. Create Modal secrets

```bash
modal secret create hermes-secrets \
  OPENROUTER_API_KEY=sk-or-... \
  TELEGRAM_BOT_TOKEN=123456:ABC... \
  TELEGRAM_CHAT_ID=your_numeric_id
```

Get `OPENROUTER_API_KEY` from [openrouter.ai/keys](https://openrouter.ai/keys).  
Get `TELEGRAM_BOT_TOKEN` from @BotFather → `/newbot`.  
Get `TELEGRAM_CHAT_ID` by messaging @userinfobot.

### 2. Deploy

```bash
# From the repo root
modal deploy hermes-agent/modal_app.py
```

### 3. Register the webhook

```bash
TELEGRAM_BOT_TOKEN=... modal run hermes-agent/modal_app.py::setup_webhook
```

This prints the Modal URL and registers it with Telegram. The bot is now live.

## Development

Changes to Python files under `hermes-agent/` take effect immediately on the
next invocation — no redeploy needed. The Modal mount copies local files into
the container on each cold start.

To rebuild the pip layer (e.g. after adding a dependency to `requirements.txt`
and `modal_app.py`):

```bash
modal deploy hermes-agent/modal_app.py
```

## Extending

**Add a tool:** Define it in `agent/tools.py` (add to `TOOL_DEFINITIONS` and
`execute_tool`), then describe when to use it in `SOUL.md`.

**Change models:** Edit `config.yaml` → `models.primary` / `models.fallback`.
Any OpenRouter model ID works. Free-tier IDs end in `:free`.

**Add commands:** Handle them in `modal_app.py` → `webhook()` before the
`process()` call, following the `/start` pattern.

## Useful Modal commands

```bash
modal app list                          # see deployed apps
modal volume ls hermes-data /           # inspect the SQLite volume
modal run hermes-agent/modal_app.py::clear_webhook   # remove webhook
modal app stop hermes-agent             # stop the app
```
