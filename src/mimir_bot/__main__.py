"""Entrypoint: wire the pieces and poll forever."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import httpx

from .app import App
from .config import Config
from .mimir import MimirClient
from .store import Store
from .telegram import Telegram


CONFIG_ERROR = 78  # EX_CONFIG: systemd must not restart on a bad .env


def _fail(message: str) -> None:
    """A configuration error is not transient: report it once and stop."""
    print(f"mimir-telegram: {message}", file=sys.stderr)
    raise SystemExit(CONFIG_ERROR)


async def main() -> None:
    try:
        cfg = Config.from_env(os.environ)
    except ValueError as exc:
        _fail(str(exc))
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("mimir_bot")

    # httpx logs every request URL at INFO, and those URLs contain the bot token
    # and the API key path. Keep the transport quiet so journald never sees it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=None,
                                                      write=30, pool=10)) as client:
        store = Store(cfg.db_path, cfg.default_model, cfg.default_effort)
        await store.init()
        app = App(cfg, store, Telegram(cfg.telegram_token, client),
                  MimirClient(cfg.mimir_base_url, cfg.mimir_api_key, client))
        try:
            await app.run()
        finally:
            log.info("shutting down")
            await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
