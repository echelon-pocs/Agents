"""
SQLite-backed memory layer.

Stored on a Modal volume so it survives container recycling.
Two concerns: conversations (rolling window for context) and
long-term memories (explicitly stored facts/preferences).
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class Memory:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    # ── internal ──────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id      TEXT    NOT NULL,
                    role         TEXT    NOT NULL,
                    content      TEXT    NOT NULL,
                    tool_calls   TEXT,
                    tool_call_id TEXT,
                    name         TEXT,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id      TEXT    NOT NULL,
                    content      TEXT    NOT NULL,
                    tags         TEXT    DEFAULT '[]',
                    importance   INTEGER DEFAULT 1,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    accessed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_conv_chat
                    ON conversations(chat_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_mem_chat
                    ON memories(chat_id, importance);
            """)

    # ── conversation history ───────────────────────────────────────────────────

    def store_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        tool_calls=None,
        tool_call_id: str = None,
        name: str = None,
    ):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations "
                "(chat_id, role, content, tool_calls, tool_call_id, name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chat_id, role, content,
                    json.dumps(tool_calls) if tool_calls else None,
                    tool_call_id, name,
                ),
            )

    def get_history(self, chat_id: str, limit: int = 30) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content, tool_calls, tool_call_id, name "
                "FROM conversations "
                "WHERE chat_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()

        messages = []
        for row in reversed(rows):
            msg = {"role": row["role"], "content": row["content"]}
            if row["tool_calls"]:
                msg["tool_calls"] = json.loads(row["tool_calls"])
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["name"]:
                msg["name"] = row["name"]
            messages.append(msg)
        return messages

    # ── long-term memory ───────────────────────────────────────────────────────

    def store_memory(
        self,
        chat_id: str,
        content: str,
        tags: list = None,
        importance: int = 3,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO memories (chat_id, content, tags, importance) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, content, json.dumps(tags or []), max(1, min(5, importance))),
            )
            return cursor.lastrowid

    def search_memories(self, chat_id: str, query: str = None, limit: int = 8) -> list:
        with self._connect() as conn:
            if query:
                rows = conn.execute(
                    "SELECT id, content, tags, importance, created_at "
                    "FROM memories "
                    "WHERE chat_id = ? AND content LIKE ? "
                    "ORDER BY importance DESC, accessed_at DESC "
                    "LIMIT ?",
                    (chat_id, f"%{query}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, content, tags, importance, created_at "
                    "FROM memories "
                    "WHERE chat_id = ? "
                    "ORDER BY importance DESC, accessed_at DESC "
                    "LIMIT ?",
                    (chat_id, limit),
                ).fetchall()

        results = [
            {
                "id": row["id"],
                "content": row["content"],
                "tags": json.loads(row["tags"]),
                "importance": row["importance"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

        if results:
            ids = [r["id"] for r in results]
            placeholders = ",".join("?" * len(ids))
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE memories SET accessed_at = CURRENT_TIMESTAMP "
                    f"WHERE id IN ({placeholders})",
                    ids,
                )

        return results
