# tests/test_telegram.py
import httpx, pytest
from mimir_bot.telegram import Telegram, RateLimited

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


async def test_errors_never_leak_the_token():
    def handler(req):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    tg = make(handler)
    with pytest.raises(RuntimeError) as exc:
        await tg.get_updates(offset=0)
    assert "123:abc" not in str(exc.value)

    def boom(req):
        raise httpx.ConnectError(f"failed to reach {req.url}")
    tg2 = make(boom)
    with pytest.raises(RuntimeError) as exc2:
        await tg2.send_message(1, "hi")
    assert "123:abc" not in str(exc2.value)


async def test_photo_download_failure_does_not_leak_the_token():
    def handler(req):
        if "/getFile" in str(req.url):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/x.jpg"}})
        return httpx.Response(404, text="not found")
    tg = make(handler)
    with pytest.raises(RuntimeError) as exc:
        await tg.photo_data_url("abc")
    assert "123:abc" not in str(exc.value)
    assert "photos/x.jpg" not in str(exc.value)

async def test_photo_download_transport_error_does_not_leak_the_token():
    def handler(req):
        if "/getFile" in str(req.url):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/x.jpg"}})
        raise httpx.ConnectError(f"failed to reach {req.url}")
    tg = make(handler)
    with pytest.raises(RuntimeError) as exc:
        await tg.photo_data_url("abc")
    assert "123:abc" not in str(exc.value)

async def test_photo_data_url_inlines_bytes():
    def handler(req):
        if "/getFile" in str(req.url):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "p.jpg"}})
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
    url = await make(handler).photo_data_url("abc")
    assert url.startswith("data:image/png;base64,")

async def test_edit_message_reports_rate_limit_with_retry_after():
    def handler(req):
        return httpx.Response(429, json={"ok": False, "description": "Too Many Requests",
            "parameters": {"retry_after": 7}})
    with pytest.raises(RateLimited) as exc:
        await make(handler).edit_message(1, 2, "x")
    assert exc.value.retry_after == 7
    assert "123:abc" not in str(exc.value)
