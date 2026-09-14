"""Logging setup with a redaction filter so secrets never reach log output."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"
# Telegram bot tokens: "<bot id>:<secret>". Matches the token shape, not a literal token.
TELEGRAM_TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


class RedactingFilter(logging.Filter):
    """Replace known secret values and token-shaped strings in every log record."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return TELEGRAM_TOKEN_PATTERN.sub(REDACTED, text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        return True


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    )
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
