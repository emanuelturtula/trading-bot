"""Property tests over drawn frames and instants for the market data pipeline (spec 010, T15).

Hypothesis draws NYSE grid frames, ``now`` at microsecond resolution and raw frames with random
row-level defects, and checks invariants that must hold for *every* draw, not only the literal
tables of ``test_closed_candles.py``, ``test_candle_normalization.py`` and
``test_market_data_pipeline.py``:

- ``drop_open_candle`` returns a prefix whose rows close at or before ``now`` (and whose first
  dropped row, if any, closes after ``now``), is monotonic in ``now`` and is unaffected by rows
  appended after the frame (AC6-AC9);
- ``normalize_candles`` accounts for every input row exactly once, is idempotent and is
  independent of row order when labels are unique (AC3, AC4);
- ``prepare_candles`` either returns a frame meeting the ``fetch_candles`` contract
  (``assert_closed_candles``) or raises one of its three documented failures (AC15).

No network, no wall clock: every instant is drawn or a literal, and the NYSE test calendar is
built once per process. Drawn windows stay comfortably inside the calendar's coverage
(2021-01-01 to 2027-12-31), so no draw needs to tolerate a ``CalendarRangeError`` from
insufficient history: a raise there would be a real defect, not an expected outcome.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from functools import cache

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fixtures.calendars import nyse_test_calendar
from tests.fixtures.provider_contract import assert_closed_candles
from tests.fixtures.session_candles import session_candles
from trading_bot.data.errors import (
    CandleNotPublishedError,
    NoDataError,
    ProviderDataError,
)
from trading_bot.data.pipeline import prepare_candles
from trading_bot.data.provider import CandleRequest
from trading_bot.domain.candle_normalization import normalize_candles
from trading_bot.domain.candles import OHLCV_COLUMNS
from trading_bot.domain.closed_candles import drop_open_candle
from trading_bot.domain.timeframe import Timeframe

TIMEFRAMES = list(Timeframe)
NYSE = nyse_test_calendar()
VALID: tuple[float, float, float, float, float] = (100.0, 101.0, 99.0, 100.5, 1000.0)
_SETTINGS = {"max_examples": 25, "deadline": None}

# A window comfortably inside the NYSE test calendar's coverage: every timeframe already has
# closed candles well before it, and the calendar still covers years past it.
_WINDOW_START = datetime(2024, 1, 1, tzinfo=UTC)
_WINDOW_END = datetime(2024, 2, 1, tzinfo=UTC)
_EXTENDED_END = datetime(2024, 2, 11, tzinfo=UTC)


@cache
def _grid(timeframe: Timeframe) -> pd.DataFrame:
    return session_candles(NYSE, timeframe, _WINDOW_START, _WINDOW_END)


@cache
def _extended_grid(timeframe: Timeframe) -> pd.DataFrame:
    """The same grid continued ten further days: a pool of rows to append after it."""
    return session_candles(NYSE, timeframe, _WINDOW_START, _EXTENDED_END)


def _now_strategy() -> st.SearchStrategy[datetime]:
    """Instants at microsecond resolution around the drawn grids' span."""
    lower = (_WINDOW_START - timedelta(days=2)).replace(tzinfo=None)
    upper = (_WINDOW_END + timedelta(days=2)).replace(tzinfo=None)
    return st.datetimes(min_value=lower, max_value=upper).map(
        lambda naive: naive.replace(tzinfo=UTC)
    )


def _now_before_window_end() -> st.SearchStrategy[datetime]:
    """Instants that close no slot of ``_extended_grid`` past ``_grid`` (Design 3.1 duration > 0).

    Every appended row opens at or after ``_WINDOW_END`` and so closes strictly after it, which
    keeps the "appended rows never change the result" property from depending on ``now``.
    """
    lower = (_WINDOW_START - timedelta(days=2)).replace(tzinfo=None)
    upper = _WINDOW_END.replace(tzinfo=None)
    return st.datetimes(min_value=lower, max_value=upper).map(
        lambda naive: naive.replace(tzinfo=UTC)
    )


