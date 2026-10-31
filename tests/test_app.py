# tests/test_app.py
import asyncio
import pytest
from mimir_bot.app import App, is_allowed
from mimir_bot.config import Config
from mimir_bot.store import Store

def upd(user_id, text, chat_id=100):
    return {"update_id": 1, "message": {"message_id": 5, "chat": {"id": chat_id},
            "from": {"id": user_id}, "text": text}}

def test_is_allowed():
    assert is_allowed(upd(1, "hi"), frozenset({1}))
    assert not is_allowed(upd(2, "hi"), frozenset({1}))
    assert not is_allowed({"update_id": 2}, frozenset({1}))

async def build(tmp_path, edits):
    class TG:
        async def edit_message(self, c, m, t): edits.append(t); return True
        async def send_message(self, c, t, reply_to=None): edits.append(t); return 1
    class M:
        async def stream(self, messages, model, effort, max_tokens=None):
            from mimir_bot.mimir import StreamEvent
            await asyncio.sleep(0.05)
            yield StreamEvent("content", "answer")
    store = Store(str(tmp_path / "t.db")); await store.init()
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    return App(cfg, store, TG(), M()), store

async def test_disallowed_user_gets_nothing(tmp_path):
    edits = []
    app, _ = await build(tmp_path, edits)
    await app.handle_update(upd(999, "hi"))
    assert edits == []

async def test_plain_message_starts_turn(tmp_path):
    edits = []
    app, store = await build(tmp_path, edits)
    await app.handle_update(upd(1, "question"))
    await app.idle()
    assert any("answer" in e for e in edits)
    assert len(await store.history(100, 100_000)) == 2

async def test_command_does_not_start_turn(tmp_path):
    edits = []
    app, store = await build(tmp_path, edits)
    await app.handle_update(upd(1, "/thinking high"))
    assert any("high" in e for e in edits)
    assert await store.history(100, 100_000) == []

async def test_second_message_interrupts_first(tmp_path):
    edits = []
    app, store = await build(tmp_path, edits)
    await app.handle_update(upd(1, "first"))
    await asyncio.sleep(0.01)  # the poll loop yields between updates; so must we
    await app.handle_update(upd(1, "second"))
    await app.idle()
    assert "[interrupted]" in edits
    hist = await store.history(100, 100_000)
    assert all(m["content"] != "first" for m in hist)
    assert any(m["content"] == "answer" for m in hist)
