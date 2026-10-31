# Mimir-Telegram

A Telegram bot for [Mimir](https://mimir.cubbit.dev) — an Open WebUI front end for a
vLLM cluster — using its OpenAI-compatible streaming API.

Send it a message from your phone and the answer streams into a single message as it
is generated. `/model` and `/thinking` switch the model and the reasoning effort per
chat; photos go to the vision model. No webhook, no listening port: it only makes
outbound HTTPS calls.

## Why a bot instead of the web page

- **One tap.** Telegram is already unlocked on the phone — no login, no browser tab,
  no re-authenticating on a small screen.
- **Async.** Ask for something long, put the phone away, get pinged when the answer is
  ready and reply straight from the notification.

## Features

- **Live streaming into one message.** A placeholder appears immediately, shows
  `thinking…` while reasoning tokens arrive, then fills with the answer. Edits are
  throttled to ~1 every 1.5 s to stay inside Telegram's rate limits.
- **Interrupt by just talking.** Send another message and the in-flight answer is
  cancelled — the upstream slot is freed immediately because the HTTP request is
  dropped, so there is no cancel API to call.
- **Reasoning is never replayed.** Thinking tokens are shown but not stored, so each
  turn does not pay for the previous turn's chain of thought.
- **Per-chat settings.** Model and reasoning effort persist across restarts.
- **Long answers.** Anything over 4096 UTF-16 units is split on paragraph boundaries
  across follow-up messages, without losing a character.
- **Photos** are downscaled by Telegram, downloaded, and sent to the vision model for
  that turn only — history keeps the caption plus `[image]`, never the image.
- **Allowlist.** Only the Telegram user ids you list get a reply; everyone else is
  silently ignored.
- **Fail-safe rendering.** A rate limit, a deleted placeholder, or a turn that dies
  mid-stream still delivers and stores whatever was paid for.

## Commands

| Command | Effect |
| --- | --- |
| `/model` | list models, or `/model slim` to switch (exact id, short name, or unique prefix) |
| `/thinking` | list reasoning efforts, or `/thinking low` to switch |
| `/new` | forget this chat's conversation history |
| `/stop` | cancel the answer currently streaming |
| `/help`, `/start` | the above |

Settings are per chat. `/model` and `/thinking` accept the values the API accepts:
`vllm/mimir`, `vllm/mimir-pro`, `vllm/mimir-slim`, `vllm/mimir-vision` and
`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`.

## How it works

```
Telegram ──long poll──▶ mimir_bot ──POST /v1/chat/completions (SSE)──▶ Mimir
   ▲                        │
   └─── send / edit ────────┘   (one edited placeholder per turn)
```

| Module | Responsibility |
| --- | --- |
| `config.py` | env parsing, model and effort whitelists, token windows |
| `store.py` | SQLite: chats, per-chat settings, message history, pruning |
| `telegram.py` | Bot API client — getUpdates, send, edit, file download, token scrubbing |
| `mimir.py` | streaming chat-completions client with lenient SSE parsing |
| `render.py` | turns stream events into one throttled, edited message |
| `turn.py` | one answer: history window, model choice, cancellation, persistence |
| `commands.py` | slash commands |
| `app.py` | update dispatch, allowlist, long-poll loop, maintenance |
| `__main__.py` | wiring and fail-fast config check |

## Quick start

Requires Python 3.12+.

```bash
git clone git@github.com:esignoretti/Mimir-Telegram.git
cd Mimir-Telegram
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

cp .env.example .env      # then fill in the three required values
set -a; . ./.env; set +a
.venv/bin/python -m mimir_bot
```

Get the bot token from [@BotFather](https://t.me/BotFather) and your numeric user id
from [@userinfobot](https://t.me/userinfobot). `PYTHONPATH=src` is configured in
`pyproject.toml`, so `pytest` finds the package without an install.

```bash
.venv/bin/pytest -q
```

## Configuration

Everything comes from the environment, so systemd can supply it via
`EnvironmentFile` and tests can pass a plain dict.

| Variable | Default | Notes |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | **required**, from @BotFather |
| `MIMIR_API_KEY` | — | **required**, the `sk-` key for the Mimir `/v1` endpoint |
| `ALLOWED_TELEGRAM_USER_IDS` | — | **required**, comma-separated numeric ids |
| `MIMIR_BASE_URL` | `https://mimir.cubbit.dev/v1` | OpenAI-compatible base URL |
| `DEFAULT_MODEL` | `vllm/mimir-slim` | model for new chats |
| `DEFAULT_EFFORT` | `none` | reasoning effort for new chats |
| `DB_PATH` | `mimir-bot.db` | SQLite file; must be writable by the service user |
| `SYSTEM_PROMPT` | empty | prepended to every request |
| `NOTIFY_AFTER_SECONDS` | `60` | ping `answer ready ↑` if a turn takes longer; `-1` disables |
| `HISTORY_TOKEN_BUDGET` | `200000` | history window, capped by the model's own context |
| `RETENTION_DAYS` | `90` | messages older than this are pruned daily; `0` keeps forever |
| `HEALTHCHECK_URL` | empty | optional GET once a minute |
| `LOG_LEVEL` | `INFO` | |

## Deploying

See [deploy/README.md](deploy/README.md) for the systemd unit and host runbook. The
service runs as an unprivileged user, binds no port, and writes only to
`/var/lib/mimir-telegram` — so it can share a host that already owns 80/443.

A bad `.env` exits with status `78` (`EX_CONFIG`) and a single stderr line, and the
unit lists that status in `RestartPreventExitStatus=`, so a misconfiguration parks the
service in `failed` instead of crash-looping.

## Notes on the upstream API

Measured against the live endpoint, not assumed — see
[the design doc](docs/superpowers/specs/2026-10-31-mimir-telegram-design.md) for the
probes behind each of these:

- Reasoning arrives in `delta.reasoning` (mirrored in `delta.reasoning_details[].text`)
  **before** any `delta.content`, and is on by default.
- `reasoning_effort: "none"` is the only reliable way to switch it off;
  `chat_template_kwargs.enable_thinking=false` is ignored.
- The final SSE chunk has `created: 0`, `model: ""` and `delta: {}` but carries
  `usage` — parsing has to be lenient.
- Dropping the HTTP request frees the server slot immediately (next request answered in
  0.3 s), so `/stop` needs no upstream cancel call.
- `max_model_len`: 1 048 576 for `mimir` and `mimir-pro`, 262 144 for `mimir-slim`,
  131 072 for `mimir-vision`. The history budget is capped by these.
- No Cloudflare in front (Caddy + fasthttp), so there is no 100 s timeout and SSE is
  not buffered.

## Security notes

- The bot token is embedded in every Bot API URL, and `httpx` logs full URLs at INFO —
  so `httpx`/`httpcore` loggers are pinned to WARNING and every raised message is
  scrubbed of the token. Both are regression-tested.
- File-download errors report only the HTTP status, never the URL (which contains the
  token) or the file path.
- Secrets live in `.env` (mode `600`, git-ignored), never in the repo.
- Message bodies are never logged; the log carries chat ids, model names, character
  counts and token usage only.

## Development

```bash
.venv/bin/pytest -q            # 77 tests, no network
.venv/bin/python -m pyflakes src tests
```

Tests are one module per source module and use fakes for Telegram and Mimir — no
network, no credentials. The suite pins the failure modes that actually cost answers:
a model that emits no reasoning, reasoning with no content, reasoning leaking into
history, the empty-delta final chunk, answers over the message limit, a 429 mid-stream,
and a placeholder deleted while the answer is still arriving.

## License

[MIT](LICENSE) © 2026 Edoardo Signoretti
