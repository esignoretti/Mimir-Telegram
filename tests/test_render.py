# tests/test_render.py
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


async def test_rate_limited_edit_is_skipped_not_fatal():
    from mimir_bot.telegram import RateLimited
    class TG:
        def __init__(self): self.edits = []; self.sent = []
        async def edit_message(self, c, m, t):
            raise RateLimited("too many requests", retry_after=5)
        async def send_message(self, c, t, reply_to=None):
            self.sent.append(t); return 1
    tg = TG()
    r = TurnRenderer(1, tg, 10, edit_interval=0)
    await r.on_event(StreamEvent("content", "partial"))
    await r.on_event(StreamEvent("content", " more"))
    await r.finish(final_text="partial more")
    # The 429 must not cost the answer: the fallback send carries it.
    assert tg.sent and tg.sent[0] == "partial more"

async def test_failed_final_edit_still_delivers_the_answer():
    class TG:
        def __init__(self): self.sent = []; self.edits = 0
        async def edit_message(self, c, m, t):
            self.edits += 1
            raise RuntimeError("Bad Request: message to edit not found")
        async def send_message(self, c, t, reply_to=None):
            self.sent.append(t); return 1
    tg = TG()
    r = TurnRenderer(1, tg, 10, edit_interval=0)
    await r.on_event(StreamEvent("content", "the answer"))
    await r.finish(final_text="the answer")
    assert tg.edits >= 1
    assert tg.sent and tg.sent[0] == "the answer"

def test_split_message_counts_utf16_units():
    # Each astral emoji is 1 code point but 2 UTF-16 units.
    text = "\U0001F600" * 3000
    parts = split_message(text, limit=4096)
    assert "".join(parts) == text
    assert all(len(p.encode("utf-16-le")) // 2 <= 4096 for p in parts)
    assert len(parts) == 2

def test_tail_within_keeps_the_end():
    from mimir_bot.render import tail_within
    assert tail_within("abcdef", 3) == "def"
    assert tail_within("ab", 10) == "ab"
    assert tail_within("\U0001F600\U0001F600", 3) == "\U0001F600"


def test_tail_within_is_linear_on_a_long_answer():
    import time
    from mimir_bot.render import tail_within
    text = ""
    started = time.perf_counter()
    for _ in range(5000):
        text += "x" * 5
        tail_within(text, 4096)
    elapsed = time.perf_counter() - started
    # The quadratic version took ~20s for this loop; keep a wide margin.
    assert elapsed < 1.0, f"tail_within looks quadratic: {elapsed:.2f}s"


def test_tail_within_handles_astral_characters():
    from mimir_bot.render import tail_within
    text = "a" + "\U0001F600" * 3000
    out = tail_within(text, 4096)
    assert len(out.encode("utf-16-le")) // 2 <= 4096
    assert text.endswith(out)


async def test_dead_placeholder_is_not_retried():
    class TG:
        def __init__(self): self.calls = 0
        async def edit_message(self, c, m, t):
            self.calls += 1
            raise RuntimeError("Bad Request: message to edit not found")
        async def send_message(self, c, t, reply_to=None): return 1
    tg = TG()
    r = TurnRenderer(1, tg, 10, edit_interval=0)
    for _ in range(20):
        await r.on_event(StreamEvent("content", "x"))
    assert tg.calls == 1, f"retried a dead placeholder {tg.calls} times"


async def test_long_answer_streaming_stays_linear():
    """`answer += chunk` per SSE event was quadratic; 100k chars must stay fast."""
    import time
    from mimir_bot.render import TurnRenderer

    class TG:
        def __init__(self): self.last = None
        async def edit_message(self, c, m, t): self.last = t; return True
        async def send_message(self, c, t, reply_to=None): return 1

    r = TurnRenderer(1, TG(), 10, edit_interval=0)
    started = time.perf_counter()
    for _ in range(20_000):
        await r.on_event(StreamEvent("content", "xxxxx"))
    elapsed = time.perf_counter() - started
    assert len(r.answer_text) == 100_000
    assert elapsed < 2.0, f"streaming looks quadratic: {elapsed:.2f}s"


async def test_display_keeps_the_tail_of_a_long_answer():
    from mimir_bot.render import TurnRenderer, MESSAGE_LIMIT

    class TG:
        async def edit_message(self, c, m, t): return True
        async def send_message(self, c, t, reply_to=None): return 1

    r = TurnRenderer(1, TG(), 10, edit_interval=0)
    for i in range(3000):
        await r.on_event(StreamEvent("content", f"part{i} "))
    shown = r._display()
    assert len(shown.encode("utf-16-le")) // 2 <= MESSAGE_LIMIT
    assert r.answer_text.endswith(shown)
