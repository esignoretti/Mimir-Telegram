# Mimir on the phone — Telegram bridge

2026-10-31 · short design (small project, single service)

## Intent and why

Mimir is reachable today only through the Open WebUI page at
`https://mimir.cubbit.dev/` — a browser, a login, a tab. On a phone that is
enough friction that it does not get used while moving.

The point of this service is two things the web page cannot give:

1. **Reach** — one tap from the home screen or the lock screen, no login, no
   browser chrome, works with one thumb.
2. **Async** — send a long question, pocket the phone, get notified when the
   answer lands, reply from the notification. The web page holds you hostage
   in the tab; a chat does not.

## Goals

- Send a message from Telegram, get Mimir's answer streamed into it as it is
  generated (live typing, like the web UI).
- Conversation continuity per Telegram chat: follow-up questions keep context.
- If an answer takes longer than ~60s, a *new* message is sent on completion so
  the phone actually notifies (Telegram edits are silent).
- `/new` to start a fresh conversation, `/stop` to cancel a running generation.
- `/model` to list and switch the model per chat; `/thinking` to set reasoning
  effort per chat. Both persist across restarts.
- Images sent as photos go to a vision model.
- Runs unattended on `hygro.signocloud.it`, survives restarts, one process.

## Non-goals

- No tool calling, no MCP, no business-account actions. (Deliberate: this
  service holds no credentials beyond the Mimir key.)
- Not multi-user. One person's chats, or silence.
- No group chats, no inline queries, no message history search.
- No web UI of its own.

## Anti-goals

- Would be a failure if it needed an inbound port, a TLS cert, or a public
  webhook endpoint.
- Would be a failure if the bot token leak gave an attacker access to the Mimir
  key or to conversation history.
- Would be a failure if it felt slower than opening the web page for a short
  question.

## Constraints

### Verified against the live endpoint (probes run 2026-10-31)

All of the following were measured, not assumed:

- `https://mimir.cubbit.dev/v1` is live and OpenAI-compatible; the `sk-` key
  from `~/.config/opencode/mimir-key` authenticates directly. No Open WebUI
  session/JWT is involved. **No pivot needed.**
- Models: `vllm/mimir`, `vllm/mimir-pro`, `vllm/mimir-slim`, `vllm/mimir-vision`,
  `vllm/mimir-decide`.
- **Reasoning tokens are streamed first, in a separate `reasoning` field**
  (mirrored in `reasoning_details[].text`). `delta.content` is empty until
  reasoning finishes, then the answer arrives. A naive renderer that only reads
  `content` shows a frozen placeholder for the entire thinking phase. This is
  the single most important renderer constraint.
- Reasoning is **on by default**, even for "say hello". Disable with a
  top-level `"reasoning_effort": "none"` — verified: content returned
  immediately, `completion_tokens: 7`, no reasoning field.
  `chat_template_kwargs.enable_thinking=false` does **not** disable it.
- Final SSE chunk carries `created: 0`, `model: ""`, an empty `delta: {}`, and
  the `usage` block (`completion_tokens_details.reasoning_tokens`). Parse it
  leniently; do not require `choices[0].delta` to be non-empty.
- `mimir-decide` emits no reasoning field at all and is a small, noisy model —
  not suitable as the chat default.
- Throughput: `mimir-slim` produced 9,279 completion tokens in 32.6s
  (~285 tok/s, 2,863 chunks). Streaming latency is not the bottleneck.
- **No Cloudflare in front.** Response headers show `server: fasthttp` and
  `via: 1.0 Caddy`, no `cf-ray`. So the 100s Cloudflare response timeout does
  not apply and there is no SSE-buffering proxy in the path. Long streams are
  safe as-is.
- Client-side cancel is sufficient: killing the request mid-generation left the
  next request answered in 0.32s. No explicit upstream cancel call is needed;
  closing the connection frees the slot.
- Token estimation: 104,889 characters → 22,952 prompt tokens, i.e. **~4.6
  chars/token**. Use that ratio for history budgeting; no tokenizer needed.
- Vision works: `vllm/mimir-vision` accepts `image_url` with a `data:image/png;base64,`
  URL. An 8×8 PNG cost 127 prompt tokens.
- Non-streaming `POST` also works, and returns the same `reasoning` field.

### Inherent

- Mimir's API is **stateless**: history must be resent every turn. Context is
  large (917k for `mimir`/`mimir-pro`), but resending everything forever costs
  latency and money.
