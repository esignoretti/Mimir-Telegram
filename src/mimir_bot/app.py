"""Update dispatch and the long-poll loop.

There are no webhooks: the bot lives behind no public port, so it polls. That
also means the whole dispatch path is a plain function of an update dict, which
is what makes the tests below possible without a network.

A turn is started as a task and *not* awaited, so the next message from the same
chat can interrupt it. ``idle()`` is the handle tests and shutdown use to wait
for whatever is in flight.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .commands import STOP_MARKER, handle_command
from .config import Config
from .mimir import MimirClient
from .telegram import Telegram
from .turn import GenerationLock, TurnRunner
from .store import Store

log = logging.getLogger(__name__)

PLACEHOLDER = "…"
POLL_TIMEOUT = 30
HEALTHCHECK_INTERVAL = 60
PRUNE_INTERVAL = 86_400


def is_allowed(update: dict, allowed: frozenset[int]) -> bool:
    message = update.get("message") or {}
    sender = (message.get("from") or {}).get("id")
    return sender in allowed


def _message(update: dict) -> dict | None:
    return update.get("message") or None


def largest_photo(message: dict) -> dict | None:
    """Telegram sends several sizes; the last entry is the largest."""
    photos = message.get("photo") or []
    if not photos:
        return None
    return max(photos, key=lambda p: p.get("file_size") or p.get("width") or 0)


class App:
    def __init__(
        self, cfg: Config, store: Store, tg: Telegram, mimir: MimirClient
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.tg = tg
        self.mimir = mimir
        self.runner = TurnRunner(store, mimir, cfg)
        self.lock = GenerationLock()
        self._tasks: set[asyncio.Task] = set()
        self._offset = 0

    # -- dispatch ---------------------------------------------------------

    async def handle_update(self, update: dict) -> None:
        if not is_allowed(update, self.cfg.allowed_user_ids):
            log.info("ignoring update from disallowed user")
            return

        message = _message(update)
        if message is None:
            return
        chat_id = int(message["chat"]["id"])
        text = message.get("text") or message.get("caption") or ""

        if text.startswith("/"):
            result = await handle_command(text, chat_id, self.store, self.cfg)
            if result is None:
                return
            if result.text == STOP_MARKER:
                cancelled = await self.lock.cancel(chat_id)
                await self.tg.send_message(
                    chat_id, "stopped" if cancelled else "nothing to stop"
                )
                return
            await self.tg.send_message(chat_id, result.text)
            return

        photo = largest_photo(message)
        image_data_url = None
        if photo is not None:
            image_data_url = await self._download_photo(photo.get("file_id"))
            if image_data_url is None:
                await self.tg.send_message(chat_id, "could not download that photo")
                return

        if not text and image_data_url is None:
            return

        if image_data_url is not None and not text:
            text = "Describe this image."

        placeholder_id = await self.tg.send_message(chat_id, PLACEHOLDER)
        task = await self.lock.replace(
            chat_id,
            self.runner.run(chat_id, text, self.tg, placeholder_id, image_data_url),
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _download_photo(self, file_id: str | None) -> str | None:
        if not file_id:
            return None
        try:
            return await self.tg.photo_data_url(file_id)
        except Exception as exc:
            log.warning("photo download failed: %s", exc)
            return None

    async def idle(self) -> None:
        while True:
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    # -- loop -------------------------------------------------------------

    async def run(self) -> None:
        last_health = 0.0
        last_prune = 0.0
        log.info("polling for updates")
        while True:
            try:
                updates = await self.tg.get_updates(self._offset, POLL_TIMEOUT)
                for update in updates:
                    self._offset = max(self._offset, int(update["update_id"]) + 1)
                    try:
                        await self.handle_update(update)
                    except Exception:
                        log.exception("update %s failed", update.get("update_id"))

                now = time.monotonic()
                if self.cfg.healthcheck_url and now - last_health > HEALTHCHECK_INTERVAL:
                    last_health = now
                    await self._healthcheck()
                if now - last_prune > PRUNE_INTERVAL:
                    last_prune = now
                    removed = await self.store.prune(self.cfg.retention_days)
                    if removed:
                        log.info("pruned %s messages older than %s days", removed,
                                 self.cfg.retention_days)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("poll failed: %s", exc)
                await asyncio.sleep(3)

    async def _healthcheck(self) -> None:
        url = self.cfg.healthcheck_url
        if not url:
            return
        try:
            await self.mimir.client.get(url, timeout=10)
        except Exception as exc:
            log.warning("healthcheck failed: %s", exc)

    async def shutdown(self) -> None:
        for chat_id in list(self.lock._tasks):
            with contextlib.suppress(BaseException):
                await self.lock.cancel(chat_id)
        await self.store.close()
