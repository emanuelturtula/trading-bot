"""Logging setup with a redaction filter so secrets never reach log output."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"
# Telegram bot tokens: "<bot id>:<secret>". Matches the token shape, not a literal token.
TELEGRAM_TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
# Libraries that log request URLs at INFO or DEBUG. A Bot API URL carries the bot token in its
# path, so these loggers never go below WARNING, whatever the configured level (spec 017, §2).
QUIET_LOGGERS = ("httpx", "httpcore", "telegram")
# A dashboard access link carries its one-time token in the path (spec 023, D3), and the
# server's access log prints every request path.
ACCESS_LINK_PATTERN = re.compile(r"(/dashboard/invite/)[A-Za-z0-9_-]+")
# Loggers that keep handlers of their own (the server configures them before the app starts),
# so the root handler's filter never sees their records: they get the filter directly.
OWN_HANDLER_LOGGERS = ("uvicorn.access",)


class RedactingFilter(logging.Filter):
    """Replace known secret values and token-shaped strings in every log record."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = ACCESS_LINK_PATTERN.sub(r"\g<1>" + REDACTED, text)
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
    redacting = RedactingFilter(secrets)
    handler.addFilter(redacting)
    for name in OWN_HANDLER_LOGGERS:
        logger = logging.getLogger(name)
        for existing in [item for item in logger.filters if isinstance(item, RedactingFilter)]:
            logger.removeFilter(existing)
        logger.addFilter(redacting)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
