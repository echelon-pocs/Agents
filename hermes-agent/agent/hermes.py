"""
Core Hermes agent loop.

process() is the single public entry point: takes a user message + chat_id,
runs the LLM→tool→LLM loop, persists the exchange, returns the reply string.

Models are called via OpenRouter's OpenAI-compatible API.  If the primary
model (DeepSeek Chat v3 free) hits a rate limit or errors, we fall back to
Llama 4 Maverick free.
"""
import json
import os
from pathlib import Path

import yaml
from openai import OpenAI, RateLimitError, APIStatusError

from agent.memory import Memory
from agent.tools import TOOL_DEFINITIONS, execute_tool

# ── config / soul ──────────────────────────────────────────────────────────────

_ROOT = Path(__file__).parent.parent   # hermes-agent/
_cfg: dict = None
_soul: str = None


def _config() -> dict:
    global _cfg
    if _cfg is None:
        with open(_ROOT / "config.yaml") as f:
            _cfg = yaml.safe_load(f)
    return _cfg


def _soul_prompt() -> str:
    global _soul
    if _soul is None:
        _soul = (_ROOT / _config()["agent"]["soul_file"]).read_text()
    return _soul


# ── OpenRouter client ──────────────────────────────────────────────────────────

def _client() -> OpenAI:
    cfg = _config()["openrouter"]
    return OpenAI(
        base_url=cfg["base_url"],
        api_key=os.environ["OPENROUTER_API_KEY"],
        default_headers={
            "HTTP-Referer": cfg.get("http_referer", ""),
            "X-Title":      cfg.get("x_title", "Hermes Agent"),
        },
    )


def _chat(client: OpenAI, model: str, messages: list, cfg: dict):
    return client.chat.completions.create(
        model=model,
        messages=messages,
        tools=TOOL_DEFINITIONS,
        tool_choice="auto",
        temperature=cfg["agent"]["temperature"],
        max_tokens=cfg["agent"]["max_tokens"],
        timeout=cfg["models"]["timeout_s"],
    )


# ── main loop ─────────────────────────────────────────────────────────────────

def process(user_message: str, chat_id: str, db_path: str) -> str:
    """
    Run one full turn of the agent loop.

    Loads conversation history, appends the new user message, calls the LLM,
    executes any tool calls, loops until the model stops calling tools or
    max_tool_rounds is reached, then stores the exchange and returns the reply.
    """
    cfg    = _config()
    memory = Memory(db_path)
    client = _client()

    history  = memory.get_history(chat_id, limit=cfg["agent"]["context_window_turns"])
    messages = [{"role": "system", "content": _soul_prompt()}] + history
    messages.append({"role": "user", "content": user_message})

    primary  = cfg["models"]["primary"]
    fallback = cfg["models"]["fallback"]
    max_rounds = cfg["agent"]["max_tool_rounds"]
    final_reply = None

    for _ in range(max_rounds):
        try:
            resp = _chat(client, primary, messages, cfg)
        except (RateLimitError, APIStatusError):
            try:
                resp = _chat(client, fallback, messages, cfg)
            except Exception as exc:
                return f"Both models unavailable right now. Error: {exc}"
        except Exception as exc:
            return f"LLM call failed: {exc}"

        choice = resp.choices[0]

        # No tool calls — we have a final answer
        if not choice.message.tool_calls:
            final_reply = choice.message.content or ""
            break

        # Append assistant turn with tool calls
        asst_msg: dict = {
            "role":       "assistant",
            "content":    choice.message.content or "",
            "tool_calls": [
                {
                    "id":   tc.id,
                    "type": "function",
                    "function": {
                        "name":      tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in choice.message.tool_calls
            ],
        }
        messages.append(asst_msg)

        # Execute each tool and append results
        for tc in choice.message.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            result = execute_tool(tc.function.name, args, memory, chat_id)
            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      result,
            })

    if final_reply is None:
        final_reply = "I seem to have gotten stuck in a loop. Please rephrase your message."

    # Persist this exchange
    memory.store_message(chat_id, "user",      user_message)
    memory.store_message(chat_id, "assistant", final_reply)

    return final_reply
