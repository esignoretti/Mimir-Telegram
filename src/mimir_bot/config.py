"""Configuration loading.

Everything comes from the environment so the systemd unit can supply it via
EnvironmentFile, and so tests can pass a plain dict instead of touching
os.environ.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

#: model id -> max_model_len, as reported by the API's own error messages.
MODELS: dict[str, int] = {
    "vllm/mimir": 1048576,
    "vllm/mimir-pro": 1048576,
    "vllm/mimir-slim": 262144,
    "vllm/mimir-vision": 131072,
}

#: The only values the API accepts for reasoning_effort.
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

DEFAULT_BASE_URL = "https://mimir.cubbit.dev/v1"
DEFAULT_MODEL = "vllm/mimir-slim"
DEFAULT_EFFORT = "none"


def _require(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _optional(env: Mapping[str, str], name: str) -> str | None:
    raw = (env.get(name) or "").strip()
    return raw or None


@dataclass(frozen=True)
class Config:
    telegram_token: str
    mimir_api_key: str
    allowed_user_ids: frozenset[int]
    mimir_base_url: str = DEFAULT_BASE_URL
    default_model: str = DEFAULT_MODEL
    default_effort: str = DEFAULT_EFFORT
    db_path: str = "mimir-bot.db"
    system_prompt: str = ""
    notify_after_seconds: int = 60
    history_token_budget: int = 200_000
    retention_days: int = 90
    healthcheck_url: str | None = None
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        token = _require(env, "TELEGRAM_BOT_TOKEN")
        api_key = _require(env, "MIMIR_API_KEY")

        raw_ids = (env.get("ALLOWED_TELEGRAM_USER_IDS") or "").strip()
        ids = {int(part) for part in raw_ids.split(",") if part.strip()}
        if not ids:
            raise ValueError(
                "ALLOWED_TELEGRAM_USER_IDS is required: a comma-separated list "
                "of Telegram user ids, otherwise the bot answers nobody"
            )

        effort = (env.get("DEFAULT_EFFORT") or "").strip() or DEFAULT_EFFORT
        if effort not in EFFORTS:
            raise ValueError(
                f"DEFAULT_EFFORT must be one of {', '.join(EFFORTS)}, got {effort!r}"
            )

        return cls(
            telegram_token=token,
            mimir_api_key=api_key,
            allowed_user_ids=frozenset(ids),
            mimir_base_url=(env.get("MIMIR_BASE_URL") or "").strip() or DEFAULT_BASE_URL,
            default_model=(env.get("DEFAULT_MODEL") or "").strip() or DEFAULT_MODEL,
            default_effort=effort,
            db_path=(env.get("DB_PATH") or "").strip() or "mimir-bot.db",
            system_prompt=env.get("SYSTEM_PROMPT") or "",
            notify_after_seconds=_int(env, "NOTIFY_AFTER_SECONDS", 60),
            history_token_budget=_int(env, "HISTORY_TOKEN_BUDGET", 200_000),
            retention_days=_int(env, "RETENTION_DAYS", 90),
            healthcheck_url=_optional(env, "HEALTHCHECK_URL"),
            log_level=(env.get("LOG_LEVEL") or "").strip().upper() or "INFO",
        )
