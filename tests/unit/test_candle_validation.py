"""Tests of the candle frame contract and ``validate_candles`` (spec 004, AC5-AC8)."""

from __future__ import annotations

import copy
import pickle
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import UTC, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.candles import (
    ALL_SCENARIOS,
    TIMEFRAMES,
    Scenario,
    TimeframeCode,
    synthetic_candles,
)
from trading_bot.domain.candles import (
    OHLCV_COLUMNS,
    PRICE_COLUMNS,
    CandleColumnsError,
    CandleErrorKind,
    CandleIndexError,
    CandleValidationError,
    CandleValuesError,
    validate_candles,
)

DEFECT_ROWS = (3, 7)


def base_frame() -> pd.DataFrame:
    return synthetic_candles(20, seed=1)


def assert_accepted(frame: pd.DataFrame) -> None:
    """``validate_candles`` returns the very same frame and leaves it untouched (AC5)."""
    before = frame.copy(deep=True)

    result = validate_candles(frame)

    assert result is frame
    pd.testing.assert_frame_equal(frame, before, check_exact=True)
    assert frame.index.dtype == before.index.dtype


# --- T3: valid frames are accepted (AC5) --------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 60, 250])
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_every_synthetic_scenario_is_accepted(
    scenario: Scenario, timeframe: TimeframeCode, n: int
) -> None:
    assert_accepted(synthetic_candles(n, seed=5, scenario=scenario, timeframe=timeframe))


def test_empty_frame_is_accepted() -> None:
    frame = pd.DataFrame(
        {column: pd.Series([], dtype=np.float64) for column in OHLCV_COLUMNS},
        index=pd.DatetimeIndex([], tz="UTC"),
    )

    assert_accepted(frame)


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_any_index_unit_is_accepted(unit: str) -> None:
    frame = base_frame()
    frame = frame.set_axis(frame.index.as_unit(unit))
    assert frame.index.unit == unit

    assert_accepted(frame)


@pytest.mark.parametrize(
    "zone",
    [
        pytest.param("UTC", id="string"),
        pytest.param(UTC, id="datetime-utc"),
        pytest.param(ZoneInfo("UTC"), id="zoneinfo-utc"),
    ],
)
def test_every_utc_spelling_is_accepted(zone: object) -> None:
    frame = base_frame()
    frame = frame.set_axis(frame.index.tz_localize(None).tz_localize(zone))

    assert_accepted(frame)


@pytest.mark.parametrize("name", [None, "Date", "Datetime"])
def test_any_index_name_is_accepted(name: str | None) -> None:
    assert_accepted(base_frame().rename_axis(name))


def test_labels_off_the_utc_hour_grid_are_accepted() -> None:
    frame = synthetic_candles(24, seed=2, timeframe="1h")
    frame = frame.set_axis(frame.index + pd.Timedelta(minutes=30))
    assert frame.index[13].isoformat() == "2024-01-01T13:30:00+00:00"

    assert_accepted(frame)


def test_irregular_spacing_is_accepted() -> None:
    index = pd.DatetimeIndex(
        [
            "2024-01-05T14:30:00+00:00",  # Friday
            "2024-01-05T15:30:00+00:00",
            "2024-01-08T14:30:00+00:00",  # Monday, after a night and a weekend
            "2024-01-10T20:30:00+00:00",  # after a holiday-like gap
        ]
    )
    frame = synthetic_candles(4, seed=3).set_axis(index)

    assert_accepted(frame)


def test_zero_volume_and_flat_candles_are_accepted() -> None:
    frame = pd.DataFrame(
        {"open": 5.0, "high": 5.0, "low": 5.0, "close": 5.0, "volume": 0.0},
        index=pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC"),
    )

    assert_accepted(frame)


# --- Frame builders for the rejection table ---------------------------------------------


def with_labels(changes: dict[int, Hashable]) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def build(frame: pd.DataFrame) -> pd.DataFrame:
        labels = frame.index.tolist()
        for position, label in changes.items():
            labels[position] = label
        return frame.set_axis(pd.DatetimeIndex(labels))

    return build


def with_nat(frame: pd.DataFrame) -> pd.DataFrame:
    keep = ~np.isin(np.arange(len(frame)), DEFECT_ROWS)
    return frame.set_axis(frame.index.where(keep))


def swapped_labels(first: int, second: int) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def build(frame: pd.DataFrame) -> pd.DataFrame:
        return with_labels({first: frame.index[second], second: frame.index[first]})(frame)

    return build


