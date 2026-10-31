# Mimir Telegram Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A long-polling Telegram bot on `hygro.signocloud.it` that streams Mimir's answers into Telegram messages, keeps per-chat conversation history in SQLite, and exposes `/model` and `/thinking` per chat.

**Architecture:** One asyncio Python process. A poll loop pulls updates from the Telegram Bot API; each allowed chat message becomes a turn: history is loaded from SQLite, posted to `https://mimir.cubbit.dev/v1/chat/completions` with `stream: true`, and deltas are rendered into a single Telegram message via throttled edits. One global generation lock; a second message interrupts. No inbound ports.

**Tech Stack:** Python 3.12, `httpx` (async, SSE), `aiosqlite`, `pytest` + `pytest-asyncio`, stdlib `logging`. No bot framework — the Bot API surface used here is five endpoints, and raw `httpx` keeps the update loop, the cancel path, and the edit throttle under our control. Deployed as a systemd unit with a venv.

**Spec:** `docs/superpowers/specs/2026-10-31-mimir-telegram-design.md`

## Global Constraints

- Python 3.12; venv at `/opt/mimir-telegram/venv`; service user `mimir` (non-root).
- Mimir base URL: `https://mimir.cubbit.dev/v1`, auth `Authorization: Bearer $MIMIR_API_KEY`.
- Models and their `max_model_len`: `vllm/mimir` 1048576 · `vllm/mimir-pro` 1048576 · `vllm/mimir-slim` 262144 · `vllm/mimir-vision` 131072.
- Default model `vllm/mimir-slim`; default `reasoning_effort` `"none"`.
- Valid `reasoning_effort` values, exactly: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`.
- Reasoning arrives in `delta.reasoning` (mirror `delta.reasoning_details[].text`); answer text arrives in `delta.content`. Reasoning is never persisted or resent.
- Final SSE chunk has `created: 0`, `model: ""`, empty `delta: {}`, and carries `usage`. Parse leniently.
- Token estimate: `len(text) / 4.6` characters per token. Budget threshold default 200000 tokens.
- Telegram: 4096-char message cap; edits throttled to ≥1.5s apart and only when text changed; 429 responses are retried after `retry_after`.
- No inbound ports. Only outbound HTTPS to `api.telegram.org` and `mimir.cubbit.dev`.
- On `hygro.signocloud.it` never touch: `esp32-hygro.service`, `/opt/ESP32-hygro`, `/opt/Signocloud-IOT`, PostgreSQL, `/etc/caddy/Caddyfile`, ports 80/443/8000/5432/2019.

## Review Focus

- **Model with no reasoning support** (`vllm/mimir-decide`-style, or any model that returns `content` with no `reasoning` field): must render the answer immediately, not sit on a "thinking…" placeholder. Tested in Task 5.
- **A model that returns reasoning but no content at all** (e.g. hits `max_tokens` during thinking): must not leave a permanently empty message; the message must end in a visible state. Tested in Task 5.
- **Reasoning never leaking into history**: after a turn, the stored assistant message must contain only answer text. Tested in Task 6.
- **SSE chunk with empty `delta` and empty `choices`-adjacent shapes** (the final usage chunk, keepalive comments): must not raise. Tested in Task 4.
- **Answer longer than 4096 chars**: must be split into multiple messages without losing or duplicating text. Tested in Task 7.

---

### Task 1: Project scaffold and configuration

**Files:**
- Create: `pyproject.toml`
- Create: `src/mimir_bot/__init__.py`
- Create: `src/mimir_bot/config.py`
- Create: `tests/test_config.py`
- Create: `.gitignore`
- Create: `.env.example`

**Interfaces:**
- Consumes: nothing.
- Produces: `Config` dataclass with fields `telegram_token: str`, `mimir_api_key: str`, `mimir_base_url: str`, `allowed_user_ids: frozenset[int]`, `default_model: str`, `default_effort: str`, `db_path: str`, `system_prompt: str`, `notify_after_seconds: int`, `history_token_budget: int`, `retention_days: int`, `healthcheck_url: str | None`, `log_level: str`. Also `Config.from_env(env: Mapping[str, str]) -> Config` and module constant `MODELS: dict[str, int]` mapping model id → `max_model_len`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import pytest
from mimir_bot.config import Config, MODELS

BASE = {
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "MIMIR_API_KEY": "sk-mimir-x",
    "ALLOWED_TELEGRAM_USER_IDS": "111, 222",
}

def test_from_env_applies_defaults():
    cfg = Config.from_env(BASE)
    assert cfg.mimir_base_url == "https://mimir.cubbit.dev/v1"
    assert cfg.default_model == "vllm/mimir-slim"
    assert cfg.default_effort == "none"
    assert cfg.notify_after_seconds == 60
    assert cfg.history_token_budget == 200_000
    assert cfg.retention_days == 90
    assert cfg.healthcheck_url is None
    assert cfg.system_prompt == ""
    assert cfg.allowed_user_ids == frozenset({111, 222})

def test_from_env_requires_token_and_key():
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        Config.from_env({"MIMIR_API_KEY": "k", "ALLOWED_TELEGRAM_USER_IDS": "1"})

def test_from_env_rejects_empty_allowlist():
    env = dict(BASE, ALLOWED_TELEGRAM_USER_IDS="")
    with pytest.raises(ValueError, match="ALLOWED_TELEGRAM_USER_IDS"):
        Config.from_env(env)

def test_models_table_matches_spec():
    assert MODELS == {
        "vllm/mimir": 1048576,
        "vllm/mimir-pro": 1048576,
        "vllm/mimir-slim": 262144,
        "vllm/mimir-vision": 131072,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_bot'`

