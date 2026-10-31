# tests/test_app.py
import asyncio
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


async def test_turn_crash_is_logged_not_swallowed(tmp_path, caplog):
    import logging

    class TG:
        async def edit_message(self, c, m, t): return True
        async def send_message(self, c, t, reply_to=None): return 1

    class M:
        async def stream(self, messages, model, effort, max_tokens=None):
            from mimir_bot.mimir import StreamEvent
            yield StreamEvent("content", "answer")

    class BoomStore(Store):
        """Fails while persisting the answer, i.e. after the stream succeeded."""
        async def add_message(self, chat_id, role, content, model=None):
            if role == "assistant":
                raise RuntimeError("disk full")
            await super().add_message(chat_id, role, content, model)

    store = BoomStore(str(tmp_path / "t.db")); await store.init()
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    app = App(cfg, store, TG(), M())
    with caplog.at_level(logging.ERROR, logger="mimir_bot.app"):
        await app.handle_update(upd(1, "question"))
        await app.idle()
    messages = [r.getMessage() for r in caplog.records]
    assert any("turn crashed" in m and "disk full" in m for m in messages)


async def test_second_message_interrupts_after_streaming_started(tmp_path):
    """The real case: cancel lands after the turn has produced an event."""
    edits = []
    class TG:
        async def edit_message(self, c, m, t): edits.append(t); return True
        async def send_message(self, c, t, reply_to=None): edits.append(t); return 1
    class M:
        """First call stalls after one event; the second answers immediately."""
        def __init__(self): self.calls = 0
        async def stream(self, messages, model, effort, max_tokens=None):
            from mimir_bot.mimir import StreamEvent
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent("content", "first-partial")
                await asyncio.sleep(5)
                yield StreamEvent("content", "never")
            else:
                yield StreamEvent("content", "second-answer")
    store = Store(str(tmp_path / "t.db")); await store.init()
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    app = App(cfg, store, TG(), M())
    await app.handle_update(upd(1, "first"))
    await asyncio.sleep(0.05)
    await app.handle_update(upd(1, "second"))
    await app.idle()
    hist = await store.history(100, 100_000)
    assert [m["content"] for m in hist] == ["second", "second-answer"]
    assert "[interrupted]" in edits


async def test_edit_failure_does_not_lose_the_answer(tmp_path):
    from mimir_bot.mimir import StreamEvent
    class TG:
        def __init__(self): self.sent = []
        async def edit_message(self, c, m, t):
            raise RuntimeError("telegram editMessageText failed: Bad Request")
        async def send_message(self, c, t, reply_to=None): self.sent.append(t); return 1
    class M:
        async def stream(self, messages, model, effort, max_tokens=None):
            yield StreamEvent("content", "the answer")
    store = Store(str(tmp_path / "t.db")); await store.init()
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    tg = TG()
    app = App(cfg, store, tg, M())
    await app.handle_update(upd(1, "question"))
    await app.idle()
    assert any("the answer" in s for s in tg.sent)
    hist = await store.history(100, 100_000)
    assert hist[-1]["content"] == "the answer"


async def test_prune_runs_even_when_polling_fails(tmp_path, monkeypatch):
    """A wedged long-poll must not stop maintenance."""
    pruned = []
    class TG:
        async def get_updates(self, offset, timeout=30):
            raise RuntimeError("network is down")
        async def edit_message(self, c, m, t): return True
        async def send_message(self, c, t, reply_to=None): return 1
    class M:
        async def stream(self, *a, **k):
            return
            yield
    class CountingStore(Store):
        async def prune(self, days):
            pruned.append(days)
            return 0
    store = CountingStore(str(tmp_path / "t.db")); await store.init()
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    app = App(cfg, store, TG(), M())
    from mimir_bot import app as app_mod
    monkeypatch.setattr(app_mod, "POLL_RETRY_SECONDS", 0)
    task = asyncio.create_task(app.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert pruned == [90]


async def test_maintenance_prunes_on_a_freshly_booted_host(tmp_path):
    """monotonic() counts from boot; a 0.0 sentinel skipped pruning for a day."""
    pruned = []

    class TG:
        async def edit_message(self, c, m, t): return True
        async def send_message(self, c, t, reply_to=None): return 1

    class M:
        async def stream(self, *a, **k):
            return
            yield

    class CountingStore(Store):
        async def prune(self, days):
            pruned.append(days)
            return 0

    store = CountingStore(str(tmp_path / "t.db"))
    await store.init()
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    app = App(cfg, store, TG(), M())
    # A host that booted five seconds ago.
    app._clock = lambda: 5.0
    last_health = 5.0
    last_prune = 5.0 - 86_400  # what run() now initialises
    await app._maintenance(last_health, last_prune)
    assert pruned == [90]
