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
