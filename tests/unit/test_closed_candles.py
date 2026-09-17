"""Tests of open-candle removal (spec 010, T5-T6: AC6-AC8).

``drop_open_candle`` keeps the rows labelled at or before the label of the last slot closed at
``now`` and checks the last returned label with the calendar. Every expected label is a literal
from the spec tables (Design 4.2 and 4.3); nothing is recomputed with the code under test.
"""

from __future__ import annotations

from functools import cache

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.calendars import NEW_YORK, nyse_test_calendar, toy_calendar, utc
from tests.fixtures.session_candles import session_candles
from trading_bot.domain import closed_candles as closed_candles_module
from trading_bot.domain.candles import OHLCV_COLUMNS, CandleValidationError
from trading_bot.domain.closed_candles import drop_open_candle
from trading_bot.domain.market_calendar.sessions import (
    CalendarRangeError,
    CandleLabelError,
    CandleLabelErrorKind,
)
from trading_bot.domain.timeframe import Timeframe

H1 = Timeframe.H1
H4 = Timeframe.H4
D1 = Timeframe.D1
NYSE = nyse_test_calendar()
CANONICAL_INDEX = pd.DatetimeTZDtype(unit="us", tz="UTC")
VALID = (100.0, 101.0, 99.0, 100.5, 1000.0)


def grid(timeframe: Timeframe, start: str, end: str) -> pd.DataFrame:
    return session_candles(NYSE, timeframe, utc(start), utc(end))


@cache
def golden_grid(timeframe: Timeframe) -> pd.DataFrame:
    """The Design 4.2 grid frame: labels in ``[2023-12-27T00:00Z, 2024-11-06T00:00Z)``."""
    return grid(timeframe, "2023-12-27T00:00", "2024-11-06T00:00")


def frame_of(labels: list[str], *, unit: str = "us") -> pd.DataFrame:
    """A valid frame with the given UTC wall-time labels and constant candle values."""
    stamps = [pd.Timestamp(label, tz="UTC") for label in labels]
    index = pd.DatetimeIndex(stamps, tz="UTC").as_unit(unit)
    values = np.tile(np.asarray(VALID, dtype=np.float64), (len(labels), 1))
    return pd.DataFrame(values, index=index, columns=list(OHLCV_COLUMNS))


def iso_labels(frame: pd.DataFrame) -> list[str]:
    return [label.isoformat() for label in frame.index]


# --- T5: contract (AC6, AC8) -------------------------------------------------------------------


def test_the_module_exports_exactly_drop_open_candle() -> None:
    assert closed_candles_module.__all__ == ["drop_open_candle"]


@pytest.mark.parametrize(
    ("timeframe", "calendar"),
    [
        pytest.param("1h", NYSE, id="timeframe-code"),
        pytest.param(None, NYSE, id="timeframe-none"),
        pytest.param(H1, None, id="calendar-none"),
        pytest.param(H1, "XNYS", id="calendar-name"),
    ],
)
def test_a_non_timeframe_or_non_calendar_raises_type_error_first(
    timeframe: object, calendar: object
) -> None:
    with pytest.raises(TypeError):
        drop_open_candle("not a frame", timeframe, "not a datetime", calendar=calendar)  # type: ignore[arg-type]


def test_now_is_checked_before_the_frame() -> None:
    with pytest.raises(TypeError):
        drop_open_candle("not a frame", H1, "2024-07-02T20:00Z", calendar=NYSE)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="naive"):
        drop_open_candle("not a frame", H1, pd.Timestamp("2024-07-02 20:00"), calendar=NYSE)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sub-microsecond"):
        drop_open_candle(
            "not a frame",  # type: ignore[arg-type]
            H1,
            pd.Timestamp("2024-07-02 20:00:00.000000001", tz="UTC"),
            calendar=NYSE,
        )


