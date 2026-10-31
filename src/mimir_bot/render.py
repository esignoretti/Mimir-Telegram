"""Turning a stream of deltas into one Telegram message that keeps up.

Telegram allows ~1 edit per second per chat before it starts throttling, so edits
are rate-limited and the message is truncated from the *front* while streaming:
the tail is what the user is reading. The final answer is then delivered in full,
split across as many messages as it needs.
"""

from __future__ import annotations

import time

from .mimir import StreamEvent

#: Telegram's hard limit for a single message body.
MESSAGE_LIMIT = 4096

#: Shown while the model is still reasoning and has produced no answer yet.
THINKING = "thinking…"

NO_ANSWER = "…no answer (hit the token limit while thinking)"
INTERRUPTED = "[interrupted]"
READY = "answer ready ↑"


def thinking_label(reasoning_chars: int) -> str:
    if reasoning_chars < 200:
        return THINKING
    return f"{THINKING} ({reasoning_chars} chars)"


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split on paragraph boundaries where possible; never lose a character.

    ``"".join(split_message(t)) == t`` for every input, which matters because the
    tail of an answer is often the part that carries the conclusion.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut > 0:
            cut += 2
        else:
            cut = window.rfind("\n")
            if cut > 0:
                cut += 1
            else:
                cut = limit
        parts.append(rest[:cut])
        rest = rest[cut:]
    parts.append(rest)
    return parts


class TurnRenderer:
    def __init__(
        self,
        chat_id: int,
        tg,
        placeholder_id: int,
        edit_interval: float = 1.5,
        notify_after: int = 60,
    ) -> None:
        self.chat_id = chat_id
        self.tg = tg
        self.placeholder_id = placeholder_id
        self.edit_interval = edit_interval
        self.notify_after = notify_after

        self.reasoning_chars = 0
        self.answer_text = ""
        self.usage: dict | None = None
        self._last_edit_at = 0.0
        self._last_sent_text: str | None = None
        self._started_at = time.monotonic()
        self._finished = False

    def _display(self) -> str:
        if self.answer_text:
            # Keep the tail: that is where the answer is still arriving.
            return self.answer_text[-MESSAGE_LIMIT:]
        return thinking_label(self.reasoning_chars)

    async def _maybe_edit(self, force: bool = False) -> None:
        display = self._display()
        if display == self._last_sent_text:
            return
        now = time.monotonic()
        if not force and now - self._last_edit_at < self.edit_interval:
            return
        self._last_edit_at = now
        await self.tg.edit_message(self.chat_id, self.placeholder_id, display)
        self._last_sent_text = display

    async def on_event(self, event: StreamEvent) -> None:
        if event.kind == "reasoning":
            self.reasoning_chars += len(event.text)
        elif event.kind == "content":
            self.answer_text += event.text
        elif event.kind == "usage":
            self.usage = event.text  # kept for logging only
            return
        else:
            return
        await self._maybe_edit()

    async def finish(self, final_text: str) -> None:
        if self._finished:
            return
        self._finished = True

        text = final_text
        if not text and self.reasoning_chars:
            text = NO_ANSWER
        if not text:
            text = "…"

        parts = split_message(text)
        await self.tg.edit_message(self.chat_id, self.placeholder_id, parts[0])
        self._last_sent_text = parts[0]
        for part in parts[1:]:
            await self.tg.send_message(self.chat_id, part)

        if self.notify_after >= 0 and self.elapsed >= self.notify_after:
            await self.tg.send_message(self.chat_id, READY, reply_to=self.placeholder_id)

    async def interrupt(self) -> None:
        self._finished = True
        await self.tg.edit_message(self.chat_id, self.placeholder_id, INTERRUPTED)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started_at