- [ ] **Step 3: Implement `Config` and `MODELS` in `src/mimir_bot/config.py`**

`Config.from_env` is a classmethod taking a `Mapping[str, str]` (so tests never touch `os.environ`), raising `ValueError` naming the offending variable. `allowed_user_ids` parses a comma-separated list of ints, ignoring blanks. `notify_after_seconds`, `history_token_budget`, `retention_days` parse as int; `healthcheck_url` is `None` when unset or empty. `pyproject.toml` sets `[tool.pytest.ini_options] pythonpath = ["src"]` and `asyncio_mode = "auto"`; dependencies are `httpx`, `aiosqlite`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git init && git add -A && git commit -m "feat: project scaffold and config loading"
```

---

### Task 2: SQLite store

**Files:**
- Create: `src/mimir_bot/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `Config.db_path` from Task 1.
- Produces:
  - `class Store` with `Store(path: str)`, `async def init() -> None`, `async def close() -> None`.
  - `@dataclass ChatSettings(chat_id: int, model: str, effort: str)`.
  - `async def get_chat(chat_id: int) -> ChatSettings` (creates the row from defaults on first use).
  - `async def set_model(chat_id: int, model: str) -> None`, `async def set_effort(chat_id: int, effort: str) -> None`.
  - `async def reset_chat(chat_id: int) -> None` (deletes messages, keeps settings).
  - `async def add_message(chat_id: int, role: str, content: str, model: str | None = None) -> None`.
  - `async def history(chat_id: int, token_budget: int) -> list[dict[str, str]]` — returns `[{"role": ..., "content": ...}]`, oldest first, trimmed from the oldest end so the estimated total stays under `token_budget` (estimate = `len(content) / 4.6`).
  - `async def prune(retention_days: int) -> int` — deletes messages older than the cutoff, returns count.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
import pytest
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_bot.store'`

- [ ] **Step 3: Implement `Store` in `src/mimir_bot/store.py`**

