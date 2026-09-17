"""Anti look-ahead tests of normalization and open-candle removal (spec 010, T7: AC5, AC9).

CLAUDE.md rule 4: the result at candle ``t`` computed with ``data[:t]`` must equal the one
computed with ``data[:t+k]`` truncated to ``t``. Both functions return frames indexed by labels
of their input, so they go through ``assert_no_lookahead`` with a full sweep of cuts
(``max_cuts=len(frame)``) and exact comparisons. Two control functions that peek at the last row
prove the check is not vacuous.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.calendars import nyse_test_calendar, utc
from tests.fixtures.session_candles import session_candles
from tests.lookahead import LookaheadError, assert_no_lookahead
from trading_bot.domain.candle_normalization import DropReason, normalize_candles
from trading_bot.domain.candles import OHLCV_COLUMNS
from trading_bot.domain.closed_candles import drop_open_candle
from trading_bot.domain.timeframe import Timeframe

NYSE = nyse_test_calendar()
NOWS = ("2024-07-03T17:00", "2024-07-05T14:30", "2024-07-09T20:00")
VALID = (100.0, 101.0, 99.0, 100.5, 1000.0)

# Labels (UTC) that the calendar rejects for each timeframe, with the expected reason.
CALENDAR_REJECTIONS: dict[Timeframe, tuple[tuple[str, DropReason], ...]] = {
    Timeframe.H1: (
        ("2024-07-04T14:30", DropReason.NOT_A_SESSION),
        ("2024-07-02T12:00", DropReason.OUTSIDE_SESSION),
        ("2024-07-02T14:00", DropReason.OFF_GRID),
    ),
    Timeframe.H4: (
        ("2024-07-04T13:30", DropReason.NOT_A_SESSION),
        ("2024-07-02T12:00", DropReason.OUTSIDE_SESSION),
        ("2024-07-02T15:30", DropReason.OFF_GRID),
    ),
    Timeframe.D1: (
        ("2024-07-04T04:00", DropReason.NOT_A_SESSION),
        ("2024-07-03T00:00", DropReason.OFF_GRID),
    ),
}


@cache
def grid_frame(timeframe: Timeframe) -> pd.DataFrame:
    """Grid candles with labels from 2024-06-24T00:00Z to 2024-07-10T00:00Z."""
    return session_candles(NYSE, timeframe, utc("2024-06-24T00:00"), utc("2024-07-10T00:00"))


def raw_with_bad_rows(timeframe: Timeframe) -> pd.DataFrame:
    """A sorted ``ns`` raw frame with unique labels: grid rows plus every row-level defect.

    Every reason except ``missing_timestamp``, ``duplicate`` and ``conflicting_duplicate``, which
    need ``NaT`` or repeated labels that the harness does not accept (T4 covers them).
    """
    grid = grid_frame(timeframe)
    frame = grid.copy()
    frame.index = pd.DatetimeIndex(frame.index).as_unit("ns")
    values = frame.to_numpy(dtype=np.float64, copy=True)
    # Value defects on grid rows, spread across the frame.
    open_, high, low, close, volume = range(len(OHLCV_COLUMNS))
    positions = np.linspace(2, len(frame) - 2, 5).astype(int)
    values[positions[0], close] = np.nan
    values[positions[1], high] = np.inf
    values[positions[2], open_] = 0.0
    values[positions[3], volume] = -1.0
    values[positions[4], high] = values[positions[4], low] / 2.0
    frame = pd.DataFrame(values, index=frame.index, columns=list(OHLCV_COLUMNS))

    extra_labels = [pd.Timestamp("2020-12-31T15:30", tz="UTC")]
    extra_labels.append(pd.Timestamp(grid.index[3]).as_unit("ns") + pd.Timedelta(nanoseconds=1))
    extra_labels.extend(
        pd.Timestamp(label, tz="UTC") for label, _ in CALENDAR_REJECTIONS[timeframe]
    )
    extra = pd.DataFrame(
        np.tile(np.asarray(VALID, dtype=np.float64), (len(extra_labels), 1)),
        index=pd.DatetimeIndex(extra_labels, tz="UTC").as_unit("ns"),
        columns=list(OHLCV_COLUMNS),
    )
    return pd.concat([frame, extra]).sort_index(kind="stable")


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_the_raw_frame_mixes_valid_rows_with_every_harness_compatible_reason(
    timeframe: Timeframe,
) -> None:
    raw = raw_with_bad_rows(timeframe)

    result = normalize_candles(raw, timeframe, calendar=NYSE)

    assert raw.index.is_unique
    assert raw.index.is_monotonic_increasing
    expected = {
        DropReason.OUTSIDE_CALENDAR,
        DropReason.OFF_GRID,
        DropReason.NOT_A_SESSION,
        DropReason.MISSING_VALUE,
        DropReason.INFINITE_VALUE,
        DropReason.NON_POSITIVE_PRICE,
        DropReason.NEGATIVE_VOLUME,
        DropReason.INCONSISTENT_RANGE,
    }
    if timeframe is not Timeframe.D1:
        expected.add(DropReason.OUTSIDE_SESSION)
    assert {row.reason for row in result.dropped} == expected
    assert len(result.candles) == len(grid_frame(timeframe)) - 5


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_normalize_candles_has_no_lookahead(timeframe: Timeframe) -> None:
    raw = raw_with_bad_rows(timeframe)

    def normalized(frame: pd.DataFrame) -> pd.DataFrame:
        return normalize_candles(frame, timeframe, calendar=NYSE).candles

    report = assert_no_lookahead(normalized, raw, max_cuts=len(raw))

    assert report.cuts == tuple(range(1, len(raw)))
    assert report.non_missing_values > 0


@pytest.mark.parametrize("now", NOWS)
@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_drop_open_candle_has_no_lookahead(timeframe: Timeframe, now: str) -> None:
    frame = grid_frame(timeframe)
    instant = utc(now)

    def closed(candles: pd.DataFrame) -> pd.DataFrame:
        return drop_open_candle(candles, timeframe, instant, calendar=NYSE)

    report = assert_no_lookahead(closed, frame, max_cuts=len(frame))

    assert report.cuts == tuple(range(1, len(frame)))
    assert report.non_missing_values > 0


def _always_drops_the_last_row(candles: pd.DataFrame) -> pd.DataFrame:
    return candles.iloc[:-1]


def _keeps_rows_before_the_last_label(candles: pd.DataFrame) -> pd.DataFrame:
    return candles[candles.index < candles.index[-1]]


@pytest.mark.parametrize("control", [_always_drops_the_last_row, _keeps_rows_before_the_last_label])
@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_controls_that_read_the_last_row_are_caught_as_result_appeared(
    control: Callable[[pd.DataFrame], pd.DataFrame], timeframe: Timeframe
) -> None:
    frame = grid_frame(timeframe)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(control, frame, max_cuts=len(frame))

    assert caught.value.violation.kind == "result_appeared"
