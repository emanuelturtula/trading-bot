"""Exchange sessions, the candle grid and real candle closes (spec 009, Design 3 and 4).

A ``MarketCalendar`` is an immutable snapshot of the regular sessions of one exchange between
two days, in UTC. Its queries take the instant as an argument and read only that snapshot, so
they are pure (CLAUDE.md rule 3): whether the market is open, the session of a day, the candle
grid of a timeframe, the next real candle close and the candles closed at an instant. They
decide closedness and scheduling; signal identity keeps ``Timeframe.nominal_close`` (spec 004).

Intervals are half-open: a session is ``[open_time, close_time)`` and a candle is closed when
``close_time <= now``. Outside its span a calendar raises ``CalendarRangeError`` instead of
guessing. This module imports neither pandas, numpy nor ``exchange_calendars``.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import Final

from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc

__all__ = [
    "CalendarError",
    "CalendarRangeError",
    "CandleLabelError",
    "CandleLabelErrorKind",
    "CandleSlot",
    "MarketCalendar",
    "Session",
]

_MAX_NAME_LENGTH: Final = 16
_MAX_TYPE_NAME_LENGTH: Final = 64


class CalendarError(ValueError):
    """Base class of calendar errors."""


class CalendarRangeError(CalendarError):
    """A day or instant outside the calendar, or history or future the calendar does not hold."""

    calendar: str  # MarketCalendar.name
    first_day: date
    last_day: date

    def __init__(self, message: str, *, calendar: str, first_day: date, last_day: date) -> None:
        # args holds only the positional message, so the default exception pickling
        # (``cls(*args)`` plus ``__dict__``) would lose the keyword-only fields: see __reduce__.
        super().__init__(message)
        self.calendar = calendar
        self.first_day = first_day
        self.last_day = last_day

    def __reduce__(self) -> tuple[object, ...]:
        return (_rebuild_range_error, (str(self), self.calendar, self.first_day, self.last_day))


class CandleLabelErrorKind(StrEnum):
    """Why a label inside the calendar is not a candle label of a timeframe (Design 4.3)."""

    NOT_A_SESSION = "not_a_session"
    OUTSIDE_SESSION = "outside_session"
    OFF_GRID = "off_grid"


class CandleLabelError(CalendarError):
    """A label inside the calendar that is not a label of the timeframe's session grid."""

    kind: CandleLabelErrorKind
    timeframe: Timeframe
    label: datetime  # stdlib UTC

    def __init__(
        self,
        message: str,
        *,
        kind: CandleLabelErrorKind,
        timeframe: Timeframe,
        label: datetime,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.timeframe = timeframe
        self.label = label

    def __reduce__(self) -> tuple[object, ...]:
        return (_rebuild_label_error, (str(self), self.kind, self.timeframe, self.label))


def _rebuild_range_error(
    message: str, calendar: str, first_day: date, last_day: date
) -> CalendarRangeError:
    return CalendarRangeError(message, calendar=calendar, first_day=first_day, last_day=last_day)


def _rebuild_label_error(
    message: str, kind: CandleLabelErrorKind, timeframe: Timeframe, label: datetime
) -> CandleLabelError:
    return CandleLabelError(message, kind=kind, timeframe=timeframe, label=label)


@dataclass(frozen=True, slots=True, kw_only=True)
class Session:
    """One regular session. ``day`` is the session date in exchange time; instants are UTC."""

    day: date
    open_time: datetime
    close_time: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "day", _plain_day(self.day, "day"))
        open_time = _utc_instant(self.open_time, "open_time")
        close_time = _utc_instant(self.close_time, "close_time")
        if open_time >= close_time:
            raise ValueError(
                f"session {self.day.isoformat()} must open before it closes, got open "
                f"{open_time.isoformat()} and close {close_time.isoformat()}"
            )
        object.__setattr__(self, "open_time", open_time)
        object.__setattr__(self, "close_time", close_time)


