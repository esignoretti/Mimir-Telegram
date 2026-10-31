"""Minimal Telegram Bot API client.

Five methods is the whole surface this bot needs, so it talks to the HTTP API
directly instead of pulling in a framework: the poll loop, the cancel path and
the edit throttle all stay visible here rather than inside someone else's
dispatcher.
"""

from __future__ import annotations

import base64
import logging

import httpx

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org/bot{token}/{method}"

#: Telegram rejects edits whose text is unchanged; that is not an error here.
NOT_MODIFIED = "message is not modified"


class Telegram:
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self.token = token
        self.client = client

    async def _call(self, method: str, payload: dict, soft: bool = False) -> dict:
        url = API_ROOT.format(token=self.token, method=method)
        resp = await self.client.post(url, json=payload)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 200 and body.get("ok"):
            return body
        if soft and resp.status_code == 200:
            # 200 + ok=false is a transient condition (e.g. "Conflict" while a
            # previous long poll is still open); the poll loop just retries.
            log.debug("telegram %s: %s", method, body.get("description"))
            return body
        description = body.get("description") or resp.text
        raise RuntimeError(f"telegram {method} failed: {description}")

    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict]:
        body = await self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message"],
            },
            soft=True,
        )
        if not body.get("ok"):
            return []
        return body.get("result") or []

    async def send_message(
        self, chat_id: int, text: str, reply_to: int | None = None
    ) -> int:
        payload: dict = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        body = await self._call("sendMessage", payload)
        return int(body["result"]["message_id"])

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> bool:
        url = API_ROOT.format(token=self.token, method="editMessageText")
        resp = await self.client.post(
            url,
            json={"chat_id": chat_id, "message_id": message_id, "text": text},
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 200 and body.get("ok"):
            return True
        description = body.get("description") or resp.text
        if NOT_MODIFIED in description:
            return False
        raise RuntimeError(f"telegram editMessageText failed: {description}")

    async def send_typing(self, chat_id: int) -> None:
        try:
            await self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception as exc:  # best effort: never break a turn over a hint
            log.debug("sendChatAction failed: %s", exc)

    async def get_file_url(self, file_id: str) -> str:
        body = await self._call("getFile", {"file_id": file_id})
        file_path = (body.get("result") or {}).get("file_path")
        if not file_path:
            raise RuntimeError(f"telegram getFile returned no file_path for {file_id}")
        return f"https://api.telegram.org/file/bot{self.token}/{file_path}"

    async def photo_data_url(self, file_id: str) -> str:
        """Download a photo and inline it as a data URL the vision model accepts."""
        url = await self.get_file_url(file_id)
        resp = await self.client.get(url)
        resp.raise_for_status()
        mime = resp.headers.get("content-type") or "image/jpeg"
        encoded = base64.b64encode(resp.content).decode("ascii")
        return f"data:{mime};base64,{encoded}"
