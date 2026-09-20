"""Column types shared by every model (spec 012, Design 6; spec 013, Design 4; spec 014, 4)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator

from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc
from trading_bot.persistence.state import StateKey


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


class TimeframeType(TypeDecorator[Timeframe]):
    """A ``Timeframe`` stored as its canonical code (``1h``, ``4h``, ``1d``).

    Only a member binds: a raw string cannot reach the column through the ORM, and text the
    database should never hold raises ``UnknownTimeframeError`` at the boundary instead of
    flowing on as a value that compares unequal to every member. The ``CHECK`` constraints of
    spec 013 Design 3 are the same rule on the database side, for a manual ``sqlite3`` session.
    """

    impl = String(2)
    cache_ok = True

    def process_bind_param(self, value: Timeframe | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Timeframe):
            raise TypeError(f"timeframe must be a Timeframe, got {type(value).__name__}")
        return value.value

    def process_result_value(self, value: str | None, dialect: Dialect) -> Timeframe | None:
        if value is None:
            return None
        return Timeframe.parse(value)


class SideType(TypeDecorator[Side]):
    """A ``Side`` stored as ``BUY`` or ``SELL``, with the same boundary rules as above."""

    impl = String(4)
    cache_ok = True

    def process_bind_param(self, value: Side | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Side):
            raise TypeError(f"signal must be a Side, got {type(value).__name__}")
        return value.value

    def process_result_value(self, value: str | None, dialect: Dialect) -> Side | None:
        if value is None:
            return None
        return Side(value)


class StateKeyType(TypeDecorator[StateKey]):
    """A ``StateKey`` stored as its text (``paused_since``, ``last_run.1d``, ...).

    The same boundary rules as ``TimeframeType``: only a member binds, and text the database
    should never hold raises ``ValueError`` when it is read instead of flowing on as a key that
    matches nothing. The ``CHECK`` of ``bot_state`` is the same rule on the database side.
    """

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: StateKey | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, StateKey):
            raise TypeError(f"key must be a StateKey, got {type(value).__name__}")
        return value.value

    def process_result_value(self, value: str | None, dialect: Dialect) -> StateKey | None:
        if value is None:
            return None
        return StateKey(value)
