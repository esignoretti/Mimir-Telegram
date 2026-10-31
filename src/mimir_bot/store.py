"""SQLite persistence: per-chat settings and message history.

One file, two tables, no migrations beyond CREATE TABLE IF NOT EXISTS — the
schema is small enough that a version table would be more machinery than it
saves. Trimming happens on read (history) so the stored log stays complete for
later inspection while the prompt stays inside the budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import aiosqlite

#: Measured against the live API: 104,889 chars -> 22,952 prompt tokens.
CHARS_PER_TOKEN = 4.6

DEFAULT_MODEL = "vllm/mimir-slim"
DEFAULT_EFFORT = "none"

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    model   TEXT NOT NULL,
    effort  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role    TEXT NOT NULL,
    content TEXT NOT NULL,
    model   TEXT,
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, id);
"""


@dataclass(frozen=True)
class ChatSettings:
    chat_id: int
    model: str
    effort: str


def estimate_tokens(text: str) -> float:
    return len(text) / CHARS_PER_TOKEN


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store.init() was not awaited")
        return self._db

    async def get_chat(self, chat_id: int) -> ChatSettings:
        async with self.db.execute(
            "SELECT model, effort FROM chats WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            return ChatSettings(chat_id, row[0], row[1])
        await self.db.execute(
            "INSERT INTO chats (chat_id, model, effort) VALUES (?, ?, ?)",
            (chat_id, DEFAULT_MODEL, DEFAULT_EFFORT),
        )
        await self.db.commit()
        return ChatSettings(chat_id, DEFAULT_MODEL, DEFAULT_EFFORT)

    async def set_model(self, chat_id: int, model: str) -> None:
        await self.get_chat(chat_id)
        await self.db.execute(
            "UPDATE chats SET model = ? WHERE chat_id = ?", (model, chat_id)
        )
        await self.db.commit()

    async def set_effort(self, chat_id: int, effort: str) -> None:
        await self.get_chat(chat_id)
        await self.db.execute(
            "UPDATE chats SET effort = ? WHERE chat_id = ?", (effort, chat_id)
        )
        await self.db.commit()

    async def reset_chat(self, chat_id: int) -> None:
        await self.db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await self.db.commit()

    async def add_message(
        self,
        chat_id: int,
        role: str,
        content: str,
        model: str | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO messages (chat_id, role, content, model, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, role, content, model, time.time()),
        )
        await self.db.commit()

    async def history(self, chat_id: int, token_budget: int) -> list[dict[str, str]]:
        async with self.db.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()

        kept: list[dict[str, str]] = []
        used = 0.0
        for role, content in reversed(rows):
            cost = estimate_tokens(content)
            if kept and used + cost > token_budget:
                break
            kept.append({"role": role, "content": content})
            used += cost
        kept.reverse()
        return kept

    async def prune(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86_400
        cur = await self.db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
        await self.db.commit()
        return cur.rowcount or 0
