# mimir-telegram

A Telegram bot that talks to [Mimir](https://mimir.cubbit.dev) (an Open WebUI
front end for a vLLM cluster) over its OpenAI-compatible streaming API.

Send it a message from your phone, watch the answer stream into a single message,
send another message to interrupt. `/model` and `/thinking` switch the model and
the reasoning effort per chat. Photos go to the vision model.

## Why a bot and not the web page

- **One tap.** Telegram is already unlocked on the phone; no login, no browser tab.
- **Async.** Ask for something long, put the phone away, get pinged when the answer
  is ready, reply from the notification.

## How it works

```
Telegram  ──long poll──▶  mimir_bot  ──POST /v1/chat/completions (SSE)──▶  Mimir
   ▲                          │
   └──── sendMessage / ────────┘
         editMessageText (throttled)
```

No webhook, no listening port: the process only makes outbound HTTPS calls, which
is why it can live on a host that already has other services on 80/443.

Reasoning tokens are streamed into the placeholder message while they arrive and
are **not** stored in history; only the final answer is persisted, so the next turn
does not re-feed thinking back into the prompt.

## Commands

| Command | Effect |
| --- | --- |
| `/model` | list models, or `/model slim` to switch (prefix match accepted) |
| `/thinking` | list reasoning efforts, or `/thinking low` to switch |
| `/new` | forget the conversation history for this chat |
| `/stop` | cancel the answer that is currently streaming |
| `/help` | the above |

Settings are per chat and persist across restarts.

## Layout

```
src/mimir_bot/
  config.py      env parsing, model/effort whitelists
  store.py       SQLite: chats, per-chat settings, message history
  telegram.py    Bot API client (getUpdates, send, edit, files)
  mimir.py       streaming chat-completions client with lenient SSE parsing
  render.py      turns stream events into one edited message
  turn.py        one answer: history, model choice, cancellation
  commands.py    slash commands
  app.py         update dispatch + long-poll loop
  __main__.py    wiring
tests/           one module per unit above, no network
deploy/          systemd unit and host runbook
```

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env      # fill in the token, key and your user id
set -a; . ./.env; set +a
.venv/bin/python -m mimir_bot
.venv/bin/pytest -q
```

`PYTHONPATH=src` is set by `pyproject.toml`'s pytest config, so `pytest` finds the
package without an install.

## Deploying

See [deploy/README.md](deploy/README.md). The service binds no port and writes only
to `/var/lib/mimir-telegram`.

## Notes on the upstream API

Behaviour that is measured, not assumed (see
`docs/superpowers/specs/2026-10-31-mimir-telegram-design.md` for the probes):

- reasoning arrives in `delta.reasoning` before any `delta.content`;
- `reasoning_effort: "none"` is the only reliable way to switch thinking off;
- the last SSE chunk has `created: 0`, `model: ""`, `delta: {}` and carries `usage`;
- dropping the HTTP request frees the server slot immediately, so `/stop` needs no
  upstream cancel call;
- `max_model_len` is 1M for `mimir`/`mimir-pro`, 262k for `mimir-slim`, 131k for
  `mimir-vision`.