@dataclass(frozen=True, slots=True, kw_only=True)
class CandleSlot:
    """One candle of the session grid. Built by MarketCalendar; fields are stdlib UTC."""

    timeframe: Timeframe
    session_day: date
    label: datetime  # the canonical provider label (spec 009, D21)
    open_time: datetime
    close_time: datetime  # the real close


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class MarketCalendar:
    """The regular sessions of one exchange between ``first_day`` and ``last_day`` (inclusive).

    ``sessions`` holds every session of the span, sorted by day. Days of the span without a
    session are closed days. Instants from ``coverage_start`` (00:00 of ``first_day`` in
    ``timezone``) up to ``coverage_end`` (00:00 of the day after ``last_day``) are answerable;
    any other instant raises ``CalendarRangeError``.
    """

    name: str
    timezone: tzinfo
    first_day: date
    last_day: date
    sessions: tuple[Session, ...]
    _coverage_start: datetime = field(init=False, repr=False, compare=False)
    _coverage_end: datetime = field(init=False, repr=False, compare=False)
    _days: tuple[date, ...] = field(init=False, repr=False, compare=False)
    _opens: tuple[datetime, ...] = field(init=False, repr=False, compare=False)
    _closes: tuple[datetime, ...] = field(init=False, repr=False, compare=False)
    _day_starts: tuple[datetime, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        name = _calendar_name(self.name)
        object.__setattr__(self, "name", name)
        if not isinstance(self.timezone, tzinfo):
            raise TypeError(f"calendar timezone must be a tzinfo, got {_type_name(self.timezone)}")
        first_day = _plain_day(self.first_day, "first_day")
        last_day = _plain_day(self.last_day, "last_day")
        if not date.min < first_day <= last_day < date.max:
            raise ValueError(
                f"calendar {name} needs {date.min.isoformat()} < first_day <= last_day < "
                f"{date.max.isoformat()}, got {first_day.isoformat()} and {last_day.isoformat()}"
            )
        object.__setattr__(self, "first_day", first_day)
        object.__setattr__(self, "last_day", last_day)
        sessions = _session_tuple(self.sessions, name)
        object.__setattr__(self, "sessions", sessions)

        previous: date | None = None
        for session in sessions:
            _check_session(session, previous, self.timezone, name, first_day, last_day)
            previous = session.day

        object.__setattr__(self, "_coverage_start", self._day_start(first_day))
        object.__setattr__(self, "_coverage_end", self._day_start(last_day + timedelta(days=1)))
        object.__setattr__(self, "_days", tuple(session.day for session in sessions))
        object.__setattr__(self, "_opens", tuple(session.open_time for session in sessions))
        object.__setattr__(self, "_closes", tuple(session.close_time for session in sessions))
        object.__setattr__(
            self, "_day_starts", tuple(self._day_start(session.day) for session in sessions)
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(name={self.name!r}, first_day={self.first_day.isoformat()}, "
            f"last_day={self.last_day.isoformat()}, sessions={len(self.sessions)})"
        )

    @property
    def coverage_start(self) -> datetime:
        """00:00 of ``first_day`` in ``timezone``, as a stdlib UTC datetime."""
        return self._coverage_start

    @property
    def coverage_end(self) -> datetime:
        """00:00 of the day after ``last_day`` in ``timezone``, as a stdlib UTC datetime."""
        return self._coverage_end

    def session_bounds(self, day: date) -> Session | None:
        """The session of ``day`` (a date in exchange time), or ``None`` on a closed day."""
        plain = _plain_day(day, "day")
        if not self.first_day <= plain <= self.last_day:
            raise self._range_error(f"day {plain.isoformat()} is outside calendar {self.name}")
        index = self._session_index(plain)
        return None if index is None else self.sessions[index]

    def is_open(self, ts: datetime) -> bool:
        """Whether ``ts`` is inside a session: ``open_time <= ts < close_time``."""
        instant = self._inside(_utc_instant(ts, "ts"), "ts")
        index = bisect_right(self._opens, instant) - 1
        return index >= 0 and instant < self._closes[index]

    def next_candle_close(self, timeframe: Timeframe, now: datetime) -> datetime:
        """The smallest real ``close_time`` of a ``timeframe`` slot that is strictly after ``now``.

        Raises ``CalendarRangeError`` when ``now`` is outside the calendar or no slot of the
        calendar closes after it.
        """
        _check_timeframe(timeframe)
        instant = self._inside(_utc_instant(now, "now"), "now")
        index = bisect_right(self._closes, instant)  # the first session that closes after now
        if index == len(self._closes):
            raise self._range_error(
                f"no {timeframe} candle of calendar {self.name} closes after {instant.isoformat()}"
            )
        position = 0
        if timeframe is not Timeframe.D1 and self._opens[index] <= instant:
            position = (instant - self._opens[index]) // timeframe.duration
        return self._slot(timeframe, index, position).close_time

    def candle_slot(self, timeframe: Timeframe, label: datetime) -> CandleSlot:
        """The slot whose canonical label is exactly ``label``.

        Any other label inside the calendar raises ``CandleLabelError`` with its kind
        (``not_a_session``, ``outside_session`` or ``off_grid``, in that order of precedence).
        """
        _check_timeframe(timeframe)
        instant = self._inside(_utc_instant(label, "label"), "label")
        if timeframe is Timeframe.D1:
            index = bisect_left(self._day_starts, instant)
            if index < len(self._day_starts) and self._day_starts[index] == instant:
                return self._slot(timeframe, index, 0)
        else:
            index = bisect_right(self._opens, instant) - 1
            if index >= 0 and instant < self._closes[index]:
                position, rest = divmod(instant - self._opens[index], timeframe.duration)
                if not rest:
                    return self._slot(timeframe, index, position)
        raise self._label_error(timeframe, instant)

    def candle_slots(
        self, timeframe: Timeframe, start: datetime, end: datetime
    ) -> tuple[CandleSlot, ...]:
        """Every ``timeframe`` slot with ``start <= label < end``, in increasing label order.

        Both bounds may be anywhere from ``coverage_start`` to ``coverage_end`` inclusive;
        ``start > end`` raises ``ValueError`` before the range is checked.
        """
        _check_timeframe(timeframe)
        first = _utc_instant(start, "start")
        stop = _utc_instant(end, "end")
        if first > stop:
            raise ValueError(f"start {first.isoformat()} is after end {stop.isoformat()}")
        for bound, argument in ((first, "start"), (stop, "end")):
            if not self._coverage_start <= bound <= self._coverage_end:
                raise self._range_error(
                    f"{argument} {bound.isoformat()} is outside calendar {self.name}"
                )
        if timeframe is Timeframe.D1:
            low = bisect_left(self._day_starts, first)
            high = bisect_left(self._day_starts, stop)
            return tuple(self._slot(timeframe, index, 0) for index in range(low, high))
        slots: list[CandleSlot] = []
        # Sessions that close at or before ``start`` have no label at or after it.
        index = bisect_right(self._closes, first)
        while index < len(self._opens) and self._opens[index] < stop:
            first_position = max(0, _ceil_div(first - self._opens[index], timeframe.duration))
            for position in range(first_position, self._slot_count(timeframe, index)):
                candle = self._slot(timeframe, index, position)
                if candle.label >= stop:
                    break
                slots.append(candle)
            index += 1
        return tuple(slots)

    def closed_candles(
        self, timeframe: Timeframe, now: datetime, count: int
    ) -> tuple[CandleSlot, ...]:
        """The last ``count`` slots of ``timeframe`` with ``close_time <= now``, oldest first.

        Raises ``CalendarRangeError`` when ``now`` is outside the calendar or fewer than
        ``count`` slots of the calendar close by ``now``.
        """
        _check_timeframe(timeframe)
        instant = _utc_instant(now, "now")
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError(f"count must be an int, got {_type_name(count)}")
        if count < 0:
            raise ValueError("count must be zero or positive")
        self._inside(instant, "now")
        if count == 0:
            return ()
        # Sessions before ``index`` have closed; session ``index`` may be in progress at ``now``.
        index = bisect_right(self._closes, instant)
        in_progress = 0
        if (
            timeframe is not Timeframe.D1
            and index < len(self._opens)
            and self._opens[index] <= instant
        ):
            in_progress = (instant - self._opens[index]) // timeframe.duration
        # Walk back over slot counts, building nothing, to the session of the oldest slot.
        first_index = index
        first_position = in_progress - count
        available = in_progress
        while first_position < 0:
            first_index -= 1
            if first_index < 0:
                raise self._range_error(
                    f"only {available} {timeframe} candles of calendar {self.name} close by "
                    f"{instant.isoformat()}, fewer than requested"
                )
            slot_count = self._slot_count(timeframe, first_index)
            available += slot_count
            first_position += slot_count
        slots: list[CandleSlot] = []
        position = first_position
        for session_index in range(first_index, index):
            last = self._slot_count(timeframe, session_index)
            slots.extend(self._slot(timeframe, session_index, p) for p in range(position, last))
            position = 0
        slots.extend(self._slot(timeframe, index, p) for p in range(position, in_progress))
        return tuple(slots)

    def _day_start(self, day: date) -> datetime:
        """00:00 of ``day`` in the calendar time zone, in UTC (the first occurrence, fold 0)."""
        return to_utc(datetime.combine(day, time(0), tzinfo=self.timezone))

    def _session_index(self, day: date) -> int | None:
        index = bisect_left(self._days, day)
        if index < len(self._days) and self._days[index] == day:
            return index
        return None

    def _inside(self, instant: datetime, argument: str) -> datetime:
        """``instant`` if ``coverage_start <= instant < coverage_end``; ``CalendarRangeError``."""
        if not self._coverage_start <= instant < self._coverage_end:
            raise self._range_error(
                f"{argument} {instant.isoformat()} is outside calendar {self.name}"
            )
        return instant

    def _slot_count(self, timeframe: Timeframe, index: int) -> int:
        """The number of ``timeframe`` slots of session ``index`` (Design 4.2)."""
        if timeframe is Timeframe.D1:
            return 1
        return _ceil_div(self._closes[index] - self._opens[index], timeframe.duration)

    def _slot(self, timeframe: Timeframe, index: int, position: int) -> CandleSlot:
        """Slot ``position`` of session ``index``: anchored at the open, cut at the close."""
        open_time = self._opens[index]
        close_time = self._closes[index]
        if timeframe is Timeframe.D1:
            return CandleSlot(
                timeframe=timeframe,
                session_day=self._days[index],
                label=self._day_starts[index],
                open_time=open_time,
                close_time=close_time,
            )
        label = open_time + position * timeframe.duration
        return CandleSlot(
            timeframe=timeframe,
            session_day=self._days[index],
            label=label,
            open_time=label,
            close_time=min(label + timeframe.duration, close_time),
        )

    def _label_error(self, timeframe: Timeframe, label: datetime) -> CandleLabelError:
        """Classify a label that matched no slot (Design 4.3)."""
        index = self._session_index(label.astimezone(self.timezone).date())
        if index is None:
            kind = CandleLabelErrorKind.NOT_A_SESSION
        elif timeframe is not Timeframe.D1 and not (
            self._opens[index] <= label < self._closes[index]
        ):
            kind = CandleLabelErrorKind.OUTSIDE_SESSION
        else:
            kind = CandleLabelErrorKind.OFF_GRID
        return CandleLabelError(
            f"{label.isoformat()} is not a {timeframe} candle label in {self.name} ({kind})",
            kind=kind,
            timeframe=timeframe,
            label=label,
        )

    def _range_error(self, message: str) -> CalendarRangeError:
        return CalendarRangeError(
            f"{message}; calendar {self.name} covers {self.first_day.isoformat()} to "
            f"{self.last_day.isoformat()}",
            calendar=self.name,
            first_day=self.first_day,
            last_day=self.last_day,
        )


def _check_timeframe(value: object) -> None:
    if not isinstance(value, Timeframe):
        raise TypeError(
            f"timeframe must be a Timeframe, got {_type_name(value)} (use Timeframe.parse for text)"
        )


def _ceil_div(numerator: timedelta, denominator: timedelta) -> int:
    whole, rest = divmod(numerator, denominator)
    return whole + 1 if rest else whole


def _check_session(
    session: Session,
    previous: date | None,
    timezone: tzinfo,
    name: str,
    first_day: date,
    last_day: date,
) -> None:
    day = session.day
    if previous is not None and day <= previous:
        raise ValueError(
            f"session days of calendar {name} must be strictly increasing, got "
            f"{day.isoformat()} after {previous.isoformat()}"
        )
    if not first_day <= day <= last_day:
        raise ValueError(
            f"session {day.isoformat()} is outside calendar {name} "
            f"({first_day.isoformat()} to {last_day.isoformat()})"
        )
    if (
        session.open_time.astimezone(timezone).date() != day
        or session.close_time.astimezone(timezone).date() != day
    ):
        raise ValueError(
            f"session {day.isoformat()} of calendar {name} must open and close on that day in "
            f"the calendar time zone, got open {session.open_time.isoformat()} and close "
            f"{session.close_time.isoformat()}"
        )


def _calendar_name(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"calendar name must be a str, got {_type_name(value)}")
    # The rejected text is never echoed: only a valid name reaches later messages.
    if not 0 < len(value) <= _MAX_NAME_LENGTH or not all("!" <= char <= "~" for char in value):
        raise ValueError(
            f"calendar name must be 1-{_MAX_NAME_LENGTH} printable ASCII characters without "
            "whitespace"
        )
    return str.__str__(value)  # a plain str, also for str subclasses


def _session_tuple(value: object, name: str) -> tuple[Session, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"sessions of calendar {name} must be a tuple, got {_type_name(value)}")
    for item in value:
        if not isinstance(item, Session):
            raise TypeError(
                f"sessions of calendar {name} must all be Session, got {_type_name(item)}"
            )
    return tuple(value)


def _plain_day(value: object, argument: str) -> date:
    """``value`` as a plain ``date``; a ``datetime`` (or ``pd.Timestamp``) is not a day."""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(
            f"{argument} must be a date that is not a datetime, got {_type_name(value)}"
        )
    return date(value.year, value.month, value.day)


def _utc_instant(value: object, argument: str) -> datetime:
    """``to_utc(value)``, with a ``TypeError`` that names the argument and stays one line."""
    if not isinstance(value, datetime):
        raise TypeError(f"{argument} must be a datetime, got {_type_name(value)}")
    return to_utc(value)


def _type_name(value: object) -> str:
    """The type name of ``value`` for messages: a short identifier, never arbitrary text."""
    name = type(value).__name__
    if name.isidentifier() and name.isascii() and len(name) <= _MAX_TYPE_NAME_LENGTH:
        return name
    return "an unnamed type"
