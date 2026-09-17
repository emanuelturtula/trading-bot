"""Property tests over drawn inputs for the Yahoo provider stack (spec 011, T18).

Hypothesis draws inputs the literal tables of T1-T9 do not cover and checks invariants that must
hold for every draw:

- ``resample_hourly_to_4h``: every returned row equals the aggregation of the hourly rows of its
  own ``4h`` slot and no other slot, only slots closed at ``now`` with at least one row appear,
  and rows already returned for an earlier ``now`` never change or disappear as ``now`` grows
  (CLAUDE.md rule 4);
- ``ProviderTransport.call``: its sleeps equal ``RetryPolicy.delay`` for the same arguments, it
  never makes more than ``max_attempts`` calls, and the last error instance is the one raised
  (AC17);
- ``TokenBucket.acquire``: over any window of ``w`` seconds between two granted acquisitions, the
  number of acquisitions granted never exceeds ``burst + rate * w`` (AC19);
- ``plan_history``: an intraday ``start`` is never earlier than ``now - INTRADAY_HISTORY``, is
  always a slot label of the requested timeframe, and ``capped`` holds exactly when the requested
  start is earlier than that bound (AC20);
- ``check_yahoo_symbol`` / ``HistoryQuery``: every symbol the former accepts also builds a
  ``HistoryQuery`` (AC10, AC9).

Every coroutine test builds its own ``ProviderTransport`` and ``TokenBucket`` inside the single
``asyncio.run`` call that uses them (decision D68): none is shared across two event loops. No
test reads the wall clock or sleeps for real; the transport gets a fake clock, jitter and sleep,
and the resampler and planning tests never touch the network or a clock at all.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from functools import cache

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fixtures.calendars import nyse_test_calendar, utc
from tests.fixtures.session_candles import session_candles
from trading_bot.data.errors import InvalidTickerError, ProviderFailure, ProviderUnavailableError
from trading_bot.data.provider import CandleRequest
from trading_bot.data.transport import ProviderTransport, RateLimit, RetryPolicy, TokenBucket
from trading_bot.data.yahoo.history import HistoryQuery
from trading_bot.data.yahoo.instruments import check_yahoo_symbol
from trading_bot.data.yahoo.provider import INTRADAY_HISTORY, plan_history
from trading_bot.domain.candle_resampling import resample_hourly_to_4h
from trading_bot.domain.market_calendar.sessions import CalendarRangeError, MarketCalendar
from trading_bot.domain.timeframe import Timeframe

NYSE = nyse_test_calendar()
H1 = Timeframe.H1
H4 = Timeframe.H4
_SETTINGS = {"max_examples": 25, "deadline": None}

_GRID_START = utc("2024-11-20T00:00")
_GRID_END = utc("2024-12-10T00:00")


@cache
def _full_grid() -> pd.DataFrame:
    return session_candles(NYSE, H1, _GRID_START, _GRID_END)


def _gapped_frame(drop_positions: frozenset[int]) -> pd.DataFrame:
    """The full grid with the rows of ``drop_positions`` removed."""
    grid = _full_grid()
    keep = np.ones(len(grid), dtype=np.bool_)
    for position in drop_positions:
        keep[position] = False
    return grid.iloc[keep]


def _drop_positions_strategy() -> st.SearchStrategy[frozenset[int]]:
    return st.frozensets(st.integers(min_value=0, max_value=len(_full_grid()) - 1), max_size=15)


def _now_strategy() -> st.SearchStrategy[datetime]:
    """Instants at microsecond resolution spanning the grid and a little past its end."""
    lower = _GRID_START.replace(tzinfo=None)
    upper = (_GRID_END + timedelta(days=2)).replace(tzinfo=None)
    return st.datetimes(min_value=lower, max_value=upper).map(
        lambda naive: naive.replace(tzinfo=UTC)
    )


def _reference_4h(
    frame: pd.DataFrame, now: datetime, calendar: MarketCalendar
) -> dict[datetime, tuple[float, float, float, float, float, int]]:
    """The ``4h`` aggregation of ``frame``'s rows closed by ``now``, built without the resampler.

    A reference computed with plain pandas group operations over the calendar's own ``4h`` grid,
    independent of ``resample_hourly_to_4h``'s internal algorithm.
    """
    last = calendar.closed_candles(H4, now, 1)[-1]
    labels = pd.DatetimeIndex(frame.index)
    considered = frame[labels < pd.Timestamp(last.close_time)]
    if len(considered) == 0:
        return {}
    considered_labels = pd.DatetimeIndex(considered.index)
    blocks = calendar.candle_slots(H4, considered_labels.min() - H4.duration, last.close_time)
    aggregates: dict[datetime, tuple[float, float, float, float, float, int]] = {}
    for block in blocks:
        mask = np.asarray(
            (considered_labels >= pd.Timestamp(block.open_time))
            & (considered_labels < pd.Timestamp(block.close_time)),
            dtype=np.bool_,
        )
        rows = considered[mask]
        if len(rows) == 0:
            continue
        ordered = rows.sort_index()
        aggregates[block.label] = (
            float(ordered["open"].iloc[0]),
            float(ordered["high"].max()),
            float(ordered["low"].min()),
            float(ordered["close"].iloc[-1]),
            float(ordered["volume"].sum()),
            len(ordered),
        )
    return aggregates


# --- Resampler (AC5, AC7) ------------------------------------------------------------------------


@settings(**_SETTINGS)
@given(drop_positions=_drop_positions_strategy(), now=_now_strategy())
def test_every_resampled_row_is_the_aggregation_of_its_own_closed_slot_and_no_other(
    drop_positions: frozenset[int], now: datetime
) -> None:
    frame = _gapped_frame(drop_positions)

    result = resample_hourly_to_4h(frame, now, calendar=NYSE)

    reference = _reference_4h(frame, now, NYSE)
    result_labels = {pd.Timestamp(label).to_pydatetime() for label in result.candles.index}
    assert result_labels == set(reference)
    for entry, label in zip(result.slots, result.candles.index, strict=True):
        assert entry.slot.close_time <= now  # only closed slots appear
        key = pd.Timestamp(label).to_pydatetime()
        expected_open, expected_high, expected_low, expected_close, expected_volume, bars = (
            reference[key]
        )
        row = result.candles.loc[label]
        assert row["open"] == pytest.approx(expected_open, rel=1e-9, abs=1e-9)
        assert row["high"] == pytest.approx(expected_high, rel=1e-9, abs=1e-9)
        assert row["low"] == pytest.approx(expected_low, rel=1e-9, abs=1e-9)
        assert row["close"] == pytest.approx(expected_close, rel=1e-9, abs=1e-9)
        assert row["volume"] == pytest.approx(expected_volume, rel=1e-9, abs=1e-9)
        assert entry.bars == bars


@settings(**_SETTINGS)
@given(drop_positions=_drop_positions_strategy(), pair=st.tuples(_now_strategy(), _now_strategy()))
def test_resampled_rows_are_final_and_unaffected_by_data_published_after_now(
    drop_positions: frozenset[int], pair: tuple[datetime, datetime]
) -> None:
    earlier, later = sorted(pair)
    frame = _gapped_frame(drop_positions)

    small = resample_hourly_to_4h(frame, earlier, calendar=NYSE)
    big = resample_hourly_to_4h(frame, later, calendar=NYSE)

    assert len(small.candles) <= len(big.candles)
    pd.testing.assert_frame_equal(
        small.candles, big.candles.iloc[: len(small.candles)], check_exact=True
    )
    assert small.slots == big.slots[: len(small.slots)]


# --- Transport (AC16, AC17) -----------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay
        await asyncio.sleep(0)


class _RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _policy_strategy() -> st.SearchStrategy[RetryPolicy]:
    # base_delay <= 5.0 <= max_delay and rate_limited_delay <= 5.0 <= max_delay by construction,
    # so every draw is a valid RetryPolicy without a rejection filter.
    return st.builds(
        RetryPolicy,
        max_attempts=st.integers(min_value=1, max_value=5),
        base_delay=st.floats(min_value=0.001, max_value=5.0, allow_nan=False, allow_infinity=False),
        max_delay=st.floats(min_value=5.0, max_value=30.0, allow_nan=False, allow_infinity=False),
        rate_limited_delay=st.floats(
            min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False
        ),
        attempt_timeout=st.just(20.0),
    )


@settings(**_SETTINGS)
@given(data=st.data())
def test_transport_sleeps_match_the_policy_delay_and_never_exceed_max_attempts(
    data: st.DataObject,
) -> None:
    policy = data.draw(_policy_strategy())
    jitter_value = data.draw(st.floats(min_value=0.0, max_value=0.999999, allow_nan=False))
    failure = data.draw(st.sampled_from(list(ProviderFailure)))
    retry_after = data.draw(
        st.one_of(st.none(), st.floats(min_value=0.0, max_value=40.0, allow_nan=False))
    )
    fail_count = data.draw(st.integers(min_value=0, max_value=policy.max_attempts + 2))
    error = ProviderUnavailableError(
        failure,
        ticker="SPY",
        retry_after=None if retry_after is None else timedelta(seconds=retry_after),
    )
    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        if len(calls) <= fail_count:
            raise error
        return "ok"

    # A reference simulation of `call`'s documented control flow (Design 9.3), using
    # `policy.delay` itself as the oracle for each single-attempt decision.
    expected_sleeps: list[float] = []
    attempt = 1
    while True:
        if attempt > fail_count:
            expected_result: str | None = "ok"
            expected_calls = attempt
            break
        below_cap = attempt < policy.max_attempts
        delay = policy.delay(attempt, error, jitter_value) if below_cap else None
        if delay is None:
            expected_result = None
            expected_calls = attempt
            break
        expected_sleeps.append(delay)
        attempt += 1

    clock = _FakeClock()
    sleep = _RecordingSleep()
    transport = ProviderTransport(
        policy=policy,
        limiter=TokenBucket(RateLimit(rate=1000.0, burst=1000), clock=clock, sleep=clock.sleep),
        sleep=sleep,
        jitter=lambda: jitter_value,
    )
    caught_error: ProviderUnavailableError | None = None

    async def scenario() -> str | None:
        nonlocal caught_error
        try:
            result: str = await transport.call(operation, ticker="SPY", timeframe=None)
        except ProviderUnavailableError as caught:
            caught_error = caught
            return None
        return result

    result = asyncio.run(scenario())

    assert len(calls) == expected_calls
    assert len(calls) <= policy.max_attempts
    assert sleep.delays == expected_sleeps
    assert result == expected_result
    assert caught_error is (None if expected_result is not None else error)


# --- Bucket (AC19) --------------------------------------------------------------------------------


class _ClockStalledError(Exception):
    """A fake-clock float-precision artifact, not a ``TokenBucket`` property to check.

    A naive test clock advances by plain ``float`` addition. At a large enough magnitude, an
    extremely small top-up delay (``(1 - tokens) / rate`` for a ``tokens`` value that has drifted
    to within a float ULP of ``1.0``) can leave ``now`` unchanged, which would spin the strategy
    forever. Real deployments never hit this: ``asyncio``'s running-loop clock is re-read from the
    OS on every call rather than accumulated in Python, so it always advances by some real amount,
    if only the platform timer's own resolution. This sentinel turns the artifact into a discarded
    example instead of a hang.
    """


class _BoundedFakeClock:
    _STALL_LIMIT = 1_000

    def __init__(self) -> None:
        self.now = 1000.0
        self._stalled = 0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        before = self.now
        self.now += delay
        self._stalled = 0 if self.now != before else self._stalled + 1
        if self._stalled > self._STALL_LIMIT:
            raise _ClockStalledError
        await asyncio.sleep(0)


def _rate_limit_strategy() -> st.SearchStrategy[RateLimit]:
    return st.builds(
        RateLimit,
        rate=st.floats(min_value=0.1, max_value=10.0, allow_nan=False, allow_infinity=False),
        burst=st.integers(min_value=1, max_value=10),
    )


@settings(**_SETTINGS)
@given(limit=_rate_limit_strategy(), count=st.integers(min_value=1, max_value=25))
def test_bucket_acquisitions_in_any_window_never_exceed_burst_plus_rate_times_the_window(
    limit: RateLimit, count: int
) -> None:
    clock = _BoundedFakeClock()
    bucket = TokenBucket(limit, clock=clock, sleep=clock.sleep)
    times: list[float] = []

    async def scenario() -> None:
        for _ in range(count):
            await bucket.acquire()
            times.append(clock.now)

    try:
        asyncio.run(scenario())
    except _ClockStalledError:
        return  # see _ClockStalledError: a fake-clock artifact, not a property violation

    tolerance = 1e-9
    for start in range(len(times)):
        for end in range(start, len(times)):
            window = times[end] - times[start]
            granted = end - start + 1
            assert granted <= limit.burst + limit.rate * window + tolerance


# --- Planning (AC20) ------------------------------------------------------------------------------


def _planning_now_strategy() -> st.SearchStrategy[datetime]:
    lower = datetime(2024, 1, 1)
    upper = datetime(2026, 12, 31, 23, 59, 59)
    return st.datetimes(min_value=lower, max_value=upper).map(
        lambda naive: naive.replace(tzinfo=UTC)
    )


@settings(**_SETTINGS)
@given(
    now=_planning_now_strategy(),
    timeframe=st.sampled_from([H1, H4]),
    lookback=st.integers(min_value=1, max_value=5000),
)
def test_plan_history_never_starts_earlier_than_the_720_day_cap(
    now: datetime, timeframe: Timeframe, lookback: int
) -> None:
    request = CandleRequest(ticker="SPY", timeframe=timeframe, lookback=lookback, now=now)

    try:
        plan = plan_history(request, calendar=NYSE)
    except CalendarRangeError:
        return  # the window reaches before the test calendar: not a planning outcome to check

    earliest = now - INTRADAY_HISTORY
    assert plan.start >= earliest
    assert NYSE.candle_slot(timeframe, plan.start).label == plan.start
    assert plan.capped is (plan.requested_start < earliest)


# --- Symbols (AC9, AC10) --------------------------------------------------------------------------


@settings(**_SETTINGS)
@given(
    symbol=st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=0, max_size=40
    )
)
def test_every_symbol_check_yahoo_symbol_accepts_builds_a_history_query(symbol: str) -> None:
    try:
        checked = check_yahoo_symbol(symbol)
    except InvalidTickerError:
        return

    assert checked == symbol
    query = HistoryQuery(symbol=symbol, interval="1h", start=utc("2025-01-01T00:00"))
    assert query.symbol == symbol