Tables: `chats(chat_id INTEGER PRIMARY KEY, model TEXT NOT NULL, effort TEXT NOT NULL)` and `messages(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, model TEXT, ts REAL NOT NULL DEFAULT (unixepoch('subsec')))` with an index on `(chat_id, id)`. `init()` runs `CREATE TABLE IF NOT EXISTS`. Trim logic walks messages newest→oldest accumulating the estimate and stops before exceeding the budget. `prune(0)` returns 0 without deleting.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_store.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: sqlite store for chats, settings and history"
```

---

### Task 3: Telegram client

**Files:**
- Create: `src/mimir_bot/telegram.py`
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: `Config.telegram_token`.
- Produces:
  - `class Telegram` with `Telegram(token: str, client: httpx.AsyncClient)`.
  - `async def get_updates(offset: int, timeout: int = 30) -> list[dict]`.
  - `async def send_message(chat_id: int, text: str, reply_to: int | None = None) -> int` (returns `message_id`).
  - `async def edit_message(chat_id: int, message_id: int, text: str) -> bool` (returns `False` when Telegram rejects the edit as "message is not modified", `True` otherwise; raises on other errors).
  - `async def send_typing(chat_id: int) -> None` (best-effort, swallows errors).

- [ ] **Step 1: Write the failing test**

```python
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
    assert b'"offset": 6' in seen["body"]

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_telegram.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_bot.telegram'`

- [ ] **Step 3: Implement `Telegram` in `src/mimir_bot/telegram.py`**

Base URL `https://api.telegram.org/bot{token}/{method}`, JSON body via `httpx`, `timeout=None` on the client for long polls (the caller sets the poll timeout). `edit_message` inspects the 400 body for `"message is not modified"` and returns `False`; any other non-200 raises `RuntimeError` with the description. `send_message` does no splitting — that is `split_message` in Task 5, which the renderer applies.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_telegram.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: minimal telegram bot api client"
```

---

### Task 4: Mimir streaming client

**Files:**
- Create: `src/mimir_bot/mimir.py`
- Test: `tests/test_mimir.py`

**Interfaces:**
- Consumes: `Config.mimir_api_key`, `Config.mimir_base_url`.
- Produces:
  - `@dataclass StreamEvent(kind: str, text: str)` where `kind` is `"reasoning"`, `"content"`, or `"usage"`.
  - `class MimirClient` with `MimirClient(base_url: str, api_key: str, client: httpx.AsyncClient)`.
  - `async def stream(messages: list[dict], model: str, effort: str, max_tokens: int | None = None) -> AsyncIterator[StreamEvent]`.
  - `def parse_sse_line(line: str) -> dict | None` — module-level, returns the decoded JSON payload or `None` for blanks, comments, and `[DONE]`.
  - `def describe_error(status: int, body: str) -> str` — module-level, turns an error response into a one-line human message (handles `no keys found that support model/deployment: X` and `max_completion_tokens=... cannot be greater than max_model_len=...`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mimir.py
import httpx, pytest
from mimir_bot.mimir import MimirClient, parse_sse_line, describe_error

SSE = (
    'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n'
    'data: {"choices":[{"delta":{"reasoning":"hm","reasoning_details":[{"index":0,"type":"reasoning.text","text":"hm"}]}}]}\n'
    ': keepalive\n'
    'data: {"choices":[{"delta":{"content":"Hello"}}]}\n'
    'data: {"choices":[{"delta":{}}],"created":0,"model":"","usage":{"prompt_tokens":5,"completion_tokens":2}}\n'
    'data: [DONE]\n'
)

def test_parse_sse_line_skips_noise():
    assert parse_sse_line("") is None
    assert parse_sse_line(": keepalive") is None
    assert parse_sse_line("data: [DONE]") is None
    assert parse_sse_line('data: {"a":1}') == {"a": 1}

async def test_stream_yields_reasoning_then_content_then_usage():
    def handler(req):
        return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})
    client = MimirClient("https://x/v1", "k", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    events = [e async for e in client.stream([{"role": "user", "content": "hi"}], "vllm/mimir-slim", "none")]
    assert [(e.kind, e.text) for e in events] == [
        ("reasoning", "hm"),
        ("content", "Hello"),
        ("usage", '{"prompt_tokens": 5, "completion_tokens": 2}'),
    ]

async def test_stream_tolerates_empty_delta_and_empty_choices():
    body = 'data: {"choices":[]}\n\ndata: {"choices":[{"delta":{}}]}\n\ndata: [DONE]\n'
    def handler(req):
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    client = MimirClient("https://x/v1", "k", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert [e async for e in client.stream([{"role": "user", "content": "hi"}], "vllm/mimir-slim", "none")] == []

def test_describe_error_model_and_max_tokens():
    assert "nope" in describe_error(400, '{"error":{"error":"no keys found that support model/deployment: nope"}}')
    msg = describe_error(400, '{"error":{"message":"max_completion_tokens=999999 cannot be greater than max_model_len=262144."}}')
    assert "262144" in msg

async def test_stream_raises_with_description_on_http_error():
    def handler(req):
        return httpx.Response(400, json={"error": {"error": "no keys found that support model/deployment: nope"}})
    client = MimirClient("https://x/v1", "k", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="nope"):
        [e async for e in client.stream([{"role": "user", "content": "hi"}], "vllm/nope", "none")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mimir.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_bot.mimir'`