- Direct `/v1` calls bypass whatever the Open WebUI page adds server-side
  (persona system prompts, RAG/knowledge, web search, title generation). Answers
  may therefore differ from the web page. Mitigation: optional `SYSTEM_PROMPT`
  env var, empty by default — raw model unless configured otherwise.
- Telegram: `editMessageText` is rate-limited (practically ~1/sec per chat,
  and bursty edits get 429s). Edits do not trigger a phone notification.
- Telegram message length cap: 4096 chars. Long answers must be split.
- Markdown: Telegram's legacy Markdown is fragile. Use HTML parse mode with
  escaping, or plain text if escaping proves lossy.

### Host: `hygro.signocloud.it` (inspected 2026-10-31)

- Ubuntu 24.04.4 LTS, x86_64, **2 vCPU / 1.8 GiB RAM / 69 GB free**. Caddy and
  `esp32-hygro.service` are already running here; the box is shared and must
  not be disturbed.
- No Docker installed. Python 3.12.3 with `venv` and `pip` available.
- Existing services, all bound to loopback: gunicorn on `127.0.0.1:8000`
  (ESP32-Hygro backend), PostgreSQL 16 on `127.0.0.1:5432`, Caddy admin on
  `127.0.0.1:2019`. Caddy owns `*:80` and `*:443` with a single site
  `hygro.signocloud.it → reverse_proxy 127.0.0.1:8000`.
- Outbound to `mimir.cubbit.dev` (401 without key, i.e. reachable) and to
  `api.telegram.org` (302) both work.
- The existing `esp32-hygro` bot only *sends* via `/sendMessage` — it neither
  long-polls nor sets a webhook. A second bot with its own token cannot
  conflict with it.
- **Because Docker is absent and the box is small and shared, deploy as a
  systemd service under `/opt/mimir-telegram` with its own venv**, mirroring
  exactly how `esp32-hygro` is deployed. Adding a Docker daemon to a 1.8 GiB
  shared host is a bigger footprint than the service itself.
- **Never touch**: `esp32-hygro.service`, `/opt/ESP32-hygro`, `/opt/Signocloud-IOT`,
  PostgreSQL, `/etc/caddy/Caddyfile`, ports 80/443/8000/5432/2019. The new
  service binds no ports at all.

### API limits, measured

- `max_model_len` per model (the hard cap on `max_completion_tokens`):
  `mimir` 1,048,576 · `mimir-pro` 1,048,576 · `mimir-slim` 262,144 ·
  `mimir-vision` 131,072.
- `reasoning_effort` accepts exactly: `none`, `minimal`, `low`, `medium`,
  `high`, `xhigh`, `max`. Anything else is a 400 with a pydantic
  `literal_error`.
- A bad model name returns HTTP 400
  `no keys found that support model/deployment: <name>`.
- Effort levels are not finely calibrated: on `mimir-slim`, `low`, `medium` and
  `high` all produced ~22–26 reasoning tokens for a trivial prompt. The toggle
  is effectively `none` vs `anything else`; `/thinking` should therefore expose
  `none / low / medium / high / max` but the UI should not promise fine
  granularity.

## Approaches considered

1. **Thin bridge** — Telegram ⇄ `/v1/chat/completions`. One process, SQLite for
   conversation mapping, streaming via message edits. No tools, no business
   credentials. *Chosen.*
2. **Bridge + agent loop** — same, plus tool calling with business integrations
   living inside the service. Rejected for now: puts real credentials on a
   public box for no gain at this stage, and it is a much larger build.
3. **Bridge as remote control** — bot only forwards to an agent already running
   (headless pi/opencode with MCP tools), holding just a shared secret. Rejected
   as a *first* step because it needs that agent server to exist; it is the
   likely destination later, and approach 1 does not block it.

## Decisions made

- **Platform: Telegram.** Official stable Bot API, and message editing gives
  real token streaming. Signal was the privacy-favoured alternative and was
  declined for v1; it would cost live streaming and depend on an unofficial
  `signal-cli` link.
- **Transport: long polling, not webhooks.** The service makes outbound HTTPS
  calls to Telegram only. No inbound port, no cert, no reverse proxy, no public
  endpoint. This is the single biggest security simplification available.
- **Auth: allowlist of Telegram user IDs** in `ALLOWED_TELEGRAM_USER_IDS`.
  Any other chat is ignored silently (no "not authorized" reply — that just
  confirms the bot exists).