# --- drop_open_candle: prefix, closedness and monotonicity (AC6-AC9) ----------------------------


@settings(**_SETTINGS)
@given(now=_now_strategy(), timeframe=st.sampled_from(TIMEFRAMES))
def test_drop_open_candle_returns_a_prefix_closed_at_or_before_now(
    now: datetime, timeframe: Timeframe
) -> None:
    frame = _grid(timeframe)

    result = drop_open_candle(frame, timeframe, now, calendar=NYSE)

    pd.testing.assert_frame_equal(result, frame.iloc[: len(result)], check_exact=True)
    for label in result.index:
        assert NYSE.candle_slot(timeframe, label).close_time <= now
    if len(result) < len(frame):
        next_label = frame.index[len(result)]
        assert NYSE.candle_slot(timeframe, next_label).close_time > now


@settings(**_SETTINGS)
@given(pair=st.tuples(_now_strategy(), _now_strategy()), timeframe=st.sampled_from(TIMEFRAMES))
def test_drop_open_candle_is_monotonic_in_now(
    pair: tuple[datetime, datetime], timeframe: Timeframe
) -> None:
    earlier, later = sorted(pair)
    frame = _grid(timeframe)

    smaller = drop_open_candle(frame, timeframe, earlier, calendar=NYSE)
    larger = drop_open_candle(frame, timeframe, later, calendar=NYSE)

    assert len(smaller) <= len(larger)
    pd.testing.assert_frame_equal(smaller, larger.iloc[: len(smaller)], check_exact=True)


@settings(**_SETTINGS)
@given(
    now=_now_before_window_end(),
    timeframe=st.sampled_from(TIMEFRAMES),
    extra=st.integers(min_value=0, max_value=20),
)
def test_drop_open_candle_is_unaffected_by_rows_appended_after_the_frame(
    now: datetime, timeframe: Timeframe, extra: int
) -> None:
    frame = _grid(timeframe)
    pool = _extended_grid(timeframe)
    appended = pool.iloc[len(frame) : len(frame) + extra]
    extended = pd.concat([frame, appended])

    base = drop_open_candle(frame, timeframe, now, calendar=NYSE)
    with_more_rows = drop_open_candle(extended, timeframe, now, calendar=NYSE)

    pd.testing.assert_frame_equal(base, with_more_rows, check_exact=True)


# --- normalize_candles: accounting, idempotency and order independence (AC3, AC4) ---------------

_DEFECT_KINDS: tuple[str, ...] = (
    "none",
    "missing_value",
    "infinite_value",
    "non_positive_price",
    "negative_volume",
    "inconsistent_range",
)


def _apply_defect(kind: str) -> tuple[float, float, float, float, float]:
    open_, high, low, close, volume = VALID
    match kind:
        case "missing_value":
            return (math.nan, high, low, close, volume)
        case "infinite_value":
            return (math.inf, high, low, close, volume)
        case "non_positive_price":
            return (0.0, high, low, close, volume)
        case "negative_volume":
            return (open_, high, low, close, -1.0)
        case "inconsistent_range":
            return (open_, low - 1.0, low, close, volume)
        case _:
            return (open_, high, low, close, volume)


@cache
def _defect_window(timeframe: Timeframe) -> pd.DataFrame:
    """A small NYSE grid slice with unique labels: several rows, small enough for fast draws."""
    return session_candles(NYSE, timeframe, _WINDOW_START, _WINDOW_START + timedelta(days=4))


def _build_defect_frame(window: pd.DataFrame, kinds: list[str]) -> pd.DataFrame:
    rows = [_apply_defect(kind) for kind in kinds]
    return pd.DataFrame(
        np.asarray(rows, dtype=np.float64), index=window.index, columns=list(OHLCV_COLUMNS)
    )


def _kinds_strategy(length: int) -> st.SearchStrategy[list[str]]:
    return st.lists(st.sampled_from(_DEFECT_KINDS), min_size=length, max_size=length)