def test_the_frame_is_validated_before_the_calendar_is_asked() -> None:
    unsorted = frame_of(["2024-07-02T14:30", "2024-07-02T13:30"])
    outside = utc("2030-01-01T00:00")

    with pytest.raises(TypeError):
        drop_open_candle(unsorted["close"], H1, outside, calendar=NYSE)  # type: ignore[arg-type]
    with pytest.raises(CandleValidationError):
        drop_open_candle(unsorted, H1, outside, calendar=NYSE)


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_no_slot_closed_yet_raises_calendar_range_error_even_for_an_empty_frame(
    timeframe: Timeframe,
) -> None:
    with pytest.raises(CalendarRangeError):
        drop_open_candle(frame_of([]), timeframe, utc("2024-03-07T15:00"), calendar=toy_calendar())


@pytest.mark.parametrize("timeframe", list(Timeframe))
@pytest.mark.parametrize(
    "now",
    [
        # Design 4.3: the NYSE test calendar covers [2021-01-01T05:00Z, 2028-01-01T05:00Z).
        pytest.param("2028-01-01T05:00", id="exactly-coverage-end"),
        pytest.param("2028-01-02T00:00", id="after-coverage-end"),
        pytest.param("2021-01-01T04:59:59.999999", id="one-microsecond-before-coverage-start"),
    ],
)
def test_now_outside_the_coverage_raises_calendar_range_error(
    timeframe: Timeframe, now: str
) -> None:
    frame = grid(timeframe, "2024-07-01T00:00", "2024-07-03T00:00")

    with pytest.raises(CalendarRangeError):
        drop_open_candle(frame, timeframe, utc(now), calendar=NYSE)


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_now_at_midnight_utc_of_2028_is_still_inside_the_coverage(timeframe: Timeframe) -> None:
    # 19:00 ET on 2027-12-31, a regular session: New Year's Day 2028 falls on a Saturday.
    frame = grid(timeframe, "2024-07-01T00:00", "2024-07-03T00:00")

    result = drop_open_candle(frame, timeframe, utc("2028-01-01T00:00"), calendar=NYSE)

    pd.testing.assert_frame_equal(result, frame, check_exact=True)


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_the_result_is_a_new_prefix_with_the_same_dtypes_and_index_unit(unit: str) -> None:
    frame = grid(H1, "2024-07-02T00:00", "2024-07-06T00:00")
    frame.index = pd.DatetimeIndex(frame.index).as_unit(unit)  # type: ignore[arg-type]
    frame.index.name = "labels"
    before = frame.copy(deep=True)

    result = drop_open_candle(frame, H1, utc("2024-07-02T20:00"), calendar=NYSE)

    assert result is not frame
    assert result.index.dtype == pd.DatetimeTZDtype(unit=unit, tz="UTC")  # type: ignore[arg-type]
    assert result.index.name == "labels"
    assert result.dtypes.tolist() == frame.dtypes.tolist()
    pd.testing.assert_frame_equal(result, frame.iloc[:7], check_exact=True)
    pd.testing.assert_frame_equal(frame, before, check_exact=True)


def test_a_frame_that_is_entirely_closed_is_returned_whole_as_a_new_object() -> None:
    frame = grid(D1, "2024-07-01T00:00", "2024-07-04T00:00")

    result = drop_open_candle(frame, D1, utc("2024-07-06T00:00"), calendar=NYSE)

    assert result is not frame
    pd.testing.assert_frame_equal(result, frame, check_exact=True)


def test_now_may_be_a_timestamp_in_another_zone() -> None:
    frame = grid(H1, "2024-07-02T00:00", "2024-07-06T00:00")

    result = drop_open_candle(
        frame, H1, pd.Timestamp("2024-07-02 16:00", tz=NEW_YORK), calendar=NYSE
    )

    assert len(result) == 7


def test_the_kept_row_counts_at_one_microsecond_before_and_at_the_close() -> None:
    frame = grid(H1, "2024-07-02T00:00", "2024-07-06T00:00")
    assert len(frame) == 18

    before_close = drop_open_candle(frame, H1, utc("2024-07-02T19:59:59.999999"), calendar=NYSE)
    at_close = drop_open_candle(frame, H1, utc("2024-07-02T20:00"), calendar=NYSE)

    assert len(before_close) == 6
    assert len(at_close) == 7
    assert iso_labels(at_close)[-1] == "2024-07-02T19:30:00+00:00"


