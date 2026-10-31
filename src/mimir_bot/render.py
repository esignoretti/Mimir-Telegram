"""Turning a stream of deltas into one Telegram message that keeps up.

Telegram allows ~1 edit per second per chat before it starts throttling, so edits
are rate-limited and the message is truncated from the *front* while streaming:
the tail is what the user is reading. The final answer is then delivered in full,
split across as many messages as it needs.
"""

from __future__ import annotations

import logging
import time

from .mimir import StreamEvent
from .telegram import RateLimited

log = logging.getLogger(__name__)

#: Telegram's hard limit for a single message body, counted in UTF-16 code units
#: (not code points: an emoji outside the BMP counts as two).
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


def tail_within(text: str, limit: int) -> str:
    """The longest suffix of ``text`` that fits in ``limit`` UTF-16 units.

    Called once per streamed chunk, so this must not be quadratic: take the cheap
    slice first (at most ``limit`` code points) and only count units when that
    slice is actually too wide, i.e. when it contains astral characters.
    """
    chunk = text[-limit:]
    if chunk.isascii() or _units(chunk) <= limit:
        return chunk

    # Walk back from the end, at most ``limit`` characters, summing widths.
    used = 0
    start = len(chunk)
    while start > 0:
        width = len(chunk[start - 1].encode("utf-16-le")) // 2
        if used + width > limit:
            break
        used += width
        start -= 1
    return chunk[start:]


def _units(text: str) -> int:
    """Length the way Telegram counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split on paragraph boundaries where possible; never lose a character.

    ``"".join(split_message(t)) == t`` for every input, which matters because the
    tail of an answer is often the part that carries the conclusion.
    """
    if _units(text) <= limit:
        return [text]

    parts: list[str] = []
    rest = text
    while _units(rest) > limit:
        cut = _cut_point(rest, limit)
        parts.append(rest[:cut])
        rest = rest[cut:]
    parts.append(rest)
    return parts


def _cut_point(rest: str, limit: int) -> int:
    """How many characters fit in ``limit`` UTF-16 units, preferring a break."""
    used = 0
    fits = 0
    for char in rest:
        width = len(char.encode("utf-16-le")) // 2
        if used + width > limit:
            break
        used += width
        fits += 1
    if fits <= 0:
        return 1

    window = rest[:fits]
    cut = window.rfind("\n\n")
    if cut > 0:
        return cut + 2
    cut = window.rfind("\n")
    if cut > 0:
        return cut + 1
    return fits


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
        self.usage: str | None = None
        self._last_edit_at = 0.0
        self._last_sent_text: str | None = None
        self._started_at = time.monotonic()
        self._finished = False

    def _display(self) -> str:
        if self.answer_text:
            # Keep the tail: that is where the answer is still arriving.
            return tail_within(self.answer_text, MESSAGE_LIMIT)
        return thinking_label(self.reasoning_chars)

    async def _maybe_edit(self, force: bool = False) -> None:
        display = self._display()
        if display == self._last_sent_text:
            return
        now = time.monotonic()
        if not force and now - self._last_edit_at < self.edit_interval:
            return
        self._last_edit_at = now
        try:
            await self.tg.edit_message(self.chat_id, self.placeholder_id, display)
        except RateLimited as exc:
            # Spec: back off and skip this edit rather than fail the turn.
            self._last_edit_at = now + exc.retry_after
            return
        except RuntimeError as exc:
            # A transient API error (deleted placeholder, oversized text) must not
            # cost the user the answer that is still streaming in.
            log.warning("edit skipped for chat %s: %s", self.chat_id, exc)
            return
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
        """Deliver the answer in full. Best effort: never raises.

        The answer has already been paid for by the time this runs, so a Telegram
        failure here is logged and swallowed rather than losing the result.
        """
        if self._finished:
            return
        self._finished = True

        text = final_text
        if not text and self.reasoning_chars:
            text = NO_ANSWER
        if not text:
            text = "…"

        parts = split_message(text)
        delivered = await self._edit_best_effort(parts[0])
        self._last_sent_text = parts[0]
        if not delivered:
            # The placeholder is gone (deleted, or the edit kept failing): fall
            # back to a fresh message so the answer is not lost.
            await self._send_best_effort(parts[0])
        for part in parts[1:]:
            await self._send_best_effort(part)

        if self.notify_after >= 0 and self.elapsed >= self.notify_after:
            await self._send_best_effort(READY, reply_to=self.placeholder_id)

    async def _edit_best_effort(self, text: str) -> bool:
        try:
            await self.tg.edit_message(self.chat_id, self.placeholder_id, text)
            return True
        except Exception as exc:
            log.warning("final edit failed for chat %s: %s", self.chat_id, exc)
            return False

    async def _send_best_effort(self, text: str, reply_to: int | None = None) -> None:
        try:
            await self.tg.send_message(self.chat_id, text, reply_to=reply_to)
        except Exception as exc:
            log.warning("send failed for chat %s: %s", self.chat_id, exc)

    async def interrupt(self) -> None:
        self._finished = True
        try:
            await self.tg.edit_message(
                self.chat_id, self.placeholder_id, INTERRUPTED
            )
        except Exception as exc:
            log.warning("interrupt edit failed for chat %s: %s", self.chat_id, exc)

    async def fail(self, message: str) -> None:
        """Show an error in the placeholder, best effort.

        The placeholder may be gone or the text may be rejected; either way the
        caller has already decided the turn is over, so this never raises.
        """
        self._finished = True
        text = f"error: {message}"[:MESSAGE_LIMIT]
        if await self._edit_best_effort(text):
            return
        await self._send_best_effort(text)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started_at
