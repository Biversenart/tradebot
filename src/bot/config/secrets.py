"""Secrets read ONLY from the environment / `.env`. Never log these values."""

from __future__ import annotations

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bot.config.schema import Mode


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    # None => not set in env; resolved against config.yaml `mode` (see loader).
    trading_mode: Mode | None = None

    egress_proxy_url: SecretStr | None = None
    egress_expected_ip: str | None = None

    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    binance_testnet_api_key: SecretStr | None = None
    binance_testnet_api_secret: SecretStr | None = None
    btcturk_api_key: SecretStr | None = None
    btcturk_api_secret: SecretStr | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    api_auth_token: SecretStr | None = None
    database_url: SecretStr = SecretStr("sqlite+aiosqlite:///./data/bot.db")
    anthropic_api_key: SecretStr | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        # `.env.example` leaves keys empty (KEY=); treat that as "not set".
        if isinstance(value, str) and value.strip() == "":
            return None
        return value