- [ ] **Step 3: Implement `MimirClient` in `src/mimir_bot/mimir.py`**

`stream` posts `{"model", "messages", "stream": True, "reasoning_effort"}` plus `max_tokens` when given, then iterates `response.aiter_lines()`, feeding each line to `parse_sse_line`. For each payload: `delta.get("reasoning")` → `StreamEvent("reasoning", ...)`; `delta.get("content")` → `StreamEvent("content", ...)`; a payload with a `usage` key and no `choices` → `StreamEvent("usage", json.dumps(usage))`. Non-2xx: read the body and raise `RuntimeError(describe_error(status, body))`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mimir.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: streaming mimir client with lenient sse parsing"
```

---

### Task 5: Renderer

**Files:**
- Create: `src/mimir_bot/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `StreamEvent` from Task 4.
- Produces:
  - `class TurnRenderer` with `TurnRenderer(chat_id: int, tg: Telegram, placeholder_id: int, edit_interval: float = 1.5, notify_after: int = 60)`.
  - `async def on_event(event: StreamEvent) -> None` — accumulates and throttles edits.
  - `async def finish(final_text: str) -> None` — performs the last edit, then the notification message if the turn exceeded the notify threshold.
  - `async def interrupt() -> None` — edits the message to `[interrupted]`.
  - `def thinking_label(reasoning_chars: int) -> str` — module-level; returns `"thinking…"` under 200 chars, else `f"thinking… ({reasoning_chars} chars)"`.
  - `def split_message(text: str, limit: int = 4096) -> list[str]` — module-level; splits on paragraph boundaries where possible, never loses characters (the concatenation of the parts equals the input).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_render.py
import pytest
from mimir_bot.render import TurnRenderer, split_message, thinking_label
from mimir_bot.mimir import StreamEvent

class FakeTG:
    def __init__(self): self.edits = []; self.sent = []
    async def edit_message(self, chat_id, message_id, text):
        self.edits.append(text); return True
    async def send_message(self, chat_id, text, reply_to=None):
        self.sent.append((text, reply_to)); return 999

async def test_reasoning_only_shows_thinking_label():
    tg = FakeTG()
    r = TurnRenderer(1, tg, 10, edit_interval=0)
    await r.on_event(StreamEvent("reasoning", "x" * 250))
    await r.on_event(StreamEvent("reasoning", "y" * 50))
    assert tg.edits[-1] == thinking_label(300)
    assert "300" in tg.edits[-1]

async def test_content_replaces_thinking_label():
    tg = FakeTG()
    r = TurnRenderer(1, tg, 10, edit_interval=0)
    await r.on_event(StreamEvent("reasoning", "thinking hard"))
    await r.on_event(StreamEvent("content", "The answer"))
    assert tg.edits[-1] == "The answer"

async def test_model_without_reasoning_renders_content_immediately():
    tg = FakeTG()
    r = TurnRenderer(1, tg, 10, edit_interval=0)
    await r.on_event(StreamEvent("content", "Hi!"))
    assert tg.edits == ["Hi!"]

