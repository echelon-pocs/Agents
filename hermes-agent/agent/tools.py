"""
Tool definitions and execution for the Hermes agent.

TOOL_DEFINITIONS: OpenAI-compatible function schemas sent to the LLM.
execute_tool():   Dispatches tool calls and returns JSON result strings.
"""
import json
from datetime import datetime, timezone


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "memory_store",
            "description": (
                "Store a fact, preference, or piece of context in persistent memory "
                "so it can be recalled in future conversations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The information to remember",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Category tags, e.g. ['preference', 'personal', 'project']"
                        ),
                    },
                    "importance": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "1=minor detail, 3=useful context, 5=critical",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": (
                "Search stored memories to recall relevant information from "
                "previous conversations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords or phrase to search for",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_datetime",
            "description": "Return the current UTC date and time.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def execute_tool(tool_name: str, tool_args: dict, memory, chat_id: str) -> str:
    """Execute a single tool call; return a JSON string result."""
    try:
        if tool_name == "memory_store":
            mem_id = memory.store_memory(
                chat_id=chat_id,
                content=tool_args["content"],
                tags=tool_args.get("tags", []),
                importance=tool_args.get("importance", 3),
            )
            return json.dumps({"status": "stored", "memory_id": mem_id})

        if tool_name == "memory_search":
            results = memory.search_memories(
                chat_id=chat_id,
                query=tool_args.get("query"),
                limit=6,
            )
            if not results:
                return json.dumps({"results": [], "note": "No matching memories found."})
            return json.dumps({"results": results})

        if tool_name == "get_datetime":
            now = datetime.now(timezone.utc)
            return json.dumps({
                "utc":         now.isoformat(),
                "date":        now.strftime("%Y-%m-%d"),
                "time":        now.strftime("%H:%M:%S"),
                "day_of_week": now.strftime("%A"),
            })

        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as exc:
        return json.dumps({"error": str(exc)})
