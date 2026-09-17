"""Tests of ``resample_hourly_to_4h`` (spec 011, T1-T2, AC5-AC6; decision D57).

T1 pins the contract of Design 5.1 and 5.2: the argument checks in order, the canonical frame,
the per-slot report, the empty result, label checks on considered rows only, and an input that
is never modified. T2 replays the hand-computed case of Design 5.3 literally, for every ``now``
of its table. Every expectation is a literal from the spec; nothing is recomputed with the code
under test.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

import trading_bot.domain.candle_resampling as resampling_module
from tests.fixtures.calendars import NEW_YORK, nyse_test_calendar, utc
from tests.fixtures.session_candles import session_candles
from tests.fixtures.yahoo_recordings import hand_frame
from trading_bot.domain.candle_resampling import (
    ResampledCandles,
    ResampledSlot,
    resample_hourly_to_4h,
)
from trading_bot.domain.candles import OHLCV_COLUMNS, CandleValidationError, validate_candles
from trading_bot.domain.market_calendar.sessions import (
    CalendarRangeError,
    CandleLabelError,
    CandleLabelErrorKind,
)
from trading_bot.domain.timeframe import Timeframe

NYSE = nyse_test_calendar()
H1 = Timeframe.H1
H4 = Timeframe.H4
CANONICAL_INDEX = pd.DatetimeTZDtype(unit="us", tz="UTC")

type Row = tuple[str, float, float, float, float, float]

# Design 5.3: the 4h rows of the hand frame, computed by hand.
HAND_ROWS: tuple[Row, ...] = (
    ("2024-11-27T14:30:00+00:00", 100.0, 105.0, 98.0, 99.0, 4500.0),
    ("2024-11-27T18:30:00+00:00", 99.0, 101.0, 96.0, 100.0, 4700.0),
    ("2024-11-29T14:30:00+00:00", 100.0, 106.0, 100.0, 104.0, 1800.0),
    ("2024-12-02T14:30:00+00:00", 104.0, 111.0, 103.0, 106.0, 4200.0),
    ("2024-12-02T18:30:00+00:00", 106.0, 107.0, 102.0, 103.0, 1350.0),
)
# (label, bars, missing, closing_bar_missing)
HAND_REPORT: tuple[tuple[str, int, tuple[str, ...], bool], ...] = (
    ("2024-11-27T14:30", 3, ("2024-11-27T16:30",), False),
    ("2024-11-27T18:30", 3, (), False),
    ("2024-11-29T14:30", 3, ("2024-11-29T17:30",), True),
    ("2024-12-02T14:30", 4, (), False),
    ("2024-12-02T18:30", 2, ("2024-12-02T20:30",), True),
)
HAND_NOW_TABLE: tuple[tuple[str, int], ...] = (
    ("2024-11-27T18:29:59.999999", 0),
    ("2024-11-27T18:30:00", 1),
    ("2024-11-27T21:00:30", 2),
    ("2024-11-29T18:00:30", 3),
    ("2024-12-02T18:30:30", 4),
    ("2024-12-02T21:00:30", 5),
    ("2024-12-03T18:30:30", 5),
)


def rows_of(frame: pd.DataFrame) -> list[Row]:
    return [
        (
            pd.Timestamp(label).isoformat(),
            float(values[0]),
            float(values[1]),
            float(values[2]),
            float(values[3]),
            float(values[4]),
        )
        for label, values in zip(frame.index, frame.to_numpy(dtype=np.float64), strict=True)
    ]


def canonical(rows: list[Row]) -> pd.DataFrame:
    """The canonical frame of ``rows`` (ISO labels with an offset)."""
    index = pd.DatetimeIndex([pd.Timestamp(row[0]) for row in rows], dtype=CANONICAL_INDEX)
    values = np.array([row[1:] for row in rows], dtype=np.float64).reshape(len(rows), 5)
    return pd.DataFrame(values, index=index, columns=list(OHLCV_COLUMNS), dtype=np.float64)


def report_of(result: ResampledCandles) -> list[tuple[str, int, tuple[str, ...], bool]]:
    return [
        (
            entry.slot.label.replace(tzinfo=None).isoformat(timespec="minutes"),
            entry.bars,
            tuple(
                label.replace(tzinfo=None).isoformat(timespec="minutes") for label in entry.missing
            ),
            entry.closing_bar_missing,
        )
        for entry in result.slots
    ]


# --- T1: module surface --------------------------------------------------------------------------


def test_the_module_exports_exactly_the_spec_names() -> None:
    assert resampling_module.__all__ == [
        "ResampledCandles",
        "ResampledSlot",
        "resample_hourly_to_4h",
    ]


def test_resampled_slot_is_frozen_keyword_only_equal_by_value_and_hashable() -> None:
    slot = NYSE.candle_slot(H4, utc("2024-11-27T14:30"))
    first = ResampledSlot(
        slot=slot, bars=3, missing=(utc("2024-11-27T16:30"),), closing_bar_missing=False
    )
    second = ResampledSlot(
        slot=slot, bars=3, missing=(utc("2024-11-27T16:30"),), closing_bar_missing=False
    )

    assert first == second
    assert hash(first) == hash(second)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.bars = 4  # type: ignore[misc]
    with pytest.raises(TypeError):
        ResampledSlot(slot, 3, (), False)  # type: ignore[misc]


def test_resampled_candles_compares_by_identity() -> None:
    first = resample_hourly_to_4h(hand_frame(), utc("2024-12-02T21:00:30"), calendar=NYSE)
    second = resample_hourly_to_4h(hand_frame(), utc("2024-12-02T21:00:30"), calendar=NYSE)

    assert first != second
    assert first == first
    assert first.slots == second.slots
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.slots = ()  # type: ignore[misc]


# --- T1: argument checks, in order --------------------------------------------------------------


def test_a_non_calendar_raises_type_error_before_any_other_check() -> None:
    with pytest.raises(TypeError, match="calendar"):
        resample_hourly_to_4h("not a frame", "not an instant", calendar="NYSE")  # type: ignore[arg-type]


def test_now_is_checked_before_the_frame() -> None:
    with pytest.raises(TypeError, match="datetime"):
        resample_hourly_to_4h("not a frame", "2024-11-27", calendar=NYSE)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="naive"):
        resample_hourly_to_4h("not a frame", datetime(2024, 11, 27, 18, 30), calendar=NYSE)  # type: ignore[arg-type]


def test_the_frame_is_validated_before_the_calendar_is_queried() -> None:
    with pytest.raises(TypeError, match="DataFrame"):
        resample_hourly_to_4h("not a frame", utc("2030-01-01T00:00"), calendar=NYSE)  # type: ignore[arg-type]
    broken = hand_frame()
    broken.iloc[0, 1] = 1.0  # high below low
    with pytest.raises(CandleValidationError):
        resample_hourly_to_4h(broken, utc("2030-01-01T00:00"), calendar=NYSE)


def test_now_outside_the_calendar_raises_calendar_range_error() -> None:
    with pytest.raises(CalendarRangeError):
        resample_hourly_to_4h(hand_frame(), utc("2030-01-01T00:00"), calendar=NYSE)


def test_now_before_the_first_closed_4h_slot_raises_calendar_range_error() -> None:
    with pytest.raises(CalendarRangeError):
        resample_hourly_to_4h(hand_frame(), utc("2021-01-04T14:00"), calendar=NYSE)


def test_the_calendar_is_queried_even_for_an_empty_frame() -> None:
    empty = hand_frame().iloc[:0]

    with pytest.raises(CalendarRangeError):
        resample_hourly_to_4h(empty, utc("2030-01-01T00:00"), calendar=NYSE)


# --- T1: label checks on considered rows ------------------------------------------------------


def with_extra_row(frame: pd.DataFrame, label: pd.Timestamp) -> pd.DataFrame:
    extra = pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.0, 10.0]],
        index=pd.DatetimeIndex([label]).as_unit(pd.DatetimeIndex(frame.index).unit),
        columns=list(OHLCV_COLUMNS),
        dtype=np.float64,
    )
    return pd.concat([frame, extra]).sort_index()


def test_an_off_grid_considered_label_raises_candle_label_error() -> None:
    frame = with_extra_row(hand_frame(), pd.Timestamp("2024-11-27T15:00", tz="UTC"))

    with pytest.raises(CandleLabelError) as caught:
        resample_hourly_to_4h(frame, utc("2024-12-02T21:00:30"), calendar=NYSE)

    assert caught.value.kind is CandleLabelErrorKind.OFF_GRID
    assert caught.value.timeframe is H1
    assert caught.value.label == utc("2024-11-27T15:00")


def test_a_considered_label_outside_the_session_raises_with_its_kind() -> None:
    frame = with_extra_row(hand_frame(), pd.Timestamp("2024-11-28T15:30", tz="UTC"))

    with pytest.raises(CandleLabelError) as caught:
        resample_hourly_to_4h(frame, utc("2024-12-02T21:00:30"), calendar=NYSE)

    assert caught.value.kind is CandleLabelErrorKind.NOT_A_SESSION


def test_the_first_offending_considered_label_is_reported() -> None:
    frame = with_extra_row(hand_frame(), pd.Timestamp("2024-11-27T21:30", tz="UTC"))
    frame = with_extra_row(frame, pd.Timestamp("2024-11-29T15:00", tz="UTC"))

    with pytest.raises(CandleLabelError) as caught:
        resample_hourly_to_4h(frame, utc("2024-12-02T21:00:30"), calendar=NYSE)

    assert caught.value.kind is CandleLabelErrorKind.OUTSIDE_SESSION
    assert caught.value.label == utc("2024-11-27T21:30")


def test_an_invalid_first_considered_label_is_reported() -> None:
    frame = with_extra_row(hand_frame(), pd.Timestamp("2024-11-27T13:00", tz="UTC"))

    with pytest.raises(CandleLabelError) as caught:
        resample_hourly_to_4h(frame, utc("2024-12-02T21:00:30"), calendar=NYSE)

    assert caught.value.kind is CandleLabelErrorKind.OUTSIDE_SESSION
    assert caught.value.label == utc("2024-11-27T13:00")


def test_a_considered_label_before_the_calendar_raises_calendar_range_error() -> None:
    frame = with_extra_row(hand_frame(), pd.Timestamp("2020-12-31T15:30", tz="UTC"))

    with pytest.raises(CalendarRangeError):
        resample_hourly_to_4h(frame, utc("2024-12-02T21:00:30"), calendar=NYSE)


def test_a_considered_label_with_sub_microsecond_precision_raises_value_error() -> None:
    frame = hand_frame()
    frame.index = pd.DatetimeIndex(frame.index).as_unit("ns")
    frame = with_extra_row(frame, pd.Timestamp("2024-11-27T20:30:00.000000001", tz="UTC"))

    with pytest.raises(ValueError, match="sub-microsecond"):
        resample_hourly_to_4h(frame, utc("2024-12-02T21:00:30"), calendar=NYSE)


def test_labels_at_or_after_the_last_close_are_ignored_without_checks() -> None:
    frame = with_extra_row(hand_frame(), pd.Timestamp("2024-12-02T21:00", tz="UTC"))
    frame = with_extra_row(frame, pd.Timestamp("2024-12-02T22:17", tz="UTC"))

    result = resample_hourly_to_4h(frame, utc("2024-12-02T21:00:30"), calendar=NYSE)

    assert rows_of(result.candles) == list(HAND_ROWS)


def test_an_in_progress_row_of_the_next_slot_is_ignored() -> None:
    frame = with_extra_row(hand_frame(), pd.Timestamp("2024-12-03T14:30", tz="UTC"))

    result = resample_hourly_to_4h(frame, utc("2024-12-03T15:00"), calendar=NYSE)

    assert rows_of(result.candles) == list(HAND_ROWS)


# --- T1: the result ---------------------------------------------------------------------------


def test_the_result_is_a_canonical_frame_that_passes_validation() -> None:
    result = resample_hourly_to_4h(hand_frame(), utc("2024-12-02T21:00:30"), calendar=NYSE)

    frame = result.candles
    assert validate_candles(frame) is frame
    assert frame.index.dtype == CANONICAL_INDEX
    assert frame.index.name is None
    assert list(frame.columns) == list(OHLCV_COLUMNS)
    assert [str(dtype) for dtype in frame.dtypes] == ["float64"] * 5


def test_slots_are_aligned_with_the_rows() -> None:
    result = resample_hourly_to_4h(hand_frame(), utc("2024-12-02T21:00:30"), calendar=NYSE)

    assert len(result.slots) == len(result.candles)
    for entry, label in zip(result.slots, result.candles.index, strict=True):
        assert entry.slot.timeframe is H4
        assert entry.slot.label == label
        assert entry.slot == NYSE.candle_slot(H4, label)
        assert entry.bars >= 1
        assert all(missing.tzinfo is UTC for missing in entry.missing)
        assert list(entry.missing) == sorted(entry.missing)


def test_the_input_frame_is_never_modified() -> None:
    frame = hand_frame()
    frame.index = pd.DatetimeIndex(frame.index).tz_convert(UTC).as_unit("s").rename("Datetime")
    pristine = frame.copy(deep=True)

    resample_hourly_to_4h(frame, utc("2024-12-02T21:00:30"), calendar=NYSE)

    pd.testing.assert_frame_equal(frame, pristine, check_exact=True)
    assert frame.index.dtype == pristine.index.dtype
    assert frame.index.name == "Datetime"


def test_any_input_unit_and_utc_zone_give_the_same_canonical_result() -> None:
    frame = hand_frame()
    frame.index = pd.DatetimeIndex(frame.index).tz_convert("UTC").as_unit("s").rename("Datetime")

    result = resample_hourly_to_4h(frame, utc("2024-12-02T21:00:30"), calendar=NYSE)

    assert result.candles.index.dtype == CANONICAL_INDEX
    assert result.candles.index.name is None
    assert rows_of(result.candles) == list(HAND_ROWS)


def test_now_in_another_zone_names_the_same_instant() -> None:
    now = datetime(2024, 12, 2, 16, 0, 30, tzinfo=NEW_YORK)

    result = resample_hourly_to_4h(hand_frame(), now, calendar=NYSE)

    assert rows_of(result.candles) == list(HAND_ROWS)


def test_an_empty_frame_gives_an_empty_canonical_result() -> None:
    result = resample_hourly_to_4h(hand_frame().iloc[:0], utc("2024-12-02T21:00:30"), calendar=NYSE)

    assert result.slots == ()
    assert len(result.candles) == 0
    assert result.candles.index.dtype == CANONICAL_INDEX
    assert list(result.candles.columns) == list(OHLCV_COLUMNS)
    assert validate_candles(result.candles) is result.candles


def test_a_full_regular_session_gives_two_complete_candles() -> None:
    frame = session_candles(NYSE, H1, utc("2024-01-02T00:00"), utc("2024-01-03T00:00"))

    result = resample_hourly_to_4h(frame, utc("2024-01-02T21:00"), calendar=NYSE)

    assert [pd.Timestamp(label).isoformat() for label in result.candles.index] == [
        "2024-01-02T14:30:00+00:00",
        "2024-01-02T18:30:00+00:00",
    ]
    assert [(entry.bars, entry.missing, entry.closing_bar_missing) for entry in result.slots] == [
        (4, (), False),
        (3, (), False),
    ]
    values = frame.to_numpy(dtype=np.float64)
    first = result.candles.iloc[0].to_numpy(dtype=np.float64)
    assert first.tolist() == [
        values[0, 0],
        values[:4, 1].max(),
        values[:4, 2].min(),
        values[3, 3],
        values[0, 4] + values[1, 4] + values[2, 4] + values[3, 4],
    ]


def test_a_slot_whose_first_hours_are_missing_reports_them() -> None:
    frame = hand_frame().iloc[[0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 13, 14]]

    result = resample_hourly_to_4h(frame, utc("2024-12-02T21:00:30"), calendar=NYSE)

    assert report_of(result)[3] == (
        "2024-12-02T14:30",
        1,
        ("2024-12-02T14:30", "2024-12-02T15:30", "2024-12-02T16:30"),
        False,
    )
    assert rows_of(result.candles)[3] == (
        "2024-12-02T14:30:00+00:00",
        109.0,
        111.0,
        105.0,
        106.0,
        900.0,
    )


def test_a_frame_starting_mid_slot_reports_the_hours_before_its_first_row() -> None:
    frame = hand_frame().iloc[1:]

    result = resample_hourly_to_4h(frame, utc("2024-11-27T18:30"), calendar=NYSE)

    assert report_of(result) == [
        ("2024-11-27T14:30", 2, ("2024-11-27T14:30", "2024-11-27T16:30"), False)
    ]
    assert rows_of(result.candles) == [
        ("2024-11-27T14:30:00+00:00", 101.0, 105.0, 98.0, 99.0, 3500.0)
    ]


def test_a_volume_sum_that_overflows_raises_the_postcondition_error() -> None:
    frame = hand_frame().iloc[:2].copy()
    frame["volume"] = [1e308, 1e308]

    with pytest.raises(CandleValidationError):
        resample_hourly_to_4h(frame, utc("2024-11-27T18:30"), calendar=NYSE)


# --- T2: the hand-computed case (Design 5.3) -----------------------------------------------------


def test_the_hand_frame_has_the_fifteen_rows_of_the_spec() -> None:
    frame = hand_frame()

    assert validate_candles(frame) is frame
    assert frame.index.dtype == CANONICAL_INDEX
    assert len(frame) == 15
    assert rows_of(frame)[0] == ("2024-11-27T14:30:00+00:00", 100.0, 102.0, 99.0, 101.0, 1000.0)
    assert rows_of(frame)[-1] == ("2024-12-02T19:30:00+00:00", 105.0, 106.0, 102.0, 103.0, 650.0)


@pytest.mark.parametrize(("now", "count"), HAND_NOW_TABLE)
def test_the_hand_frame_gives_the_spec_table_for_every_now(now: str, count: int) -> None:
    result = resample_hourly_to_4h(hand_frame(), utc(now), calendar=NYSE)

    assert rows_of(result.candles) == list(HAND_ROWS[:count])
    assert report_of(result) == list(HAND_REPORT[:count])
    pd.testing.assert_frame_equal(
        result.candles, canonical(list(HAND_ROWS[:count])), check_exact=True
    )
