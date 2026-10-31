# Deployment on `hygro.signocloud.it`

The bot runs as an unprivileged systemd service next to the existing
`esp32-hygro` app, PostgreSQL and Caddy. It **listens on no port** — it only makes
outbound HTTPS calls to `api.telegram.org` and `mimir.cubbit.dev` — so it needs no
Caddy route, no certificate and no firewall change.

## Layout on the host

| Path | Purpose |
| --- | --- |
| `/opt/mimir-telegram` | code + venv, owned by `mimir` |
| `/opt/mimir-telegram/.env` | secrets, mode `600`, owned by `mimir` |
| `/var/lib/mimir-telegram/bot.db` | SQLite database (the only writable path) |
| `/etc/systemd/system/mimir-telegram.service` | the unit |

## First install

```bash
# 0. prerequisites: python >= 3.12 and the venv module
ssh root@hygro.signocloud.it 'python3 --version'
ssh root@hygro.signocloud.it 'python3 -m venv --help >/dev/null 2>&1 \
  || apt-get install -y python3-venv'

# 1. service account (no login shell, no home directory to speak of)
ssh root@hygro.signocloud.it 'id mimir 2>/dev/null || useradd --system \
  --home /opt/mimir-telegram --shell /usr/sbin/nologin mimir'
ssh root@hygro.signocloud.it 'mkdir -p /opt/mimir-telegram /var/lib/mimir-telegram \
  && chown mimir:mimir /opt/mimir-telegram /var/lib/mimir-telegram'

# 2. code and venv
rsync -az --exclude .git --exclude .venv --exclude .superpowers \
  ./ root@hygro.signocloud.it:/opt/mimir-telegram/
ssh root@hygro.signocloud.it 'cd /opt/mimir-telegram \
  && python3 -m venv venv && venv/bin/pip install -q -U pip \
  && venv/bin/pip install -q httpx==0.28.1 aiosqlite==0.22.1 \
  && chown -R mimir:mimir /opt/mimir-telegram'

# 3. secrets
# DB_PATH must be under /var/lib/mimir-telegram: the unit runs with
# ProtectSystem=strict, so WorkingDirectory is not writable and a fallback
# relative path would fail at startup and crash-loop under Restart=always.
ssh root@hygro.signocloud.it 'umask 077; cat > /opt/mimir-telegram/.env' <<'EOF'
TELEGRAM_BOT_TOKEN=...
MIMIR_API_KEY=...
ALLOWED_TELEGRAM_USER_IDS=...
DB_PATH=/var/lib/mimir-telegram/bot.db
DEFAULT_MODEL=vllm/mimir-slim
DEFAULT_EFFORT=none
LOG_LEVEL=INFO
EOF
ssh root@hygro.signocloud.it 'chown mimir:mimir /opt/mimir-telegram/.env && chmod 600 /opt/mimir-telegram/.env'

# 4. unit
scp deploy/mimir-telegram.service root@hygro.signocloud.it:/etc/systemd/system/
ssh root@hygro.signocloud.it 'systemctl daemon-reload \
  && systemctl enable --now mimir-telegram \
  && sleep 3 && systemctl is-active mimir-telegram \
  && journalctl -u mimir-telegram -n 30 --no-pager'
```

## Updating

```bash
rsync -az --exclude .git --exclude .venv --exclude .superpowers \
  ./ root@hygro.signocloud.it:/opt/mimir-telegram/
ssh root@hygro.signocloud.it 'chown -R mimir:mimir /opt/mimir-telegram \
  && systemctl restart mimir-telegram && journalctl -u mimir-telegram -n 20 --no-pager'
```

The database lives outside `/opt`, so a redeploy never touches history.

## Operating notes

- Only one process may poll a given bot token. If the service is started twice
  (or the same token is used elsewhere), `getUpdates` returns `Conflict`; the loop
  logs it and retries rather than dying.
- `/stop` cancels the in-flight request client-side, which frees the server slot
  immediately — verified against the live API.
- Logs contain chat ids, model names, character counts and token usage. Message
  bodies are never logged.
- History is pruned to `RETENTION_DAYS` (default 90) once a day.

## What this must not disturb

`esp32-hygro.service`, `/opt/ESP32-hygro`, PostgreSQL, and `/etc/caddy/Caddyfile`
are other people's services. The unit above binds no ports and writes only to
`/var/lib/mimir-telegram`. After deploying, check:

```bash
ssh root@hygro.signocloud.it 'systemctl is-active esp32-hygro postgresql caddy'
ssh root@hygro.signocloud.it 'ss -tlnp'
```
