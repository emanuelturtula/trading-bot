"""Application settings loaded exclusively from environment variables (prefix ``TB_``)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from pathlib import Path
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "beta", "prod"]

# The zone signals are presented in (spec 017, U3). Storage and scheduling stay in UTC (rule 6).
DEFAULT_DISPLAY_TIMEZONE: Final = "America/Argentina/Buenos_Aires"
# Same rule as ``persistence.repositories.users.normalize_username``, which this module cannot
# import without loading the persistence layer.
_USERNAME: Final = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")
_HEADER_NAME: Final = re.compile(r"[a-z0-9-]{1,64}")
# Address classes ``trusted_proxies`` accepts besides networks (spec 023, D6).
ADDRESS_CLASSES: Final = ("loopback", "private")
type TrustedProxy = IPv4Network | IPv6Network | str


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
        # A validation error must never echo a value: chat ids and secrets live in these fields.
        hide_input_in_errors=True,
    )

    environment: Environment = "dev"
    version: str = "0.0.0-dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Directory of the SQLite database and any other runtime data (``/app/data`` in the
    # container). Infrastructure detail, not a secret: it is never added to ``secret_values``.
    data_dir: Path = Field(default=Path("data"), validate_default=True)

    # How long after a real candle close the scheduler runs (spec 016, U1), and how late a
    # missed fire may still run, which is also the startup catch-up window (decision D146).
    # Operational integers, not secrets: they stay out of ``secret_values``.
    candle_close_delay_seconds: int = Field(default=120, ge=0, le=900)
    scheduler_misfire_grace_seconds: int = Field(default=900, ge=60, le=3600)

    telegram_bot_token: SecretStr | None = None
    # Comma-separated chat ids: who may use the bot and who receives every signal (spec 017,
    # U1). Not a secret, but never logged; ``telegram_chat_ids`` is the parsed form.
    telegram_allowed_chat_ids: str = ""
    # IANA zone used only to present instants to the user (spec 017, U3).
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE
    # The first administrator's password hash, used only while there is no user (spec 023, D2).
    dashboard_password_hash: SecretStr | None = None
    session_secret: SecretStr | None = None
    # The username that first administrator gets (spec 023, D2).
    admin_username: str = "admin"
    # A request header holding the browser's address, set by a proxy in front of the app, such
    # as ``CF-Connecting-IP`` behind Cloudflare. Empty: the address of the TCP peer (spec 023,
    # D6). It is read only on requests whose TCP peer is in ``trusted_proxies``, so a browser
    # reaching the port directly cannot forge it.
    client_ip_header: str = ""
    # Where the proxy connects from: comma-separated networks in CIDR notation, or the words
    # ``loopback`` and ``private`` for those address classes. The default covers a tunnel daemon
    # on the same host or Docker network.
    trusted_proxies: str = "loopback,private"

    @field_validator("data_dir", mode="before")
    @classmethod
    def _normalize_data_dir(cls, value: object) -> Path:
        """Return ``value`` as an absolute, user-expanded path, rejecting an empty one."""
        if not isinstance(value, str | Path):
            raise ValueError(f"data_dir must be a string or a path, got {type(value).__name__}")
        text = str(value).strip()
        if not text:
            raise ValueError("data_dir must not be empty")
        return Path(text).expanduser().resolve()

    @field_validator("telegram_allowed_chat_ids", mode="after")
    @classmethod
    def _check_chat_ids(cls, value: str) -> str:
        """Accept an empty value or comma-separated integers; normalize the spacing."""
        return ",".join(str(chat_id) for chat_id in _parse_chat_ids(value))

    @field_validator("admin_username", mode="after")
    @classmethod
    def _check_admin_username(cls, value: str) -> str:
        username = value.strip().lower()
        if not _USERNAME.fullmatch(username):
            raise ValueError("admin_username must have 3-32 characters of a-z 0-9 . _ -")
        return username

    @field_validator("client_ip_header", mode="after")
    @classmethod
    def _check_client_ip_header(cls, value: str) -> str:
        header = value.strip().lower()
        if header and not _HEADER_NAME.fullmatch(header):
            raise ValueError("client_ip_header must be an HTTP header name")
        return header

    @field_validator("trusted_proxies", mode="after")
    @classmethod
    def _check_trusted_proxies(cls, value: str) -> str:
        return ",".join(str(item) for item in _parse_networks(value))

    @field_validator("display_timezone", mode="after")
    @classmethod
    def _check_display_timezone(cls, value: str) -> str:
        name = value.strip()
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("display_timezone must be an IANA time zone name") from None
        return name

    @property
    def telegram_chat_ids(self) -> tuple[int, ...]:
        """The parsed ``TB_TELEGRAM_ALLOWED_CHAT_IDS``, in the configured order."""
        return _parse_chat_ids(self.telegram_allowed_chat_ids)

    @property
    def trusted_proxy_networks(self) -> tuple[TrustedProxy, ...]:
        return _parse_networks(self.trusted_proxies)

    @property
    def display_zone(self) -> ZoneInfo:
        return ZoneInfo(self.display_timezone)

    def secret_values(self) -> list[str]:
        """Return the configured secret values, used to redact them from logs."""
        secrets = (self.telegram_bot_token, self.dashboard_password_hash, self.session_secret)
        return [value.get_secret_value() for value in secrets if value is not None]


def _parse_networks(value: str) -> tuple[TrustedProxy, ...]:
    networks: list[TrustedProxy] = []
    for part in value.split(","):
        text = part.strip().lower()
        if not text:
            continue
        if text in ADDRESS_CLASSES:
            networks.append(text)
            continue
        try:
            networks.append(ip_network(text, strict=False))
        except ValueError:
            raise ValueError(
                "trusted_proxies must be comma-separated IP networks, loopback or private"
            ) from None
    return tuple(networks)


def is_trusted_proxy(address: str, trusted: Sequence[TrustedProxy]) -> bool:
    """Whether ``address`` (the TCP peer) belongs to one of the ``trusted`` networks."""
    try:
        parsed = ip_address(address)
    except ValueError:  # not an address, such as the test client's "testclient"
        return False
    for item in trusted:
        if item == "loopback" and parsed.is_loopback:
            return True
        if item == "private" and parsed.is_private and not parsed.is_loopback:
            return True
        if isinstance(item, IPv4Network | IPv6Network) and parsed in item:
            return True
    return False


def _parse_chat_ids(value: str) -> tuple[int, ...]:
    """Comma-separated integers, duplicates dropped. The message never echoes the input."""
    chat_ids: list[int] = []
    for part in value.split(","):
        text = part.strip()
        if not text:
            continue
        try:
            chat_id = int(text)
        except ValueError:
            raise ValueError("telegram_allowed_chat_ids must be comma-separated integers") from None
        if chat_id not in chat_ids:
            chat_ids.append(chat_id)
    return tuple(chat_ids)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
