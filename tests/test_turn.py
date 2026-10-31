# tests/test_turn.py
from mimir_bot.turn import TurnRunner, build_request
from mimir_bot.mimir import StreamEvent

class FakeMimir:
    def __init__(self, events): self.events = events; self.calls = []
    async def stream(self, messages, model, effort, max_tokens=None):
        self.calls.append({"messages": messages, "model": model, "effort": effort})
        for e in self.events: yield e

class FakeTG:
    def __init__(self): self.edits = []; self.sent = []
    async def edit_message(self, chat_id, message_id, text):
        self.edits.append(text); return True
    async def send_message(self, chat_id, text, reply_to=None):
        self.sent.append(text); return 1

def test_build_request_prepends_system_prompt():
    h = [{"role": "user", "content": "hi"}]
    assert build_request(h, "") == h
    assert build_request(h, "be terse")[0] == {"role": "system", "content": "be terse"}

async def test_run_persists_only_answer_text(tmp_path):
    from mimir_bot.store import Store
    from mimir_bot.config import Config
    store = Store(str(tmp_path / "t.db")); await store.init()
    mimir = FakeMimir([StreamEvent("reasoning", "secret thoughts"),
                       StreamEvent("content", "final answer")])
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    runner = TurnRunner(store, mimir, cfg)
    answer = await runner.run(7, "question", FakeTG(), 10)
    assert answer == "final answer"
    hist = await store.history(7, 100_000)
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[1]["content"] == "final answer"
    assert "secret thoughts" not in hist[1]["content"]

async def test_run_uses_chat_model_and_effort(tmp_path):
    from mimir_bot.store import Store
    from mimir_bot.config import Config
    store = Store(str(tmp_path / "t.db")); await store.init()
    await store.get_chat(7); await store.set_model(7, "vllm/mimir-pro"); await store.set_effort(7, "high")
    mimir = FakeMimir([StreamEvent("content", "ok")])
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    await TurnRunner(store, mimir, cfg).run(7, "q", FakeTG(), 10)
    assert mimir.calls[0]["model"] == "vllm/mimir-pro"
    assert mimir.calls[0]["effort"] == "high"

async def test_run_sends_error_text_and_persists_nothing(tmp_path):
    from mimir_bot.store import Store
    from mimir_bot.config import Config
    store = Store(str(tmp_path / "t.db")); await store.init()
    class Boom:
        async def stream(self, *a, **k):
            raise RuntimeError("no keys found that support model/deployment: nope")
            yield
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    tg = FakeTG()
    answer = await TurnRunner(store, Boom(), cfg).run(7, "q", tg, 10)
    assert answer == ""
    assert "nope" in tg.edits[-1]
    assert await store.history(7, 100_000) == []


def test_token_budget_is_capped_by_the_model_window():
    from mimir_bot.turn import token_budget, RESPONSE_HEADROOM
    # mimir-vision has the smallest window; a 200k configured budget must shrink.
    assert token_budget("vllm/mimir-vision", 200_000) == 131_072 - RESPONSE_HEADROOM
    assert token_budget("vllm/mimir", 200_000) == 200_000
    assert token_budget("unknown/model", 200_000) == 200_000


async def test_reasoning_only_turn_persists_a_visible_placeholder(tmp_path):
    from mimir_bot.store import Store
    from mimir_bot.config import Config
    store = Store(str(tmp_path / "t.db")); await store.init()
    mimir = FakeMimir([StreamEvent("reasoning", "thoughts")])
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    await TurnRunner(store, mimir, cfg).run(7, "q", FakeTG(), 10)
    hist = await store.history(7, 100_000)
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[1]["content"].strip() != ""


async def test_cancel_after_first_event_drops_the_user_row(tmp_path):
    import asyncio
    from mimir_bot.store import Store
    from mimir_bot.config import Config
    store = Store(str(tmp_path / "t.db")); await store.init()
    class Slow:
        async def stream(self, messages, model, effort, max_tokens=None):
            yield StreamEvent("content", "partial")
            await asyncio.sleep(10)
            yield StreamEvent("content", "never")
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    task = asyncio.create_task(TurnRunner(store, Slow(), cfg).run(7, "q", FakeTG(), 10))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert await store.history(7, 100_000) == []


async def test_failed_turn_drops_the_user_row(tmp_path):
    from mimir_bot.store import Store
    from mimir_bot.config import Config
    store = Store(str(tmp_path / "t.db")); await store.init()
    class HalfBroken:
        async def stream(self, messages, model, effort, max_tokens=None):
            yield StreamEvent("content", "start")
            raise RuntimeError("connection reset")
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    tg = FakeTG()
    await TurnRunner(store, HalfBroken(), cfg).run(7, "q", tg, 10)
    assert await store.history(7, 100_000) == []
    assert "connection reset" in tg.edits[-1]


async def test_lock_forgets_finished_tasks(tmp_path):
    import asyncio
    from mimir_bot.turn import GenerationLock
    lock = GenerationLock()
    async def work(): return 1
    await lock.replace(7, work())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert lock.chat_ids() == []
    assert lock.current(7) is None