@pytest.mark.parametrize("unit", ["s", "us", "ns"])
def test_no_closed_row_gives_an_empty_frame_with_the_input_columns_and_index_dtype(
    unit: str,
) -> None:
    frame = grid(H1, "2024-07-05T00:00", "2024-07-06T00:00")
    frame.index = pd.DatetimeIndex(frame.index).as_unit(unit)  # type: ignore[arg-type]

    result = drop_open_candle(frame, H1, utc("2024-07-05T14:00"), calendar=NYSE)

    assert len(frame) == 7
    assert len(result) == 0
    assert list(result.columns) == list(OHLCV_COLUMNS)
    assert result.dtypes.tolist() == [np.dtype(np.float64)] * 5
    assert result.index.dtype == pd.DatetimeTZDtype(unit=unit, tz="UTC")  # type: ignore[arg-type]


def test_the_spec_empty_case_has_float64_columns_and_a_us_utc_index() -> None:
    frame = grid(H1, "2024-07-05T00:00", "2024-07-06T00:00")

    result = drop_open_candle(frame, H1, utc("2024-07-05T14:00"), calendar=NYSE)

    assert len(result) == 0
    assert result.dtypes.tolist() == [np.dtype(np.float64)] * 5
    assert result.index.dtype == CANONICAL_INDEX


def test_an_empty_frame_gives_an_empty_frame() -> None:
    frame = frame_of([])

    result = drop_open_candle(frame, D1, utc("2024-07-05T21:00"), calendar=NYSE)

    assert result is not frame
    assert len(result) == 0
    assert list(result.columns) == list(OHLCV_COLUMNS)


# --- T6: tables (AC7, AC8) ---------------------------------------------------------------------

