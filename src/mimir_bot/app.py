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
#: Backoff after a failed poll, so a down API is not hammered.
POLL_RETRY_SECONDS = 3
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
        self._clock = time.monotonic

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
        task.add_done_callback(self._forget)

    def _forget(self, task: asyncio.Task) -> None:
        """Retrieve the outcome so a crash inside a turn is logged, not swallowed.

        Without this, an exception escaping TurnRunner would surface only as
        asyncio's "Task exception was never retrieved" at GC time.
        """
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("turn crashed: %s", exc, exc_info=exc)

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
        # Start the maintenance clock at "due now": monotonic() is seconds since
        # boot, so a 0.0 sentinel would silently skip pruning for the first day.
        now = self._clock()
        last_health = now
        last_prune = now - PRUNE_INTERVAL
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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("poll failed: %s", exc)
                await asyncio.sleep(POLL_RETRY_SECONDS)

            # Maintenance runs whether or not the poll succeeded: a wedged
            # long-poll would otherwise also stop pruning and health checks.
            # Guarded so a future addition here cannot crash the service.
            try:
                last_health, last_prune = await self._maintenance(last_health, last_prune)
            except Exception as exc:
                log.warning("maintenance failed: %s", exc)

    async def _maintenance(self, last_health: float, last_prune: float) -> tuple[float, float]:
        now = self._clock()
        if self.cfg.healthcheck_url and now - last_health > HEALTHCHECK_INTERVAL:
            last_health = now
            await self._healthcheck()
        if now - last_prune >= PRUNE_INTERVAL:
            last_prune = now
            try:
                removed = await self.store.prune(self.cfg.retention_days)
            except Exception as exc:
                log.warning("prune failed: %s", exc)
            else:
                if removed:
                    log.info(
                        "pruned %s messages older than %s days",
                        removed,
                        self.cfg.retention_days,
                    )
        return last_health, last_prune

    async def _healthcheck(self) -> None:
        url = self.cfg.healthcheck_url
        if not url:
            return
        try:
            await self.mimir.client.get(url, timeout=10)
        except Exception as exc:
            log.warning("healthcheck failed: %s", exc)

    async def shutdown(self) -> None:
        for chat_id in self.lock.chat_ids():
            with contextlib.suppress(BaseException):
                await self.lock.cancel(chat_id)
        await self.store.close()