async def test_reasoning_with_no_content_finishes_visibly():
    tg = FakeTG()
    r = TurnRenderer(1, tg, 10, edit_interval=0)
    await r.on_event(StreamEvent("reasoning", "thinking"))
    await r.finish(final_text="")
    assert "no answer" in tg.edits[-1].lower()

async def test_interrupt_marks_message():
    tg = FakeTG()
    r = TurnRenderer(1, tg, 10, edit_interval=0)
    await r.on_event(StreamEvent("content", "partial"))
    await r.interrupt()
    assert tg.edits[-1] == "[interrupted]"

async def test_notify_after_threshold_sends_followup():
    tg = FakeTG()
    r = TurnRenderer(1, tg, 10, edit_interval=0, notify_after=0)
    await r.on_event(StreamEvent("content", "done"))
    await r.finish(final_text="done")
    assert tg.sent and tg.sent[0][1] == 10

def test_split_message_preserves_all_characters():
    text = "\n\n".join("p" * 1000 for _ in range(10))
    parts = split_message(text, limit=4096)
    assert len(parts) > 1
    assert "".join(parts) == text
    assert all(len(p) <= 4096 for p in parts)

def test_split_message_short_text_single_part():
    assert split_message("hi") == ["hi"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_bot.render'`

- [ ] **Step 3: Implement `TurnRenderer` in `src/mimir_bot/render.py`**

Keep `reasoning_chars`, `answer_text`, `last_edit_at`, `last_sent_text`. `on_event`: accumulate; if there is no answer text yet, the display is `thinking_label(reasoning_chars)`, otherwise `answer_text`; **the display is hard-truncated to 4096 chars for edits** (the tail is what the user is watching, so keep the last 4096 characters when it overflows); edit when `edit_interval` has elapsed and the display differs from `last_sent_text`. `finish`: final edit to the first `split_message(final_text)[0]` (or the no-answer text), then send the remaining parts with `send_message` in order; if `final_text` is empty and reasoning was seen, show `"…no answer (hit the token limit while thinking)"`; if the turn duration exceeded `notify_after` seconds, `send_message(chat_id, "answer ready ↑", reply_to=placeholder_id)`. `split_message` splits at the last `"\n\n"` before the limit, falling back to a hard cut; the join of parts must equal the input exactly.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_render.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: turn renderer with thinking display and message splitting"
```

---

### Task 6: Turn orchestration

**Files:**
- Create: `src/mimir_bot/turn.py`
- Test: `tests/test_turn.py`

**Interfaces:**
- Consumes: `Store` (Task 2), `MimirClient` (Task 4), `TurnRenderer` (Task 5), `Config` (Task 1).
- Produces:
  - `class TurnRunner` with `TurnRunner(store: Store, mimir: MimirClient, cfg: Config)`.
  - `async def run(self, chat_id: int, user_text: str, tg: Telegram, placeholder_id: int, image_data_url: str | None = None) -> str` — runs one turn, returns the final answer text (empty on failure), persists user message then assistant message. `image_data_url` is added by Task 9; Tasks 6–8 pass it only as `None`.
  - `def build_request(history: list[dict], system_prompt: str) -> list[dict]` — module-level; prepends the system prompt when non-empty.
  - `class GenerationLock` with `def begin(chat_id: int) -> asyncio.Task | None`-style semantics: expose `def current(self) -> asyncio.Task | None` and `async def replace(self, coro) -> asyncio.Task` — cancels the in-flight task, then starts the new one.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_turn.py
import pytest
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_turn.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_bot.turn'`

- [ ] **Step 3: Implement `TurnRunner` in `src/mimir_bot/turn.py`**

`run` reads `ChatSettings`, builds the request from `store.history(chat_id, cfg.history_token_budget)` **plus the new user message appended in memory only**, creates a `TurnRenderer`, iterates `mimir.stream(...)`, forwards events, then `renderer.finish(answer)`. The user message is persisted only once the stream has produced its first event; if the stream raises before that, nothing is written. On exception, edit the placeholder with `f"error: {exc}"` and return `""`. On success, persist the assistant message with `model=settings.model`. `GenerationLock.replace` cancels the current task with `task.cancel()` and awaits its suppression before starting the new one.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_turn.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: turn orchestration with history, model and error handling"
```

---

### Task 7: Commands

**Files:**
- Create: `src/mimir_bot/commands.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: `Store` (Task 2), `Config` (Task 1).
- Produces:
  - `@dataclass CommandResult(text: str, handled: bool)`.
  - `async def handle_command(text: str, chat_id: int, store: Store, cfg: Config) -> CommandResult | None` — `None` when the text is not a command.
  - `def resolve_model(arg: str, models: dict[str, int]) -> str | None` — module-level; exact match, then unique prefix match on the short name (`mimir`, `pro`, `slim`, `vision`), else `None`.
  - `def models_menu(current: str, models: dict[str, int]) -> str` — module-level; one line per model with `max_model_len` and a `←` marker on the current one.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_commands.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_bot.commands'`

- [ ] **Step 3: Implement commands in `src/mimir_bot/commands.py`**

`/model`, `/thinking`, `/new`, `/stop`, `/help`; unknown `/foo` replies with the help text. `/stop` returns a `CommandResult` whose text is `"stopped"` — the dispatcher in Task 8 does the actual cancellation when it sees that marker (the command module stays free of task state). `/thinking` values come from the literal list in Global Constraints.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_commands.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: /model /thinking /new /stop /help commands"
```

---

### Task 8: Poll loop and entrypoint

**Files:**
- Create: `src/mimir_bot/app.py`
- Create: `src/mimir_bot/__main__.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `class App` with `App(cfg: Config, store: Store, tg: Telegram, mimir: MimirClient)`.
  - `async def handle_update(self, update: dict) -> None` — the unit under test. For a plain message it sends the placeholder and **starts** the turn as an `asyncio.Task` without awaiting it, so a following message can interrupt it.
  - `async def idle(self) -> None` — awaits the in-flight turn task, if any. Exists so tests and shutdown are deterministic.
  - `async def run(self) -> None` — the poll loop: `get_updates(offset, timeout=30)`, dispatch each, advance `offset`, ping `healthcheck_url` every 60s, run `store.prune(cfg.retention_days)` once a day.
  - `def is_allowed(update: dict, allowed: frozenset[int]) -> bool` — module-level.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app.py
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
            import asyncio
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
    await app.handle_update(upd(1, "second"))
    await app.idle()
    assert "[interrupted]" in edits
    hist = await store.history(100, 100_000)
    assert all(m["content"] != "first" for m in hist)
    assert any(m["content"] == "answer" for m in hist)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_bot.app'`

- [ ] **Step 3: Implement `App` in `src/mimir_bot/app.py` and `__main__.py`**

`handle_update` ignores updates whose `from.id` is not allowlisted (silently — no reply). Commands go straight through `handle_command`; a `/stop` marker cancels the current task. Plain text: send a `"…"` placeholder message first, then `GenerationLock.replace(...)` a `TurnRunner.run(...)` task and return without awaiting it — the next message can therefore interrupt it, and `idle()` is what the tests await. `__main__.py` builds `Config.from_env(os.environ)`, one shared `httpx.AsyncClient`, wires `Store`/`Telegram`/`MimirClient`/`App`, and runs `asyncio.run(app.run())` with `logging.basicConfig(level=cfg.log_level)` logging chat id, duration and token counts only — never message bodies.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_app.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: poll loop, allowlist and update dispatch"
```

---

### Task 9: Photos and vision

**Files:**
- Modify: `src/mimir_bot/app.py`
- Modify: `src/mimir_bot/turn.py`
- Modify: `src/mimir_bot/telegram.py`
- Test: `tests/test_photos.py`

**Interfaces:**
- Consumes: `App.handle_update` (Task 8), `TurnRunner.run` (Task 6), `Telegram.get_updates` (Task 3).
- Produces:
  - `Telegram.get_file_url(file_id: str) -> str` — returns the full `https://api.telegram.org/file/bot{token}/{file_path}` URL after a `getFile` call.
  - `def image_content(text: str, image_data_url: str) -> list[dict]` — module-level in `turn.py`; returns the multimodal `content` array `[{"type": "text", ...}, {"type": "image_url", "image_url": {"url": ...}}]`.
  - `TurnRunner.run` gains an optional `image_data_url: str | None = None` parameter; when set it forces `model="vllm/mimir-vision"` for that turn only and leaves `ChatSettings.model` untouched.

- [ ] **Step 0: Confirm the seam**

`TurnRunner.run` already accepts `image_data_url` in its signature from Task 6 (defaulting to `None` and behaving as plain text). This task makes it functional: `App.handle_update` starts passing it, and `run` builds multimodal content and forces the vision model when it is set.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_photos.py
import base64, pytest
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_photos.py -v`
Expected: FAIL — `ImportError: cannot import name 'image_content'`

- [ ] **Step 3: Implement photo handling**

In `app.py`, a message with `photo` (not `text`) takes the largest `photo[-1].file_id`, calls `get_file_url`, downloads the bytes, and passes them as `image_data_url` with `text` defaulting to `"Describe this image."` when a caption is absent. `TurnRunner.run` sends `image_content(...)` as the last message's content but persists the plain string `text + " [image]"` so later turns are text-only.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_photos.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: photo messages routed to the vision model"
```

---

### Task 10: Deployment to `hygro.signocloud.it`

**Files:**
- Create: `deploy/mimir-telegram.service`
- Create: `deploy/README.md`

**Interfaces:**
- Consumes: the working package from Tasks 1–8.
- Produces: a running service; no code interfaces.

- [ ] **Step 1: Write the systemd unit**

`deploy/mimir-telegram.service`: `User=mimir`, `Group=mimir`, `WorkingDirectory=/opt/mimir-telegram`, `EnvironmentFile=/opt/mimir-telegram/.env`, `ExecStart=/opt/mimir-telegram/venv/bin/python -m mimir_bot`, `Restart=always`, `RestartSec=5`, `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=true`, `ReadWritePaths=/var/lib/mimir-telegram`, `After=network-online.target`.

- [ ] **Step 2: Run the full test suite locally**

Run: `pytest -v`
Expected: all tests pass (46 total)

- [ ] **Step 3: Create the user and directories on the host, without touching existing services**

```bash
ssh root@hygro.signocloud.it 'id mimir 2>/dev/null || useradd --system --home /opt/mimir-telegram --shell /usr/sbin/nologin mimir'
ssh root@hygro.signocloud.it 'mkdir -p /opt/mimir-telegram /var/lib/mimir-telegram && chown mimir:mimir /opt/mimir-telegram /var/lib/mimir-telegram'
```

- [ ] **Step 4: Ship the code and build the venv**

```bash
rsync -az --exclude .git --exclude venv ./ root@hygro.signocloud.it:/opt/mimir-telegram/
ssh root@hygro.signocloud.it 'cd /opt/mimir-telegram && python3 -m venv venv && venv/bin/pip install -q -U pip && venv/bin/pip install -q httpx aiosqlite && chown -R mimir:mimir /opt/mimir-telegram'
```

- [ ] **Step 5: Write the env file, install and start the unit**

`.env` (mode 600, owner `mimir`) holds `TELEGRAM_BOT_TOKEN`, `MIMIR_API_KEY`, `ALLOWED_TELEGRAM_USER_IDS`, `DB_PATH=/var/lib/mimir-telegram/bot.db`, and optionally `HEALTHCHECK_URL`. Then `systemctl daemon-reload && systemctl enable --now mimir-telegram`, and verify with `systemctl status mimir-telegram` and `journalctl -u mimir-telegram -n 30`.

- [ ] **Step 6: Verify the live bot and that nothing else was disturbed**

Send a message to the bot from the allowlisted account; expect a streamed answer. Then confirm `systemctl is-active esp32-hygro postgresql caddy` all report `active` and `ss -tlnp` still shows exactly the original listeners.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat: systemd deployment for hygro.signocloud.it"
```
