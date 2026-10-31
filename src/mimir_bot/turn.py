"""One turn: read settings, stream from Mimir, render, persist.

The persistence rule is the interesting part. Reasoning is streamed to the user
but never stored: replaying it as context would double the prompt size and
confuse later turns, since the model sees its own private scratchpad as if it had
been said out loud. Only the answer text goes into history.

Nothing is written until the stream has produced its first event, so a failed
request (bad model, oversized prompt) leaves no orphan user message behind to be
replayed on the next attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .config import Config, MODELS
from .mimir import MimirClient
from .render import NO_ANSWER, TurnRenderer
from .store import Store

log = logging.getLogger(__name__)

#: Photos always go to the vision model for that turn only.
VISION_MODEL = "vllm/mimir-vision"

#: Room left for the reply plus the system prompt, in tokens.
RESPONSE_HEADROOM = 8_192


def token_budget(model: str, configured: int) -> int:
    """The configured history budget, capped by what the model can actually take."""
    window = MODELS.get(model)
    if window is None:
        return configured
    return min(configured, max(window - RESPONSE_HEADROOM, 0))


def build_request(history: list[dict], system_prompt: str) -> list[dict]:
    if not system_prompt:
        return list(history)
    return [{"role": "system", "content": system_prompt}, *history]


def image_content(text: str, image_data_url: str) -> list[dict]:
    """The multimodal content array the vision model expects."""
    return [
        {"type": "text", "text": text or "Describe this image."},
        {"type": "image_url", "image_url": {"url": image_data_url}},
    ]


class GenerationLock:
    """At most one turn per chat, newest wins.

    Cancelling the client-side request is enough to free the server slot (measured:
    the next request is answered immediately), so no upstream cancel call is needed.
    """

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}

    def current(self, chat_id: int) -> asyncio.Task | None:
        task = self._tasks.get(chat_id)
        if task is not None and task.done():
            return None
        return task

    def chat_ids(self) -> list[int]:
        return [cid for cid, task in self._tasks.items() if not task.done()]

    async def replace(self, chat_id: int, coro) -> asyncio.Task:
        existing = self._tasks.get(chat_id)
        if existing is not None and not existing.done():
            existing.cancel()
            with contextlib.suppress(BaseException):
                await existing
        task = asyncio.create_task(coro)
        self._tasks[chat_id] = task
        task.add_done_callback(lambda t: self._forget(chat_id, t))
        return task

    def _forget(self, chat_id: int, task: asyncio.Task) -> None:
        # Only drop the entry if it still points at this task: a newer turn may
        # already have replaced it.
        if self._tasks.get(chat_id) is task:
            self._tasks.pop(chat_id, None)

    async def cancel(self, chat_id: int) -> bool:
        task = self.current(chat_id)
        if task is None:
            return False
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        return True


class TurnRunner:
    def __init__(self, store: Store, mimir: MimirClient, cfg: Config) -> None:
        self.store = store
        self.mimir = mimir
        self.cfg = cfg

    async def _drop(self, row_id: int) -> None:
        """Best effort: a failed delete must not mask the original error."""
        if not row_id:
            return
        with contextlib.suppress(Exception):
            await self.store.delete_message(row_id)

    async def run(
        self,
        chat_id: int,
        user_text: str,
        tg,
        placeholder_id: int,
        image_data_url: str | None = None,
    ) -> str:
        settings = await self.store.get_chat(chat_id)

        model = VISION_MODEL if image_data_url else settings.model
        effort = settings.effort
        budget = token_budget(model, self.cfg.history_token_budget)
        history = await self.store.history(chat_id, budget)

        user_message: dict = {"role": "user", "content": user_text}
        if image_data_url:
            user_message = {
                "role": "user",
                "content": image_content(user_text, image_data_url),
            }
        # History stays text-only: the image is not re-sent on later turns, and a
        # stored data URL would grow every subsequent prompt by its own size.
        stored_text = f"{user_text} [image]" if image_data_url else user_text

        request = build_request([*history, user_message], self.cfg.system_prompt)
        renderer = TurnRenderer(
            chat_id,
            tg,
            placeholder_id,
            notify_after=self.cfg.notify_after_seconds,
        )

        answer = ""
        started = False
        user_row_id = 0
        try:
            async for event in self.mimir.stream(request, model, effort):
                if not started:
                    started = True
                    user_row_id = await self.store.add_message(
                        chat_id, "user", stored_text
                    )
                await renderer.on_event(event)
                if event.kind == "content":
                    answer += event.text
        except asyncio.CancelledError:
            # The user moved on to another question. Drop the question with the
            # answer: a lone user row would be replayed on the next turn as if it
            # had been asked and answered.
            await self._drop(user_row_id)
            await renderer.interrupt()
            raise
        except Exception as exc:
            await self._drop(user_row_id)
            log.warning("turn failed for chat %s: %s", chat_id, exc)
            await renderer.fail(str(exc))
            return ""

        await renderer.finish(answer)
        if started:
            # A reasoning-only turn has no answer text; store the same placeholder
            # the user saw so history never contains an empty assistant message.
            await self.store.add_message(
                chat_id, "assistant", answer or NO_ANSWER, model=model
            )
        log.info(
            "turn done chat=%s model=%s reasoning_chars=%s answer_chars=%s "
            "elapsed=%.1fs usage=%s",
            chat_id,
            model,
            renderer.reasoning_chars,
            len(answer),
            renderer.elapsed,
            renderer.usage,
        )
        return answer
