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
