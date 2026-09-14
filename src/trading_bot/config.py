"""Application settings loaded exclusively from environment variables (prefix ``TB_``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "beta", "prod"]


class Settings(BaseSettings):
    """Runtime configuration.

    Secrets are typed as ``SecretStr`` so they never appear in ``repr``, logs or
    serialized output. Values come from the process environment; ``.env`` is only a
    local development convenience and is never committed.
    """

    model_config = SettingsConfigDict(
        env_prefix="TB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = "dev"
    version: str = "0.0.0-dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    telegram_bot_token: SecretStr | None = None
    telegram_allowed_chat_ids: str = ""
    dashboard_password_hash: SecretStr | None = None
    session_secret: SecretStr | None = None

    def secret_values(self) -> list[str]:
        """Return the configured secret values, used to redact them from logs."""
        secrets = (self.telegram_bot_token, self.dashboard_password_hash, self.session_secret)
        return [value.get_secret_value() for value in secrets if value is not None]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