# now -> (1h, 4h, 1d) last kept label and its real close, all UTC (Design 4.2).
GOLDEN_TABLE: list[tuple[str, tuple[str, str], tuple[str, str], tuple[str, str]]] = [
    (
        "2024-07-02T19:59:59.999999",
        ("2024-07-02T18:30", "2024-07-02T19:30"),
        ("2024-07-02T13:30", "2024-07-02T17:30"),
        ("2024-07-01T04:00", "2024-07-01T20:00"),
    ),
    (
        "2024-07-02T20:00",
        ("2024-07-02T19:30", "2024-07-02T20:00"),
        ("2024-07-02T17:30", "2024-07-02T20:00"),
        ("2024-07-02T04:00", "2024-07-02T20:00"),
    ),
    (
        "2024-07-03T16:59:59.999999",
        ("2024-07-03T15:30", "2024-07-03T16:30"),
        ("2024-07-02T17:30", "2024-07-02T20:00"),
        ("2024-07-02T04:00", "2024-07-02T20:00"),
    ),
    (
        "2024-07-03T17:00",
        ("2024-07-03T16:30", "2024-07-03T17:00"),
        ("2024-07-03T13:30", "2024-07-03T17:00"),
        ("2024-07-03T04:00", "2024-07-03T17:00"),
    ),
    (
        "2024-07-04T15:00",
        ("2024-07-03T16:30", "2024-07-03T17:00"),
        ("2024-07-03T13:30", "2024-07-03T17:00"),
        ("2024-07-03T04:00", "2024-07-03T17:00"),
    ),
    (
        "2024-07-05T13:30",
        ("2024-07-03T16:30", "2024-07-03T17:00"),
        ("2024-07-03T13:30", "2024-07-03T17:00"),
        ("2024-07-03T04:00", "2024-07-03T17:00"),
    ),
    (
        "2024-07-05T14:30",
        ("2024-07-05T13:30", "2024-07-05T14:30"),
        ("2024-07-03T13:30", "2024-07-03T17:00"),
        ("2024-07-03T04:00", "2024-07-03T17:00"),
    ),
    (
        "2024-07-06T12:00",
        ("2024-07-05T19:30", "2024-07-05T20:00"),
        ("2024-07-05T17:30", "2024-07-05T20:00"),
        ("2024-07-05T04:00", "2024-07-05T20:00"),
    ),
    (
        "2024-07-08T13:29:59.999999",
        ("2024-07-05T19:30", "2024-07-05T20:00"),
        ("2024-07-05T17:30", "2024-07-05T20:00"),
        ("2024-07-05T04:00", "2024-07-05T20:00"),
    ),
    (
        "2024-03-08T20:59:59.999999",
        ("2024-03-08T19:30", "2024-03-08T20:30"),
        ("2024-03-08T14:30", "2024-03-08T18:30"),
        ("2024-03-07T05:00", "2024-03-07T21:00"),
    ),
    (
        "2024-03-08T21:00",
        ("2024-03-08T20:30", "2024-03-08T21:00"),
        ("2024-03-08T18:30", "2024-03-08T21:00"),
        ("2024-03-08T05:00", "2024-03-08T21:00"),
    ),
    (
        "2024-03-11T14:29:59.999999",
        ("2024-03-08T20:30", "2024-03-08T21:00"),
        ("2024-03-08T18:30", "2024-03-08T21:00"),
        ("2024-03-08T05:00", "2024-03-08T21:00"),
    ),
    (
        "2024-03-11T14:30",
        ("2024-03-11T13:30", "2024-03-11T14:30"),
        ("2024-03-08T18:30", "2024-03-08T21:00"),
        ("2024-03-08T05:00", "2024-03-08T21:00"),
    ),
    (
        "2024-03-11T20:00",
        ("2024-03-11T19:30", "2024-03-11T20:00"),
        ("2024-03-11T17:30", "2024-03-11T20:00"),
        ("2024-03-11T04:00", "2024-03-11T20:00"),
    ),
    (
        "2024-11-01T20:00",
        ("2024-11-01T19:30", "2024-11-01T20:00"),
        ("2024-11-01T17:30", "2024-11-01T20:00"),
        ("2024-11-01T04:00", "2024-11-01T20:00"),
    ),
    (
        "2024-11-04T15:29:59.999999",
        ("2024-11-01T19:30", "2024-11-01T20:00"),
        ("2024-11-01T17:30", "2024-11-01T20:00"),
        ("2024-11-01T04:00", "2024-11-01T20:00"),
    ),
    (
        "2024-11-04T15:30",
        ("2024-11-04T14:30", "2024-11-04T15:30"),
        ("2024-11-01T17:30", "2024-11-01T20:00"),
        ("2024-11-01T04:00", "2024-11-01T20:00"),
    ),
    (
        "2024-01-02T14:30",
        ("2023-12-29T20:30", "2023-12-29T21:00"),
        ("2023-12-29T18:30", "2023-12-29T21:00"),
        ("2023-12-29T05:00", "2023-12-29T21:00"),
    ),
    (
        "2024-01-02T15:30",
        ("2024-01-02T14:30", "2024-01-02T15:30"),
        ("2023-12-29T18:30", "2023-12-29T21:00"),
        ("2023-12-29T05:00", "2023-12-29T21:00"),
    ),
]
GOLDEN_CASES = [
    pytest.param(timeframe, now, expected[0], expected[1], id=f"{timeframe}-{now}")
    for now, h1, h4, d1 in GOLDEN_TABLE
    for timeframe, expected in ((H1, h1), (H4, h4), (D1, d1))
]


@pytest.mark.parametrize(("timeframe", "now", "label", "close"), GOLDEN_CASES)
def test_the_last_kept_label_matches_the_golden_table(
    timeframe: Timeframe, now: str, label: str, close: str
) -> None:
    frame = golden_grid(timeframe)

    result = drop_open_candle(frame, timeframe, utc(now), calendar=NYSE)

    last = result.index[-1]
    assert last == utc(label)
    assert NYSE.candle_slot(timeframe, last).close_time == utc(close)
    assert len(result) == int(np.count_nonzero(frame.index <= utc(label)))
    if len(result) < len(frame):
        assert NYSE.candle_slot(timeframe, frame.index[len(result)]).close_time > utc(now)


def test_the_golden_grid_frames_span_the_spec_range() -> None:
    for timeframe in Timeframe:
        frame = golden_grid(timeframe)
        assert frame.index[0] >= utc("2023-12-27T00:00")
        assert frame.index[-1] < utc("2024-11-06T00:00")
    assert iso_labels(golden_grid(D1))[0] == "2023-12-27T05:00:00+00:00"
    assert iso_labels(golden_grid(D1))[-1] == "2024-11-05T05:00:00+00:00"


