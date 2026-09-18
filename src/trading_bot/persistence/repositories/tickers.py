"""The ticker repository over a ``Session`` (spec 013, Design 7).

It never commits, rolls back or closes: ``add`` flushes only to obtain the generated id. A
duplicate is detected with a ``SELECT`` before the insert, so the typed error leaves the session
usable and the caller can continue its unit of work; the unique constraint stays the backstop.
"""

from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from trading_bot.domain.signals import normalize_ticker
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc
from trading_bot.persistence.clock import Clock, system_clock
from trading_bot.persistence.errors import DuplicateTickerError, UnknownTickerError
from trading_bot.persistence.models import TickerRow
from trading_bot.persistence.records import StoredTicker

__all__ = ["SqlTickerRepository"]


def to_record(row: TickerRow) -> StoredTicker:
    """The frozen record of a row, with its instant normalized to UTC."""
    return StoredTicker(
        id=row.id,
        symbol=row.symbol,
        timeframe=row.timeframe,
        enabled=row.enabled,
        created_at=to_utc(row.created_at),
    )


class SqlTickerRepository:
    """``TickerRepository`` over one session; build one per unit of work."""

    def __init__(self, session: Session, *, clock: Clock = system_clock) -> None:
        self._session = session
        self._clock = clock

    def add(self, symbol: str, timeframe: Timeframe, *, enabled: bool = True) -> StoredTicker:
        normalized = normalize_ticker(symbol)
        if self._row_by_symbol(normalized, timeframe) is not None:
            raise DuplicateTickerError(normalized, timeframe)
        row = TickerRow(
            symbol=normalized,
            timeframe=timeframe,
            enabled=enabled,
            created_at=to_utc(self._clock()),
        )
        self._session.add(row)
        self._session.flush()
        return to_record(row)

    def get(self, ticker_id: int) -> StoredTicker | None:
        row = self._session.get(TickerRow, ticker_id)
        return None if row is None else to_record(row)

    def get_by_symbol(self, symbol: str, timeframe: Timeframe) -> StoredTicker | None:
        row = self._row_by_symbol(normalize_ticker(symbol), timeframe)
        return None if row is None else to_record(row)

    def list_all(self) -> tuple[StoredTicker, ...]:
        return self._listed(select(TickerRow))

    def list_enabled(self, timeframe: Timeframe | None = None) -> tuple[StoredTicker, ...]:
        statement = select(TickerRow).where(TickerRow.enabled.is_(True))
        if timeframe is not None:
            statement = statement.where(TickerRow.timeframe == timeframe)
        return self._listed(statement)

    def set_enabled(self, ticker_id: int, enabled: bool) -> StoredTicker:
        row = self._session.get(TickerRow, ticker_id)
        if row is None:
            raise UnknownTickerError(ticker_id)
        if row.enabled != enabled:
            row.enabled = enabled
            self._session.flush()
        return to_record(row)

    def delete(self, ticker_id: int) -> bool:
        row = self._session.get(TickerRow, ticker_id)
        if row is None:
            return False
        # The assignments go with it through the database cascade (D86), never a Python loop.
        self._session.delete(row)
        self._session.flush()
        return True

    def _row_by_symbol(self, symbol: str, timeframe: Timeframe) -> TickerRow | None:
        statement = select(TickerRow).where(
            TickerRow.symbol == symbol, TickerRow.timeframe == timeframe
        )
        return self._session.execute(statement).scalar_one_or_none()

    def _listed(self, statement: Select[tuple[TickerRow]]) -> tuple[StoredTicker, ...]:
        ordered = statement.order_by(TickerRow.symbol, TickerRow.timeframe)
        return tuple(to_record(row) for row in self._session.execute(ordered).scalars())
