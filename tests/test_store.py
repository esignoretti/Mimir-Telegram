# tests/test_store.py
from mimir_bot.store import Store

async def make_store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    await s.init()
    return s

async def test_get_chat_creates_defaults(tmp_path):
    s = await make_store(tmp_path)
    chat = await s.get_chat(5)
    assert chat.model == "vllm/mimir-slim"
    assert chat.effort == "none"
    assert (await s.get_chat(5)).model == "vllm/mimir-slim"

async def test_set_model_and_effort_persist(tmp_path):
    s = await make_store(tmp_path)
    await s.get_chat(5)
    await s.set_model(5, "vllm/mimir-pro")
    await s.set_effort(5, "high")
    chat = await s.get_chat(5)
    assert chat.model == "vllm/mimir-pro"
    assert chat.effort == "high"

async def test_history_is_ordered_and_trimmed(tmp_path):
    s = await make_store(tmp_path)
    await s.add_message(5, "user", "a" * 460)      # ~100 tokens
    await s.add_message(5, "assistant", "b" * 460)  # ~100 tokens
    await s.add_message(5, "user", "c" * 460)      # ~100 tokens
    full = await s.history(5, 100_000)
    assert [m["content"][0] for m in full] == ["a", "b", "c"]
    trimmed = await s.history(5, 250)
    assert [m["content"][0] for m in trimmed] == ["b", "c"]

async def test_reset_chat_keeps_settings(tmp_path):
    s = await make_store(tmp_path)
    await s.get_chat(5)
    await s.set_model(5, "vllm/mimir-pro")
    await s.add_message(5, "user", "hello")
    await s.reset_chat(5)
    assert await s.history(5, 100_000) == []
    assert (await s.get_chat(5)).model == "vllm/mimir-pro"

async def test_prune_removes_old_messages(tmp_path):
    s = await make_store(tmp_path)
    await s.add_message(5, "user", "old")
    assert await s.prune(0) == 0        # 0 = keep forever
    assert await s.prune(90) == 0       # fresh row is not old
    assert len(await s.history(5, 100_000)) == 1


async def test_store_uses_configured_defaults(tmp_path):
    s = Store(str(tmp_path / "t.db"), "vllm/mimir-pro", "high")
    await s.init()
    chat = await s.get_chat(5)
    assert chat.model == "vllm/mimir-pro"
    assert chat.effort == "high"


async def test_delete_message_removes_only_that_row(tmp_path):
    s = await make_store(tmp_path)
    keep = await s.add_message(5, "user", "keep")
    drop = await s.add_message(5, "user", "drop")
    await s.delete_message(drop)
    hist = await s.history(5, 100_000)
    assert [m["content"] for m in hist] == ["keep"]
    assert keep != drop