def repeated_label(position: int, source: int) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def build(frame: pd.DataFrame) -> pd.DataFrame:
        return with_labels({position: frame.index[source]})(frame)

    return build


def with_index(
    make: Callable[[pd.DatetimeIndex], pd.Index],
) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def build(frame: pd.DataFrame) -> pd.DataFrame:
        index = frame.index
        assert isinstance(index, pd.DatetimeIndex)
        return frame.set_axis(make(index))

    return build


def with_columns(labels: pd.Index | list[str]) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def build(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.set_axis(labels, axis=1)

    return build


def with_cells(
    columns: tuple[str, ...], value: float, rows: tuple[int, ...] = DEFECT_ROWS
) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def build(frame: pd.DataFrame) -> pd.DataFrame:
        result = frame.copy()
        for column in columns:
            result.iloc[list(rows), result.columns.get_loc(column)] = value
        return result

    return build


def swapped_high_low(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    rows = list(DEFECT_ROWS)
    high = result["high"].to_numpy(copy=True)
    low = result["low"].to_numpy(copy=True)
    result.iloc[rows, result.columns.get_loc("high")] = low[rows]
    result.iloc[rows, result.columns.get_loc("low")] = high[rows]
    return result


def body_outside(
    open_row: int | None, close_row: int | None
) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def build(frame: pd.DataFrame) -> pd.DataFrame:
        result = frame.copy()
        if open_row is not None:
            high = float(result["high"].iloc[open_row])
            result.iloc[open_row, result.columns.get_loc("open")] = high * 1.01
        if close_row is not None:
            low = float(result["low"].iloc[close_row])
            result.iloc[close_row, result.columns.get_loc("close")] = low * 0.99
        return result

    return build


def close_above_high(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for row in DEFECT_ROWS:
        high = float(result["high"].iloc[row])
        result.iloc[row, result.columns.get_loc("close")] = high * 1.01
    return result


@dataclass(frozen=True)
class Expected:
    error: type[CandleValidationError]
    kind: CandleErrorKind
    column: str | None = None
    position: int | None = None
    count: int = 0
    has_timestamp: bool = False
    in_message: tuple[str, ...] = ()


def index_error(kind: CandleErrorKind, *in_message: str) -> Expected:
    return Expected(CandleIndexError, kind, in_message=in_message)


def row_error(
    error: type[CandleValidationError],
    kind: CandleErrorKind,
    column: str | None = None,
    position: int = DEFECT_ROWS[0],
    count: int = len(DEFECT_ROWS),
    *,
    has_timestamp: bool = True,
) -> Expected:
    return Expected(error, kind, column, position, count, has_timestamp)


K = CandleErrorKind
MULTI_INDEX = pd.MultiIndex.from_tuples([(column, "AAPL") for column in OHLCV_COLUMNS])

REJECTED = [
    # 1. index_type
    pytest.param(lambda f: f.reset_index(drop=True), index_error(K.INDEX_TYPE), id="range-index"),
    pytest.param(
        with_index(lambda i: pd.Index([label.isoformat() for label in i])),
        index_error(K.INDEX_TYPE),
        id="iso-string-index",
    ),
    pytest.param(
        with_index(lambda i: i.tz_localize(None).to_period("D")),
        index_error(K.INDEX_TYPE, "PeriodIndex"),
        id="period-index",
    ),
    # 2. timezone
    pytest.param(
        with_index(lambda i: i.tz_localize(None)), index_error(K.TIMEZONE, "naive"), id="naive"
    ),
    pytest.param(
        with_index(lambda i: i.tz_convert("America/New_York")),
        index_error(K.TIMEZONE, "America/New_York"),
        id="new-york",
    ),
    pytest.param(
        with_index(lambda i: i.tz_convert("Etc/UTC")),
        index_error(K.TIMEZONE, "Etc/UTC"),
        id="etc-utc",
    ),
    pytest.param(
        with_index(lambda i: i.tz_convert("Europe/London")),
        index_error(K.TIMEZONE, "Europe/London"),
        id="europe-london",
    ),
    pytest.param(
        with_index(lambda i: i.tz_convert(timezone(timedelta(hours=1)))),
        index_error(K.TIMEZONE, "UTC+01:00"),
        id="fixed-plus-one",
    ),
    # 3. missing_timestamp
    pytest.param(
        with_nat,
        row_error(CandleIndexError, K.MISSING_TIMESTAMP, has_timestamp=False),
        id="nat",
    ),
    # 4. duplicate_timestamp
    pytest.param(
        repeated_label(4, 3),
        row_error(CandleIndexError, K.DUPLICATE_TIMESTAMP, position=4, count=1),
        id="adjacent-duplicate",
    ),
    pytest.param(
        repeated_label(7, 3),
        row_error(CandleIndexError, K.DUPLICATE_TIMESTAMP, position=7, count=1),
        id="non-adjacent-duplicate",
    ),
    # 5. unsorted_index
    pytest.param(
        swapped_labels(5, 6),
        row_error(CandleIndexError, K.UNSORTED_INDEX, position=6, count=1),
        id="swapped-labels",
    ),
    # 6. columns
    pytest.param(
        lambda f: f.drop(columns="volume"),
        Expected(CandleColumnsError, K.COLUMNS, in_message=("volume",)),
        id="missing-volume",
    ),
    pytest.param(
        lambda f: f.assign(adj_close=f["close"]),
        Expected(CandleColumnsError, K.COLUMNS, in_message=("adj_close",)),
        id="extra-adj-close",
    ),
    pytest.param(
        lambda f: f[["close", "open", "high", "low", "volume"]],
        Expected(CandleColumnsError, K.COLUMNS),
        id="reordered",
    ),
    pytest.param(
        lambda f: f.rename(columns={"open": "Open"}),
        Expected(CandleColumnsError, K.COLUMNS, in_message=("'Open'",)),
        id="capitalized",
    ),
    pytest.param(
        with_columns(["open", "high", "low", "close", "close"]),
        Expected(CandleColumnsError, K.COLUMNS),
        id="duplicated-close",
    ),
    pytest.param(
        with_columns(MULTI_INDEX),
        Expected(CandleColumnsError, K.COLUMNS, in_message=("MultiIndex",)),
        id="multi-index",
    ),
    pytest.param(lambda f: f.iloc[:, :0], Expected(CandleColumnsError, K.COLUMNS), id="no-columns"),
    # 7. dtype
    pytest.param(
        lambda f: f.astype({"volume": "int64"}),
        Expected(CandleColumnsError, K.DTYPE, "volume", in_message=("int64",)),
        id="int-volume",
    ),
    pytest.param(
        lambda f: f.astype({"close": "float32"}),
        Expected(CandleColumnsError, K.DTYPE, "close", in_message=("float32",)),
        id="float32-close",
    ),
    pytest.param(
        lambda f: f.astype({"open": object}),
        Expected(CandleColumnsError, K.DTYPE, "open", in_message=("object",)),
        id="object-open",
    ),
    pytest.param(
        lambda f: f.astype({"high": "Float64"}),
        Expected(CandleColumnsError, K.DTYPE, "high", in_message=("Float64",)),
        id="nullable-high",
    ),
    # 8. missing_value
    pytest.param(
        with_cells(("close",), np.nan),
        row_error(CandleValuesError, K.MISSING_VALUE, "close"),
        id="nan-close",
    ),
    pytest.param(
        with_cells(("volume",), np.nan),
        row_error(CandleValuesError, K.MISSING_VALUE, "volume"),
        id="nan-volume",
    ),
    pytest.param(
        with_cells(("low", "open"), np.nan),
        row_error(CandleValuesError, K.MISSING_VALUE, "open"),
        id="nan-low-and-open",
    ),
    # 9. infinite_value
    pytest.param(
        with_cells(("high",), np.inf),
        row_error(CandleValuesError, K.INFINITE_VALUE, "high"),
        id="inf-high",
    ),
    pytest.param(
        with_cells(("low",), -np.inf),
        row_error(CandleValuesError, K.INFINITE_VALUE, "low"),
        id="minus-inf-low",
    ),
    pytest.param(
        with_cells(("volume",), np.inf),
        row_error(CandleValuesError, K.INFINITE_VALUE, "volume"),
        id="inf-volume",
    ),
    # 10. non_positive_price
    pytest.param(
        with_cells(("open",), 0.0),
        row_error(CandleValuesError, K.NON_POSITIVE_PRICE, "open"),
        id="zero-open",
    ),
    pytest.param(
        with_cells(("close",), -1.0),
        row_error(CandleValuesError, K.NON_POSITIVE_PRICE, "close"),
        id="negative-close",
    ),
    pytest.param(
        with_cells(("low",), -0.0),
        row_error(CandleValuesError, K.NON_POSITIVE_PRICE, "low"),
        id="negative-zero-low",
    ),
    # 11. negative_volume
    pytest.param(
        with_cells(("volume",), -1.0),
        row_error(CandleValuesError, K.NEGATIVE_VOLUME, "volume"),
        id="negative-volume",
    ),
    # 12. high_below_low
    pytest.param(
        swapped_high_low, row_error(CandleValuesError, K.HIGH_BELOW_LOW), id="high-low-swapped"
    ),
    # 13. body_outside_range
    pytest.param(
        body_outside(open_row=3, close_row=7),
        row_error(CandleValuesError, K.BODY_OUTSIDE_RANGE, "open"),
        id="open-above-high-and-close-below-low",
    ),
    pytest.param(
        body_outside(open_row=None, close_row=3),
        row_error(CandleValuesError, K.BODY_OUTSIDE_RANGE, "close", count=1),
        id="close-below-low",
    ),
    pytest.param(
        close_above_high,
        row_error(CandleValuesError, K.BODY_OUTSIDE_RANGE, "close"),
        id="close-above-high",
    ),
]


# --- T5: every row of the Design 4 table (AC6) --------------------------------------------


@pytest.mark.parametrize(("build", "expected"), REJECTED)
def test_invalid_frames_are_rejected(
    build: Callable[[pd.DataFrame], pd.DataFrame], expected: Expected
) -> None:
    frame = build(base_frame())

    with pytest.raises(CandleValidationError) as caught:
        validate_candles(frame)

    error = caught.value
    assert type(error) is expected.error
    assert error.kind is expected.kind
    assert error.column == expected.column
    assert error.position == expected.position
    assert error.count == expected.count
    if expected.has_timestamp:
        assert expected.position is not None
        assert error.timestamp == frame.index[expected.position]
        assert isinstance(error.timestamp, pd.Timestamp)
    else:
        assert error.timestamp is None
    for fragment in expected.in_message:
        assert fragment in str(error)


# --- T6: error contract (AC7) -------------------------------------------------------------


def test_error_hierarchy() -> None:
    assert issubclass(CandleValidationError, ValueError)
    for subclass in (CandleIndexError, CandleColumnsError, CandleValuesError):
        assert issubclass(subclass, CandleValidationError)


@pytest.mark.parametrize(("build", "expected"), REJECTED)
def test_message_names_the_kind_and_the_located_fields(
    build: Callable[[pd.DataFrame], pd.DataFrame], expected: Expected
) -> None:
    frame = build(base_frame())

    with pytest.raises(CandleValidationError) as caught:
        validate_candles(frame)

    error = caught.value
    message = str(error)
    assert "\n" not in message
    assert len(message) < 400
    assert expected.kind.value in message
    if error.column is not None:
        assert f"column={error.column!r}" in message
    if error.timestamp is not None:
        assert f"timestamp={error.timestamp.isoformat()}" in message
    if error.position is not None:
        assert f"position={error.position}" in message
        assert f"count={error.count}" in message
    else:
        assert "count=" not in message


def test_directly_built_error_keeps_its_fields() -> None:
    stamp = pd.Timestamp("2024-01-04T00:00:00+00:00")

    error = CandleValuesError(
        CandleErrorKind.MISSING_VALUE,
        "candle values must not be NaN",
        column="close",
        timestamp=stamp,
        position=3,
        count=2,
    )

    assert error.kind is CandleErrorKind.MISSING_VALUE
    assert (error.column, error.timestamp, error.position, error.count) == ("close", stamp, 3, 2)
    assert str(error) == (
        "missing_value: candle values must not be NaN "
        "[column='close', timestamp=2024-01-04T00:00:00+00:00, position=3, count=2]"
    )


def test_frame_level_error_message_has_no_location() -> None:
    error = CandleColumnsError(CandleErrorKind.COLUMNS, "bad columns")

    assert (error.column, error.timestamp, error.position, error.count) == (None, None, None, 0)
    assert str(error) == "columns: bad columns"


@pytest.mark.parametrize(
    "error",
    [
        CandleIndexError(CandleErrorKind.TIMEZONE, "not UTC"),
        CandleValuesError(
            CandleErrorKind.NEGATIVE_VOLUME,
            "negative volume",
            column="volume",
            timestamp=pd.Timestamp("2024-01-04T00:00:00+00:00"),
            position=3,
            count=2,
        ),
    ],
)
def test_errors_survive_pickle_and_copy(error: CandleValidationError) -> None:
    for restored in (pickle.loads(pickle.dumps(error)), copy.deepcopy(error)):  # noqa: S301
        assert type(restored) is type(error)
        assert str(restored) == str(error)
        assert (restored.kind, restored.column, restored.timestamp) == (
            error.kind,
            error.column,
            error.timestamp,
        )
        assert (restored.position, restored.count) == (error.position, error.count)


def test_first_failing_check_wins_naive_index_over_nan_values() -> None:
    frame = with_cells(("close",), np.nan)(base_frame())
    frame = frame.set_axis(frame.index.tz_localize(None))

    with pytest.raises(CandleIndexError) as caught:
        validate_candles(frame)

    assert caught.value.kind is CandleErrorKind.TIMEZONE


def test_first_failing_check_wins_nan_over_high_below_low() -> None:
    frame = base_frame()
    high = float(frame["high"].iloc[2])
    low = float(frame["low"].iloc[2])
    frame.iloc[2, frame.columns.get_loc("high")] = low
    frame.iloc[2, frame.columns.get_loc("low")] = high
    frame.iloc[8, frame.columns.get_loc("close")] = np.nan

    with pytest.raises(CandleValuesError) as caught:
        validate_candles(frame)

    assert caught.value.kind is CandleErrorKind.MISSING_VALUE
    assert caught.value.position == 8
    assert caught.value.count == 1


def test_first_failing_check_wins_columns_over_dtype() -> None:
    frame = base_frame().astype({"volume": "int64"}).rename(columns={"open": "Open"})

    with pytest.raises(CandleColumnsError) as caught:
        validate_candles(frame)

    assert caught.value.kind is CandleErrorKind.COLUMNS


def test_earliest_offending_row_and_column_are_reported_within_a_check() -> None:
    frame = base_frame()
    frame.iloc[9, frame.columns.get_loc("open")] = np.nan
    frame.iloc[5, frame.columns.get_loc("volume")] = np.nan
    frame.iloc[5, frame.columns.get_loc("high")] = np.nan

    with pytest.raises(CandleValuesError) as caught:
        validate_candles(frame)

    assert (caught.value.position, caught.value.column, caught.value.count) == (5, "high", 2)


def test_column_messages_are_truncated() -> None:
    labels = [f"column_{number}_{'x' * 50}" for number in range(1_000)]
    frame = pd.DataFrame(
        np.ones((2, len(labels))),
        columns=labels,
        index=pd.DatetimeIndex(["2024-01-01", "2024-01-02"], tz="UTC"),
    )

    with pytest.raises(CandleColumnsError) as caught:
        validate_candles(frame)

    message = str(caught.value)
    assert "open" in message
    assert "column_0_" in message
    assert "column_999_" not in message
    assert len(message) < 400


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="none"),
        pytest.param(pd.Series([1.0, 2.0]), id="series"),
        pytest.param({"open": [1.0]}, id="dict"),
    ],
)
def test_non_frames_raise_type_error(value: object) -> None:
    with pytest.raises(TypeError, match="DataFrame") as caught:
        validate_candles(value)  # type: ignore[arg-type]

    assert not isinstance(caught.value, CandleValidationError)


