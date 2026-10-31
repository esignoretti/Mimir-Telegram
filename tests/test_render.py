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
