# tests/test_commands.py
import pytest
from mimir_bot.commands import handle_command, resolve_model, models_menu
from mimir_bot.config import Config, MODELS
from mimir_bot.store import Store

async def make(tmp_path):
    s = Store(str(tmp_path / "t.db")); await s.init()
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "MIMIR_API_KEY": "k",
                           "ALLOWED_TELEGRAM_USER_IDS": "1"})
    return s, cfg

def test_resolve_model_prefixes():
    assert resolve_model("slim", MODELS) == "vllm/mimir-slim"
    assert resolve_model("pro", MODELS) == "vllm/mimir-pro"
    assert resolve_model("mimir", MODELS) == "vllm/mimir"
    assert resolve_model("vllm/mimir-slim", MODELS) == "vllm/mimir-slim"
    assert resolve_model("nope", MODELS) is None

def test_models_menu_marks_current():
    menu = models_menu("vllm/mimir-slim", MODELS)
    assert "vllm/mimir-slim" in menu and "262144" in menu
    marked = [l for l in menu.splitlines() if "←" in l]
    assert len(marked) == 1 and "slim" in marked[0]

async def test_model_command_lists_when_no_arg(tmp_path):
    s, cfg = await make(tmp_path)
    r = await handle_command("/model", 1, s, cfg)
    assert r.handled and "vllm/mimir-slim" in r.text

async def test_model_command_switches(tmp_path):
    s, cfg = await make(tmp_path)
    r = await handle_command("/model pro", 1, s, cfg)
    assert "mimir-pro" in r.text
    assert (await s.get_chat(1)).model == "vllm/mimir-pro"

async def test_model_command_rejects_unknown_with_menu(tmp_path):
    s, cfg = await make(tmp_path)
    r = await handle_command("/model bogus", 1, s, cfg)
    assert "vllm/mimir-slim" in r.text
    assert (await s.get_chat(1)).model == "vllm/mimir-slim"

async def test_thinking_command_shows_and_sets(tmp_path):
    s, cfg = await make(tmp_path)
    shown = await handle_command("/thinking", 1, s, cfg)
    assert "none" in shown.text and "xhigh" in shown.text
    set_r = await handle_command("/thinking high", 1, s, cfg)
    assert "high" in set_r.text
    assert (await s.get_chat(1)).effort == "high"

async def test_thinking_rejects_invalid(tmp_path):
    s, cfg = await make(tmp_path)
    r = await handle_command("/thinking bogus", 1, s, cfg)
    assert "none" in r.text
    assert (await s.get_chat(1)).effort == "none"

async def test_new_and_stop_and_help(tmp_path):
    s, cfg = await make(tmp_path)
    await s.add_message(1, "user", "x")
    assert "new" in (await handle_command("/new", 1, s, cfg)).text.lower()
    assert await s.history(1, 100_000) == []
    assert (await handle_command("/stop", 1, s, cfg)).handled
    assert (await handle_command("/help", 1, s, cfg)).handled

async def test_non_command_returns_none(tmp_path):
    s, cfg = await make(tmp_path)
    assert await handle_command("hello there", 1, s, cfg) is None