- **State: SQLite.** Two tables — chats (chat_id → active conversation, model,
  reasoning_effort) and messages (chat_id, role, content, model, ts). The model
  is recorded per message, not only per chat, so a vision turn or a `/think`
  turn does not retroactively relabel earlier turns. Everything needed to
  rebuild the request is on disk; a restart loses nothing but an in-flight
  generation.
- **Retention:** `RETENTION_DAYS` (default 90) prunes old messages on a daily
  timer. `0` means keep forever. The DB is plaintext on the VPS by design; that
  is an accepted risk, documented rather than mitigated.
- **History window:** send the whole conversation until the estimated prompt
  exceeds a threshold (default ~200k tokens, configurable), then drop oldest
  turns. Keeps follow-ups correct without resending 900k tokens every turn.
- **Streaming:** one Telegram message per turn, edited in place. Accumulate
  `delta.content`; edit every ~1.5s and only when text changed; on 429 back off
  and skip that edit. Send a placeholder immediately on receipt.
- **Thinking display:** while `delta.reasoning` is non-empty and `delta.content`
  is still empty, the placeholder shows `thinking… (N chars)` (or the tail of
  the reasoning, truncated to ~200 chars). The moment `content` starts, switch
  to rendering the answer. Reasoning text is **never persisted** into history
  and never sent back upstream.
- **Default model:** `vllm/mimir-slim` (user's choice), overridable per chat
  via `/model`. `reasoning_effort` default `"none"`, overridable via
  `/thinking`.
- **Second message mid-generation:** interrupts the running generation
  (client-side connection close — verified to free the slot) and starts the new
  turn. A half-finished answer is **discarded**, not persisted: a truncated
  answer poisons the next turn's context. The message is edited to
  `[interrupted]` so the loss is visible rather than silent.
- **Single user, single generation slot.** `ALLOWED_TELEGRAM_USER_IDS` may hold
  several IDs (spare accounts), but they share one history and one in-flight
  generation; a global lock enforces it. "Not multi-user" means one person.
- **Async ping:** if generation exceeds `NOTIFY_AFTER_SECONDS` (60), send a
  second short message on completion ("answer ready ↑" as a reply to the
  streamed message) so the phone notifies.
- **Language:** Python 3.12 + `aiogram` (or plain `httpx` + Bot API if a
  dependency-light version reads better) + `aiosqlite`. Python because it is
  already on the box and the async story is straightforward.
- **Packaging:** systemd unit `/etc/systemd/system/mimir-telegram.service`,
  venv at `/opt/mimir-telegram/venv`, `EnvironmentFile=/opt/mimir-telegram/.env`
  (mode 600). `Restart=always`, `RestartSec=5`, `User=` a dedicated non-root
  user (`mimir`), not root — the existing service runs as root, but there is no
  reason to repeat that. No ports bound.
- **Commands.** `/model` with no argument lists the four models with the
  current one marked; `/model <name>` switches and confirms, and accepts
  prefixes (`slim`, `pro`, `vision`, `mimir`). An unknown name replies with the
  list rather than an error string. `/thinking` with no argument shows the
  current effort and the valid values; `/thinking <level>` sets it. Both persist
  in the chats table. `/think` is dropped as a separate command — `/thinking`
  covers it.
- **Default model: `vllm/mimir-slim`**, `reasoning_effort: "none"`.
- **Liveness:** `HEALTHCHECK_URL` (optional). The poll loop pings it every 60s,
  so a crash-loop or revoked token shows up as a missed ping rather than as a
  bot that silently stops answering. If unset, the only signal is the bot
  failing to reply, and the service replies with a visible error message on the
  next message it does receive.

## What is left to the builder

- Which host. **Settled: `hygro.signocloud.it`**, deployed as a systemd
  service beside `esp32-hygro`, without Docker.
- Exact history-token estimation (tiktoken may not match Mimir's tokenizer —
  a character-count heuristic is acceptable and probably better; measured
  ~4.6 chars/token).
- HTML vs plain-text rendering, and how to split >4096-char answers.
- Whether to persist partial answers on crash (probably not, v1).
- Photo handling detail: a photo turn is sent to `vllm/mimir-vision` for that
  turn only; the chat's default model is unchanged afterwards. History
  containing a vision turn keeps the image description as plain text going
  forward (do not resend image bytes).

## Operational notes

- Mimir key and bot token are the whole blast radius. Token leak means someone
  can act as the bot and read incoming messages; it does *not* reach the Mimir
  key or the server, because the service exposes no inbound surface. Rotate via
  BotFather if suspected.
- Logging: message metadata only (chat id, duration, token counts), never
  message bodies, unless debug is on.
