# tests/test_telegram.py
import httpx, pytest
from mimir_bot.telegram import Telegram

def make(handler):
    return Telegram("123:abc", httpx.AsyncClient(transport=httpx.MockTransport(handler)))

async def test_get_updates_posts_offset_and_returns_result():
    seen = {}
    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = req.read()
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 7}]})
    updates = await make(handler).get_updates(offset=6)
    assert updates == [{"update_id": 7}]
    assert seen["url"].endswith("/getUpdates")
    assert b'"offset":6' in seen["body"]

async def test_get_updates_returns_empty_on_not_ok():
    def handler(req):
        return httpx.Response(200, json={"ok": False, "description": "Conflict"})
    assert await make(handler).get_updates(offset=0) == []

async def test_edit_message_reports_not_modified_as_false():
    def handler(req):
        return httpx.Response(400, json={"ok": False, "description":
            "Bad Request: message is not modified"})
    assert await make(handler).edit_message(1, 2, "x") is False

async def test_send_message_returns_message_id():
    def handler(req):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
    assert await make(handler).send_message(1, "hi") == 42
