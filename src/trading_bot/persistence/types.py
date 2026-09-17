"""Column types shared by every model (spec 012, Design 6)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator

from trading_bot.domain.utc import to_utc


class UtcDateTime(TypeDecorator[datetime]):
    """Aware UTC datetimes stored as naive UTC values (CLAUDE.md rule 6).

    SQLite has no timestamp type and its driver returns naive values, so a plain ``DateTime``
    would silently hand back a value without a zone. Binding through ``to_utc`` keeps one
    definition of "an instant" across the domain, the API and the database, and rejects naive
    values, ``NaT`` and sub-microsecond precision at the boundary instead of storing a shifted
    key. Values are rendered with six fractional digits, so text order is chronological order.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return to_utc(value).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)
