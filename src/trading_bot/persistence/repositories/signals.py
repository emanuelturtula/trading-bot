"""The signal repository and the indicator-values codec (spec 014, Design 7, 8; D112-D119).

``record`` is idempotent by construction: the unique constraint on
``(ticker_id, rule_id, timeframe, candle_close_ts)`` is the only arbiter that holds across
threads, the command-line process, retries and restarts (CLAUDE.md rule 5), and its
``IntegrityError`` is caught inside a ``SAVEPOINT`` so the caller's unit of work survives it
(decision D112). **The first write wins:** a stored signal is never rewritten, because it is
what was, or will be, notified.

The repository never commits, rolls back or closes; it flushes only to obtain the generated id
and to make the constraint speak. Like every module of this package except ``rules.py`` and the
ports, it loads no rule schema, so the engine can record a signal without the analysis stack
(decision D120).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Final, NoReturn

from sqlalchemy import Row, Select, func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading_bot.domain.signals import Signal
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc
from trading_bot.persistence.clock import Clock, system_clock
from trading_bot.persistence.errors import (
    StoredSignalError,
    TimeframeMismatchError,
    UnknownRuleError,
    UnknownSignalError,
    UntrackedTickerError,
    echo_name,
)
from trading_bot.persistence.models import RuleRow, SignalRow, TickerRow
from trading_bot.persistence.signal_records import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    RecordOutcome,
    SignalCursor,
    SignalPage,
    StoredSignal,
)

__all__ = ["SqlSignalRepository", "dump_indicator_values", "load_indicator_values"]

# SQLite stores an integer as at most eight bytes; a larger id would reach the driver as an
# OverflowError instead of a typed rejection (decision D114).
MAX_RULE_ID: Final = 2**63 - 1

type _JoinedRow = Row[tuple[SignalRow, str, str]]


def dump_indicator_values(values: Mapping[str, float]) -> str:
    """The canonical text of ``signals.indicator_values_json`` (decision D119).

    ``ensure_ascii=False`` keeps a non-ASCII indicator name readable in a manual ``sqlite3``
    session, and ``allow_nan=False`` guarantees the text is standard JSON, which matches
    ``IndicatorValues``: it holds finite values only. ``json`` writes floats with ``repr``, so
    every value round-trips bit for bit, and the keys keep the evaluation's order.
    """
    return json.dumps(dict(values), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def load_indicator_values(signal_id: int, text: str) -> dict[str, float]:
    """Parse a stored text, raising ``StoredSignalError`` when it is no longer valid.

    The parse error is never chained: its rendering would echo the stored text, which must
    reach neither a traceback nor a log record (decision D93).
    """
    try:
        parsed: object = json.loads(text, parse_constant=_reject_constant)
    except ValueError:
        raise StoredSignalError(signal_id, "indicator_values") from None
    if not isinstance(parsed, dict):
        raise StoredSignalError(signal_id, "indicator_values") from None
    values: dict[str, float] = {}
    for name, value in parsed.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise StoredSignalError(signal_id, "indicator_values") from None
        try:
            number = float(value)
        except OverflowError:  # an integer literal too large for a float
            raise StoredSignalError(signal_id, "indicator_values") from None
        if not math.isfinite(number):  # a literal such as 1e400 parses to infinity
            raise StoredSignalError(signal_id, "indicator_values") from None
        values[str(name)] = number
    return values


def _reject_constant(name: str) -> NoReturn:
    """``NaN``, ``Infinity`` and ``-Infinity`` are not JSON and never a stored value."""
    raise ValueError(f"{name} is not a finite number")


class SqlSignalRepository:
    """``SignalRepository`` over one session; build one per unit of work."""

    def __init__(self, session: Session, *, clock: Clock = system_clock) -> None:
        self._session = session
        self._clock = clock

    def record(self, signal: Signal) -> RecordOutcome:
        """Store the signal, or return the one already stored under its key (D112).

        The ticker and the rule are resolved before the insert, which is safe because the
        session holds the write lock from its first statement (D110): no other writer can
        remove them, or insert this key, in between. The unique constraint stays the arbiter
        for any writer that does not go through a session.
        """
        if not isinstance(signal, Signal):
            raise TypeError(f"signal must be a Signal, got {type(signal).__name__}")
        rule_id = _rule_id(signal.rule_id)
        ticker_id = self._ticker_id(signal.ticker, signal.timeframe)
        rule_name = self._rule_name(rule_id, signal.timeframe)
        row = SignalRow(
            ticker_id=ticker_id,
            rule_id=rule_id,
            timeframe=signal.timeframe,
            candle_close_ts=signal.candle_close_ts,
            side=signal.side,
            close_price=signal.close_price,
            indicator_values_json=dump_indicator_values(signal.indicator_values),
            created_at=to_utc(self._clock()),
            notified_at=None,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError:
            # The savepoint is rolled back, so the unit of work is still usable. Reading the
            # key back tells a duplicate signal from any other violation without depending on
            # SQLite's wording.
            existing = self._joined_row(
                self._select().where(
                    SignalRow.ticker_id == ticker_id,
                    SignalRow.rule_id == rule_id,
                    SignalRow.timeframe == signal.timeframe,
                    SignalRow.candle_close_ts == signal.candle_close_ts,
                )
            )
            if existing is None:
                raise
            return RecordOutcome(stored=existing, is_new=False)
        return RecordOutcome(
            stored=_to_record(row, signal.ticker, rule_name),
            is_new=True,
        )

    def get(self, signal_id: int) -> StoredSignal | None:
        return self._joined_row(self._select().where(SignalRow.id == signal_id))

    def mark_notified(self, signal_id: int) -> StoredSignal:
        """Stamp the delivery instant once; a second call keeps the first one (D115)."""
        row = self._session.get(SignalRow, signal_id)
        if row is None:
            raise UnknownSignalError(signal_id)
        if row.notified_at is None:
            row.notified_at = to_utc(self._clock())
            self._session.flush()
        stored = self._joined_row(self._select().where(SignalRow.id == signal_id))
        if stored is None:  # pragma: no cover - the row was read one statement ago
            raise UnknownSignalError(signal_id)
        return stored

    def latest(
        self, ticker_id: int, rule_id: int, *, notified_only: bool = False
    ) -> StoredSignal | None:
        """The pair's signal with the greatest candle close, which is what a cooldown counts."""
        statement = self._select().where(
            SignalRow.ticker_id == ticker_id, SignalRow.rule_id == rule_id
        )
        if notified_only:
            statement = statement.where(SignalRow.notified_at.is_not(None))
        ordered = statement.order_by(SignalRow.candle_close_ts.desc(), SignalRow.id.desc())
        return self._joined_row(ordered.limit(1))

    def history(
        self,
        *,
        ticker_id: int | None = None,
        rule_id: int | None = None,
        timeframe: Timeframe | None = None,
        notified: bool | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        before: SignalCursor | None = None,
    ) -> SignalPage:
        """One keyset page, newest first, ordered by ``(candle_close_ts DESC, id DESC)``.

        The range is half-open on ``candle_close_ts``: ``since`` is inclusive and ``until``
        exclusive. That instant is the candle's **nominal** close, so a caller turning user
        dates into bounds must convert them per timeframe (the nominal close of a ``1d``
        candle is 00:00 New York of the next day; spec 004, Design 5).

        Within one iteration no row appears twice and every row that matched when the first
        page was read appears exactly once, whatever is written meanwhile; a row recorded
        between two pages appears only if it sorts after the cursor.
        """
        _check_limit(limit)
        start = to_utc(since) if since is not None else None
        end = to_utc(until) if until is not None else None
        if start is not None and end is not None and start > end:
            raise ValueError("since must not be later than until")
        statement = self._filtered(
            ticker_id=ticker_id,
            rule_id=rule_id,
            timeframe=timeframe,
            notified=notified,
            since=start,
            until=end,
        )
        if before is not None:
            statement = statement.where(
                tuple_(SignalRow.candle_close_ts, SignalRow.id)
                < (to_utc(before.candle_close_ts), before.id)
            )
        ordered = statement.order_by(SignalRow.candle_close_ts.desc(), SignalRow.id.desc())
        rows = self._session.execute(ordered.limit(limit + 1)).all()
        items = tuple(_record_of(row) for row in rows[:limit])
        # The extra row is read to know whether a page follows, and never returned.
        cursor = SignalCursor.of(items[-1]) if len(rows) > limit else None
        return SignalPage(items=items, next_cursor=cursor)

    def count(self, *, ticker_id: int | None = None, rule_id: int | None = None) -> int:
        statement = select(func.count()).select_from(SignalRow)
        if ticker_id is not None:
            statement = statement.where(SignalRow.ticker_id == ticker_id)
        if rule_id is not None:
            statement = statement.where(SignalRow.rule_id == rule_id)
        return int(self._session.execute(statement).scalar_one())

    def _select(self) -> Select[tuple[SignalRow, str, str]]:
        """Every read joins the symbol and the rule's current name, so one query is enough."""
        return (
            select(SignalRow, TickerRow.symbol, RuleRow.name)
            .join(TickerRow, TickerRow.id == SignalRow.ticker_id)
            .join(RuleRow, RuleRow.id == SignalRow.rule_id)
        )

    def _filtered(
        self,
        *,
        ticker_id: int | None,
        rule_id: int | None,
        timeframe: Timeframe | None,
        notified: bool | None,
        since: datetime | None,
        until: datetime | None,
    ) -> Select[tuple[SignalRow, str, str]]:
        statement = self._select()
        if ticker_id is not None:
            statement = statement.where(SignalRow.ticker_id == ticker_id)
        if rule_id is not None:
            statement = statement.where(SignalRow.rule_id == rule_id)
        if timeframe is not None:
            statement = statement.where(SignalRow.timeframe == timeframe)
        if notified is not None:
            column = SignalRow.notified_at
            statement = statement.where(column.is_not(None) if notified else column.is_(None))
        if since is not None:
            statement = statement.where(SignalRow.candle_close_ts >= since)
        if until is not None:
            statement = statement.where(SignalRow.candle_close_ts < until)
        return statement

    def _joined_row(self, statement: Select[tuple[SignalRow, str, str]]) -> StoredSignal | None:
        row = self._session.execute(statement).first()
        return None if row is None else _record_of(row)

    def _ticker_id(self, symbol: str, timeframe: Timeframe) -> int:
        statement = select(TickerRow.id).where(
            TickerRow.symbol == symbol, TickerRow.timeframe == timeframe
        )
        found = self._session.execute(statement).scalar_one_or_none()
        if found is None:
            raise UntrackedTickerError(symbol, timeframe)
        return int(found)

    def _rule_name(self, rule_id: int, timeframe: Timeframe) -> str:
        """The rule's name, once its timeframe is known to match the signal's (D114)."""
        statement = select(RuleRow.name, RuleRow.timeframe).where(RuleRow.id == rule_id)
        found = self._session.execute(statement).one_or_none()
        if found is None:
            raise UnknownRuleError(rule_id)
        # ``_tuple()`` is SQLAlchemy's typed accessor: every ``Row`` method is underscored so
        # that a column named ``tuple`` or ``count`` cannot shadow it.
        name, rule_timeframe = found._tuple()
        if rule_timeframe != timeframe:
            raise TimeframeMismatchError(ticker_timeframe=timeframe, rule_timeframe=rule_timeframe)
        return name