@pytest.mark.parametrize(
    ("timeframe", "labels", "now", "kind"),
    [
        pytest.param(
            H1,
            ["2024-07-02T13:30", "2024-07-02T14:00"],
            "2024-07-02T20:00",
            CandleLabelErrorKind.OFF_GRID,
            id="1h-10:00-et-off-grid",
        ),
        pytest.param(
            H1,
            ["2024-07-01T19:30", "2024-07-02T12:00"],
            "2024-07-02T20:00",
            CandleLabelErrorKind.OUTSIDE_SESSION,
            id="1h-08:00-et-outside-session",
        ),
        pytest.param(
            D1,
            ["2024-07-02T04:00", "2024-07-03T00:00"],
            "2024-07-03T17:00",
            CandleLabelErrorKind.OFF_GRID,
            id="1d-midnight-utc-off-grid",
        ),
        pytest.param(
            D1,
            ["2024-07-03T04:00", "2024-07-04T04:00"],
            "2024-07-05T21:00",
            CandleLabelErrorKind.NOT_A_SESSION,
            id="1d-holiday-not-a-session",
        ),
    ],
)
def test_a_rejected_last_returned_label_raises_the_calendar_error(
    timeframe: Timeframe, labels: list[str], now: str, kind: CandleLabelErrorKind
) -> None:
    frame = frame_of(labels)

    with pytest.raises(CandleLabelError) as caught:
        drop_open_candle(frame, timeframe, utc(now), calendar=NYSE)

    assert caught.value.kind is kind
    assert caught.value.label == utc(labels[-1])


def test_only_the_last_returned_label_is_checked() -> None:
    frame = frame_of(["2024-07-02T12:00", "2024-07-02T13:30"])

    result = drop_open_candle(frame, H1, utc("2024-07-02T20:00"), calendar=NYSE)

    assert iso_labels(result) == ["2024-07-02T12:00:00+00:00", "2024-07-02T13:30:00+00:00"]


def test_a_rejected_label_after_the_last_closed_slot_is_dropped_without_error() -> None:
    frame = pd.concat(
        [grid(H1, "2024-07-02T00:00", "2024-07-03T00:00"), frame_of(["2024-07-02T21:00"])]
    )
    assert len(frame) == 8

    result = drop_open_candle(frame, H1, utc("2024-07-02T20:00"), calendar=NYSE)

    assert len(result) == 7
    assert iso_labels(result)[-1] == "2024-07-02T19:30:00+00:00"


def test_a_past_now_drops_every_later_row_not_only_the_in_progress_candle() -> None:
    frame = grid(D1, "2024-06-24T00:00", "2024-07-10T00:00")
    assert len(frame) == 11

    result = drop_open_candle(frame, D1, utc("2024-06-28T20:00"), calendar=NYSE)

    assert len(result) == 5
    assert iso_labels(result)[-1] == "2024-06-28T04:00:00+00:00"


def test_rows_after_the_last_closed_slot_are_dropped_whatever_their_label() -> None:
    frame = frame_of(
        ["2024-07-02T19:30", "2024-07-02T19:45", "2024-07-02T20:00", "2024-07-04T14:30"]
    )

    result = drop_open_candle(frame, H1, utc("2024-07-02T20:00"), calendar=NYSE)

    assert iso_labels(result) == ["2024-07-02T19:30:00+00:00"]


def test_a_last_returned_label_outside_the_coverage_raises_calendar_range_error() -> None:
    frame = frame_of(["2020-06-01T13:30"])

    with pytest.raises(CalendarRangeError):
        drop_open_candle(frame, H1, utc("2024-07-02T20:00"), calendar=NYSE)


def test_a_last_returned_sub_microsecond_label_raises_value_error() -> None:
    frame = frame_of(["2024-07-02T13:30:00.000000001"], unit="ns")

    with pytest.raises(ValueError, match="sub-microsecond"):
        drop_open_candle(frame, H1, utc("2024-07-02T20:00"), calendar=NYSE)