# --- AC8: constants -------------------------------------------------------------------------


def test_contract_constants() -> None:
    assert OHLCV_COLUMNS == ("open", "high", "low", "close", "volume")
    assert PRICE_COLUMNS == ("open", "high", "low", "close")
    assert isinstance(OHLCV_COLUMNS, tuple)
    assert isinstance(PRICE_COLUMNS, tuple)


def test_error_kinds_are_the_thirteen_checks_in_order() -> None:
    assert [kind.value for kind in CandleErrorKind] == [
        "index_type",
        "timezone",
        "missing_timestamp",
        "duplicate_timestamp",
        "unsorted_index",
        "columns",
        "dtype",
        "missing_value",
        "infinite_value",
        "non_positive_price",
        "negative_volume",
        "high_below_low",
        "body_outside_range",
    ]


def test_column_messages_stay_bounded_for_escape_heavy_labels() -> None:
    labels = [chr(0xE0001) * 1_000 + str(number) for number in range(20)]
    frame = pd.DataFrame(
        np.ones((1, len(labels))),
        columns=labels,
        index=pd.DatetimeIndex(["2024-01-01"], tz="UTC"),
    )

    with pytest.raises(CandleColumnsError) as caught:
        validate_candles(frame)

    message = str(caught.value)
    assert chr(0x0A) not in message
    assert len(message) < 500