def _record_of(row: _JoinedRow) -> StoredSignal:
    signal_row, symbol, rule_name = row._tuple()  # the typed Row accessor
    return _to_record(signal_row, symbol, rule_name)


def _to_record(row: SignalRow, symbol: str, rule_name: str) -> StoredSignal:
    """The frozen record of a row, with its payload parsed and its instants in UTC."""
    values = load_indicator_values(row.id, row.indicator_values_json)
    try:
        signal = Signal(
            ticker=symbol,
            timeframe=row.timeframe,
            rule_id=str(row.rule_id),
            side=row.side,
            candle_close_ts=to_utc(row.candle_close_ts),
            close_price=row.close_price,
            indicator_values=values,
        )
    except (TypeError, ValueError):
        # The row holds values the domain refuses, so it can be neither shown nor re-sent.
        raise StoredSignalError(row.id, "payload") from None
    return StoredSignal(
        id=row.id,
        ticker_id=row.ticker_id,
        rule_id=row.rule_id,
        rule_name=rule_name,
        signal=signal,
        created_at=to_utc(row.created_at),
        notified_at=None if row.notified_at is None else to_utc(row.notified_at),
    )


def _rule_id(text: str) -> int:
    """``signal.rule_id`` as the stored rule's id, which is its canonical decimal text (D114)."""
    try:
        value = int(text)
    except ValueError:
        value = 0  # not a number at all; the canonical check below rejects it
    if str(value) != text or not 1 <= value <= MAX_RULE_ID:
        raise ValueError(
            f"rule_id must be the decimal identifier of a stored rule, got {echo_name(text)}"
        )
    return value


def _check_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError(f"limit must be an int, got {type(limit).__name__}")
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}, got {limit}")