@settings(**_SETTINGS)
@given(timeframe=st.sampled_from(TIMEFRAMES), data=st.data())
def test_every_row_is_kept_or_reported_exactly_once(
    timeframe: Timeframe, data: st.DataObject
) -> None:
    window = _defect_window(timeframe)
    kinds = data.draw(_kinds_strategy(len(window)))
    raw = _build_defect_frame(window, kinds)

    result = normalize_candles(raw, timeframe, calendar=NYSE)

    kept_labels = set(result.candles.index)
    dropped_positions = {row.position for row in result.dropped}
    expected_dropped = {position for position, kind in enumerate(kinds) if kind != "none"}
    assert dropped_positions == expected_dropped
    assert len(dropped_positions) + len(kept_labels) == len(window)
    for position, kind in enumerate(kinds):
        assert (window.index[position] in kept_labels) == (kind == "none")


@settings(**_SETTINGS)
@given(timeframe=st.sampled_from(TIMEFRAMES), data=st.data())
def test_normalization_of_drawn_frames_is_idempotent(
    timeframe: Timeframe, data: st.DataObject
) -> None:
    window = _defect_window(timeframe)
    kinds = data.draw(_kinds_strategy(len(window)))
    raw = _build_defect_frame(window, kinds)

    first = normalize_candles(raw, timeframe, calendar=NYSE)
    second = normalize_candles(first.candles, timeframe, calendar=NYSE)

    assert second.dropped == ()
    pd.testing.assert_frame_equal(second.candles, first.candles, check_exact=True)


@settings(**_SETTINGS)
@given(timeframe=st.sampled_from(TIMEFRAMES), data=st.data())
def test_normalization_is_independent_of_row_order_for_unique_labels(
    timeframe: Timeframe, data: st.DataObject
) -> None:
    window = _defect_window(timeframe)
    kinds = data.draw(_kinds_strategy(len(window)))
    raw = _build_defect_frame(window, kinds)
    order = data.draw(st.permutations(range(len(window))))
    shuffled = raw.iloc[list(order)]

    result = normalize_candles(raw, timeframe, calendar=NYSE)
    shuffled_result = normalize_candles(shuffled, timeframe, calendar=NYSE)

    pd.testing.assert_frame_equal(shuffled_result.candles, result.candles, check_exact=True)
    assert {(row.label, row.reason) for row in shuffled_result.dropped} == {
        (row.label, row.reason) for row in result.dropped
    }


# --- prepare_candles: the outcome contract holds for every draw (AC15) --------------------------


@cache
def _outcome_window(timeframe: Timeframe) -> pd.DataFrame:
    return session_candles(NYSE, timeframe, _WINDOW_START, _WINDOW_START + timedelta(days=10))


_LAST_ROW_KINDS: tuple[str, ...] = ("clean", "missing", "invalid")


@settings(**_SETTINGS)
@given(
    timeframe=st.sampled_from(TIMEFRAMES),
    lookback=st.integers(min_value=1, max_value=5),
    last_row=st.sampled_from(_LAST_ROW_KINDS),
)
def test_prepare_candles_meets_its_contract_or_raises_a_typed_error(
    timeframe: Timeframe, lookback: int, last_row: str
) -> None:
    window = _outcome_window(timeframe)
    now = NYSE.candle_slot(timeframe, window.index[-1]).close_time
    raw = window.copy(deep=True)
    if last_row == "invalid":
        raw.iloc[-1, raw.columns.get_loc("close")] = math.nan
    elif last_row == "missing":
        raw = raw.iloc[:-1]
    request = CandleRequest(ticker="AAPL", timeframe=timeframe, lookback=lookback, now=now)

    try:
        result = prepare_candles(raw, request, calendar=NYSE)
    except (NoDataError, CandleNotPublishedError, ProviderDataError):
        # Each of these is a MarketDataError (test_market_data_errors.py): the typed failure
        # itself is the expected outcome here, so there is nothing further to assert about it.
        return
    assert_closed_candles(result, request, calendar=NYSE)
