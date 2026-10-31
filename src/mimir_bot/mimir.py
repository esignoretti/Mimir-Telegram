"""Streaming client for the Mimir (Open WebUI) OpenAI-compatible endpoint.

Two details of this API shape the parser:

* reasoning arrives in ``delta.reasoning`` *before* any ``delta.content``, so a
  turn can produce a long silence that the renderer has to fill with a label;
* the final chunk has ``created: 0``, ``model: ""`` and an empty ``delta`` but
  carries ``usage``, so anything that assumes a well-formed chunk breaks at the
  end of every successful turn.

Parsing is therefore lenient: unknown fields are ignored, empty deltas produce
no event, and ``[DONE]``/comments/blanks are skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator

import httpx


@dataclass(frozen=True)
class StreamEvent:
    kind: str  # "reasoning" | "content" | "usage"
    text: str


def parse_sse_line(line: str) -> dict | None:
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def describe_error(status: int, body: str) -> str:
    """One line the user can act on, from whatever shape the API errored in."""
    message = ""
    try:
        decoded = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        decoded = None

    if isinstance(decoded, dict):
        error = decoded.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("error") or ""
        elif isinstance(error, str):
            message = error
        if not message:
            message = decoded.get("message") or decoded.get("detail") or ""
    if not message:
        message = (body or "").strip()[:300] or f"HTTP {status}"

    lowered = message.lower()
    if "no keys found that support model" in lowered:
        model = message.split(":", 1)[-1].strip()
        return f"model {model!r} is not available on this endpoint"
    if "cannot be greater than max_model_len" in lowered:
        return f"request too long for this model: {message}"
    return f"mimir error ({status}): {message}"


class MimirClient:
    def __init__(self, base_url: str, api_key: str, client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = client

    async def stream(
        self,
        messages: list[dict],
        model: str,
        effort: str,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "reasoning_effort": effort,
        }
        if max_tokens is not None:
            payload["max_completion_tokens"] = max_tokens

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        async with self.client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        ) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", "replace")
                raise RuntimeError(describe_error(response.status_code, body))

            async for line in response.aiter_lines():
                data = parse_sse_line(line)
                if data is None:
                    continue

                choices = data.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    reasoning = delta.get("reasoning")
                    if reasoning:
                        yield StreamEvent("reasoning", reasoning)
                    content = delta.get("content")
                    if content:
                        yield StreamEvent("content", content)

                usage = data.get("usage")
                if usage:
                    yield StreamEvent("usage", json.dumps(usage))
