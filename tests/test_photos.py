# tests/test_photos.py
import base64
from mimir_bot.turn import image_content, TurnRunner
from mimir_bot.mimir import StreamEvent
from mimir_bot.store import Store
from mimir_bot.config import Config

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
DATA_URL = "data:image/png;base64," + PNG

def test_image_content_shape():
    c = image_content("what is this?", DATA_URL)
    assert c[0] == {"type": "text", "text": "what is this?"}
    assert c[1]["type"] == "image_url"
    assert c[1]["image_url"]["url"] == DATA_URL

async def test_photo_turn_uses_vision_model_without_changing_chat(tmp_path):
    store = Store(str(tmp_path / "t.db")); await store.init()
    await store.get_chat(3)
    class M:
        def __init__(self): self.models = []
        async def stream(self, messages, model, effort, max_tokens=None):
            self.models.append(model)
            yield StreamEvent("content", "a red square")
    class TG:
        async def edit_message(self, c, m, t): return True
        async def send_message(self, c, t, reply_to=None): return 1
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    m = M()
    await TurnRunner(store, m, cfg).run(3, "what is this?", TG(), 10, image_data_url=DATA_URL)
    assert m.models == ["vllm/mimir-vision"]
    assert (await store.get_chat(3)).model == "vllm/mimir-slim"

async def test_photo_history_stores_plain_text(tmp_path):
    store = Store(str(tmp_path / "t.db")); await store.init()
    class M:
        async def stream(self, messages, model, effort, max_tokens=None):
            assert isinstance(messages[-1]["content"], list)
            yield StreamEvent("content", "a red square")
    class TG:
        async def edit_message(self, c, m, t): return True
        async def send_message(self, c, t, reply_to=None): return 1
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    await TurnRunner(store, M(), cfg).run(3, "what is this?", TG(), 10, image_data_url=DATA_URL)
    hist = await store.history(3, 100_000)
    assert isinstance(hist[0]["content"], str)
    assert hist[0]["content"].startswith("what is this?")


async def test_get_file_url_and_data_url():
    import httpx
    from mimir_bot.telegram import Telegram

    def handler(req):
        if str(req.url).endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/a.jpg"}})
        return httpx.Response(200, content=b"JPEGBYTES", headers={"content-type": "image/jpeg"})

    tg = Telegram("1:a", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await tg.get_file_url("f1") == "https://api.telegram.org/file/bot1:a/photos/a.jpg"
    assert (await tg.photo_data_url("f1")).startswith("data:image/jpeg;base64,")


async def test_photo_message_reaches_the_vision_model(tmp_path):
    from mimir_bot.app import App

    edits = []
    seen = []

    class TG:
        async def edit_message(self, c, m, t): edits.append(t); return True
        async def send_message(self, c, t, reply_to=None): edits.append(t); return 1
        async def photo_data_url(self, file_id): return DATA_URL

    class M:
        async def stream(self, messages, model, effort, max_tokens=None):
            seen.append((model, messages[-1]["content"]))
            yield StreamEvent("content", "a red square")

    store = Store(str(tmp_path / "t.db")); await store.init()
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    app = App(cfg, store, TG(), M())
    update = {"update_id": 1, "message": {
        "message_id": 5, "chat": {"id": 100}, "from": {"id": 1},
        "caption": "what is this?",
        "photo": [{"file_id": "small", "file_size": 10},
                  {"file_id": "big", "file_size": 999}],
    }}
    await app.handle_update(update)
    await app.idle()

    model, content = seen[0]
    assert model == "vllm/mimir-vision"
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["image_url"]["url"] == DATA_URL
    assert any("red square" in e for e in edits)
    hist = await store.history(100, 100_000)
    assert hist[0] == {"role": "user", "content": "what is this? [image]"}
