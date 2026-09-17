"""Tests of candle normalization (spec 010, T1-T4: AC1-AC4).

``normalize_candles`` turns a provider frame into the canonical candle frame (UTC ``us`` index,
exactly the five ``float64`` columns) and reports every dropped row with the first matching
reason of Design 3.3. Every expected value is a literal written from the spec tables; nothing is
recomputed with the code under test. The calendar is ``nyse_test_calendar()`` unless a test says
otherwise.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable
from datetime import UTC, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.calendars import NEW_YORK, nyse_test_calendar, toy_calendar, utc
from tests.fixtures.session_candles import provider_shaped, session_candles
from trading_bot.domain import candle_normalization as normalization_module
from trading_bot.domain.candle_normalization import (
    CandleNormalizationError,
    DroppedRow,
    DropReason,
    NormalizationErrorKind,
    NormalizedCandles,
    normalize_candles,
)
from trading_bot.domain.candles import OHLCV_COLUMNS, validate_candles
from trading_bot.domain.market_calendar.sessions import CandleLabelErrorKind
from trading_bot.domain.timeframe import Timeframe

H1 = Timeframe.H1
H4 = Timeframe.H4
D1 = Timeframe.D1
NYSE = nyse_test_calendar()
CANONICAL_INDEX = pd.DatetimeTZDtype(unit="us", tz="UTC")
NAN = math.nan
INF = math.inf


def grid(timeframe: Timeframe, start: str, end: str) -> pd.DataFrame:
    """Canonical candles on the NYSE grid with ``start <= label < end`` (UTC wall times)."""
    return session_candles(NYSE, timeframe, utc(start), utc(end))


def base_raw() -> pd.DataFrame:
    """Three valid ``1h`` candles of 2024-07-02, shaped like yfinance (ET, ``s``, 8 columns)."""
    return provider_shaped(grid(H1, "2024-07-02T13:30", "2024-07-02T16:30"))


def canonical(
    labels: list[str], rows: list[tuple[float, float, float, float, float]]
) -> pd.DataFrame:
    """The expected canonical frame: UTC wall-time labels and ``(o, h, l, c, v)`` rows."""
    index = pd.DatetimeIndex(
        [pd.Timestamp(label, tz="UTC") for label in labels], dtype=CANONICAL_INDEX
    )
    values = np.asarray(rows, dtype=np.float64).reshape(len(rows), len(OHLCV_COLUMNS))
    return pd.DataFrame(values, index=index, columns=list(OHLCV_COLUMNS))


def dropped(position: int, label: str | None, reason: DropReason) -> DroppedRow:
    """A ``DroppedRow`` with a UTC wall-time label (``None`` for ``NaT``)."""
    stamp = None if label is None else pd.Timestamp(label, tz="UTC")
    return DroppedRow(position=position, label=stamp, reason=reason)


# --- T1: API and value types (AC1) -------------------------------------------------------------


def test_the_module_exports_exactly_the_spec_names() -> None:
    assert normalization_module.__all__ == [
        "CandleNormalizationError",
        "DropReason",
        "DroppedRow",
        "NormalizationErrorKind",
        "NormalizedCandles",
        "normalize_candles",
    ]


def test_normalized_candles_is_frozen_slotted_keyword_only_and_compared_by_identity() -> None:
    frame = grid(H1, "2024-07-02T13:30", "2024-07-02T15:30")
    first = NormalizedCandles(candles=frame, dropped=())
    second = NormalizedCandles(candles=frame, dropped=())

    assert "__slots__" in NormalizedCandles.__dict__
    assert all(field.kw_only for field in dataclasses.fields(NormalizedCandles))
    assert first == first
    assert first != second  # eq=False: a frame has no boolean equality
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.dropped = ()  # type: ignore[misc]
    with pytest.raises(TypeError):
        NormalizedCandles(frame, ())  # type: ignore[misc]


def test_dropped_row_is_frozen_slotted_keyword_only_equal_by_value_and_hashable() -> None:
    label = pd.Timestamp("2024-07-02T14:30", tz="UTC")
    first = DroppedRow(position=1, label=label, reason=DropReason.MISSING_VALUE)
    second = DroppedRow(position=1, label=label, reason=DropReason.MISSING_VALUE)
    other = DroppedRow(position=1, label=None, reason=DropReason.MISSING_TIMESTAMP)

    assert "__slots__" in DroppedRow.__dict__
    assert all(field.kw_only for field in dataclasses.fields(DroppedRow))
    assert first == second
    assert hash(first) == hash(second)
    assert first != other
    assert len({first, second, other}) == 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.position = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        DroppedRow(1, label, DropReason.MISSING_VALUE)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("raw", "timeframe", "calendar"),
    [
        pytest.param(base_raw()["Close"], H1, NYSE, id="series"),
        pytest.param({"Close": [1.0]}, H1, NYSE, id="dict"),
        pytest.param(None, H1, NYSE, id="none-frame"),
        pytest.param(base_raw(), "1h", NYSE, id="timeframe-code"),
        pytest.param(base_raw(), None, NYSE, id="timeframe-none"),
        pytest.param(base_raw(), H1, None, id="calendar-none"),
        pytest.param(base_raw(), H1, "XNYS", id="calendar-name"),
    ],
)
def test_arguments_of_the_wrong_type_raise_type_error(
    raw: object, timeframe: object, calendar: object
) -> None:
    with pytest.raises(TypeError):
        normalize_candles(raw, timeframe, calendar=calendar)  # type: ignore[arg-type]


def test_argument_types_are_checked_before_the_frame_structure() -> None:
    naive = base_raw().tz_localize(None)

    with pytest.raises(TypeError):
        normalize_candles(naive, "1h", calendar=NYSE)  # type: ignore[arg-type]


# --- T2: structural errors (AC2) ---------------------------------------------------------------


def _with_index(index: pd.Index) -> pd.DataFrame:
    return base_raw().set_axis(index, axis=0)


def _without(*columns: str) -> pd.DataFrame:
    return base_raw().drop(columns=list(columns))


def _with_dtype(column: str, dtype: str) -> pd.DataFrame:
    frame = base_raw()
    frame[column] = frame[column].astype(dtype)
    return frame


def _with_column(column: str, values: object) -> pd.DataFrame:
    frame = base_raw()
    frame[column] = values
    return frame


def _naive_empty() -> pd.DataFrame:
    empty = base_raw().iloc[:0]
    return empty.set_axis(pd.DatetimeIndex([], dtype="datetime64[s]"), axis=0)


def _multiindex_columns() -> pd.DataFrame:
    frame = base_raw()
    frame.columns = pd.MultiIndex.from_product(
        [list(frame.columns), ["AAPL"]], names=["Price", "Ticker"]
    )
    return frame


def _extra_column(label: str, source: str) -> pd.DataFrame:
    frame = base_raw()
    frame[label] = frame[source]
    return frame


STRUCTURAL_CASES: list[
    tuple[str, Callable[[], pd.DataFrame], NormalizationErrorKind, str | None]
] = [
    (
        "range-index",
        lambda: base_raw().reset_index(drop=True),
        NormalizationErrorKind.INDEX_TYPE,
        None,
    ),
    (
        "iso-string-index",
        lambda: _with_index(
            pd.Index(["2024-07-02T13:30Z", "2024-07-02T14:30Z", "2024-07-02T15:30Z"])
        ),
        NormalizationErrorKind.INDEX_TYPE,
        None,
    ),
    (
        "period-index",
        lambda: _with_index(pd.period_range("2024-07-02 13:00", periods=3, freq="h")),
        NormalizationErrorKind.INDEX_TYPE,
        None,
    ),
    ("naive-index", lambda: base_raw().tz_localize(None), NormalizationErrorKind.NAIVE_INDEX, None),
    ("naive-empty-index", _naive_empty, NormalizationErrorKind.NAIVE_INDEX, None),
    ("multiindex-columns", _multiindex_columns, NormalizationErrorKind.MULTIINDEX_COLUMNS, None),
    (
        "close-and-close",
        lambda: _extra_column("close", "Close"),
        NormalizationErrorKind.AMBIGUOUS_COLUMN,
        "close",
    ),
    (
        "volume-and-padded-volume",
        lambda: _extra_column(" volume ", "Volume"),
        NormalizationErrorKind.AMBIGUOUS_COLUMN,
        "volume",
    ),
    ("no-volume", lambda: _without("Volume"), NormalizationErrorKind.MISSING_COLUMN, "volume"),
    ("only-adj-close", lambda: _without("Close"), NormalizationErrorKind.MISSING_COLUMN, "close"),
    (
        "no-columns",
        lambda: pd.DataFrame(index=base_raw().index),
        NormalizationErrorKind.MISSING_COLUMN,
        "open",
    ),
    (
        "object-numbers",
        lambda: _with_dtype("Close", "object"),
        NormalizationErrorKind.NON_NUMERIC_COLUMN,
        "close",
    ),
    (
        "string",
        lambda: _with_dtype("Open", "string"),
        NormalizationErrorKind.NON_NUMERIC_COLUMN,
        "open",
    ),
    (
        "bool",
        lambda: _with_column("Volume", [True, False, True]),
        NormalizationErrorKind.NON_NUMERIC_COLUMN,
        "volume",
    ),
    (
        "nullable-boolean",
        lambda: _with_column("Volume", pd.array([True, False, None], dtype="boolean")),
        NormalizationErrorKind.NON_NUMERIC_COLUMN,
        "volume",
    ),
    (
        "datetime64",
        lambda: _with_column(
            "High", pd.to_datetime(["2024-07-02", "2024-07-03", "2024-07-04"]).to_numpy()
        ),
        NormalizationErrorKind.NON_NUMERIC_COLUMN,
        "high",
    ),
    (
        "category",
        lambda: _with_dtype("Low", "category"),
        NormalizationErrorKind.NON_NUMERIC_COLUMN,
        "low",
    ),
]


@pytest.mark.parametrize(
    ("build", "kind", "column"),
    [
        pytest.param(build, kind, column, id=case_id)
        for case_id, build, kind, column in STRUCTURAL_CASES
    ],
)
def test_each_structural_defect_raises_its_kind_and_column(
    build: Callable[[], pd.DataFrame], kind: NormalizationErrorKind, column: str | None
) -> None:
    raw = build()

    with pytest.raises(CandleNormalizationError) as caught:
        normalize_candles(raw, H1, calendar=NYSE)

    error = caught.value
    assert isinstance(error, ValueError)
    assert error.kind is kind
    assert error.column == column
    message = str(error)
    assert message.startswith(f"{kind.value}: ")
    assert "\n" not in message
    assert "\r" not in message
    assert len(message) < 300


@pytest.mark.parametrize(
    ("build", "kind", "column"),
    [
        pytest.param(
            lambda: _multiindex_columns().tz_localize(None),
            NormalizationErrorKind.NAIVE_INDEX,
            None,
            id="naive-index-before-multiindex-columns",
        ),
        pytest.param(
            lambda: _multiindex_columns().reset_index(drop=True),
            NormalizationErrorKind.INDEX_TYPE,
            None,
            id="index-type-before-multiindex-columns",
        ),
        pytest.param(
            lambda: _extra_column("close", "Close").drop(columns=["Volume"]),
            NormalizationErrorKind.AMBIGUOUS_COLUMN,
            "close",
            id="ambiguous-before-missing",
        ),
        pytest.param(
            lambda: _extra_column("volume", "Volume").drop(columns=["Open"]),
            NormalizationErrorKind.AMBIGUOUS_COLUMN,
            "volume",
            id="ambiguous-volume-before-missing-open",
        ),
        pytest.param(
            lambda: _with_dtype("Open", "object").drop(columns=["Volume"]),
            NormalizationErrorKind.MISSING_COLUMN,
            "volume",
            id="missing-before-non-numeric",
        ),
        pytest.param(
            lambda: _extra_column("volume", "Volume").pipe(
                lambda f: f.assign(Close=f["Close"].astype(object))
            ),
            NormalizationErrorKind.AMBIGUOUS_COLUMN,
            "volume",
            id="ambiguous-before-non-numeric",
        ),
        pytest.param(
            lambda: _with_dtype("Close", "object").pipe(
                lambda f: f.assign(Open=f["Open"].astype(object))
            ),
            NormalizationErrorKind.NON_NUMERIC_COLUMN,
            "open",
            id="first-non-numeric-in-ohlcv-order",
        ),
        pytest.param(
            lambda: _without("Volume", "High"),
            NormalizationErrorKind.MISSING_COLUMN,
            "high",
            id="first-missing-in-ohlcv-order",
        ),
    ],
)
def test_the_first_failing_check_in_table_order_raises(
    build: Callable[[], pd.DataFrame], kind: NormalizationErrorKind, column: str | None
) -> None:
    with pytest.raises(CandleNormalizationError) as caught:
        normalize_candles(build(), H1, calendar=NYSE)

    assert caught.value.kind is kind
    assert caught.value.column == column


def test_messages_never_echo_raw_column_labels() -> None:
    frame = base_raw().rename(columns={"Close": " CLOSE\r\n"})
    frame["close"] = frame[" CLOSE\r\n"]
    frame["secret-label-7f3a"] = 1.0

    with pytest.raises(CandleNormalizationError) as caught:
        normalize_candles(frame, H1, calendar=NYSE)

    message = str(caught.value)
    assert caught.value.kind is NormalizationErrorKind.AMBIGUOUS_COLUMN
    assert "CLOSE" not in message
    assert "secret-label-7f3a" not in message
    assert "\n" not in message
    assert "\r" not in message


def test_a_non_numeric_dtype_is_echoed_as_a_bounded_repr() -> None:
    zone = "America/Argentina/ComodRivadavia"
    frame = _with_column("Close", pd.date_range("2024-07-02", periods=3, freq="D", tz=zone))
    dtype_name = str(frame["Close"].dtype)
    assert len(dtype_name) > 32

    with pytest.raises(CandleNormalizationError) as caught:
        normalize_candles(frame, H1, calendar=NYSE)

    message = str(caught.value)
    echoes = [part for part in message.split(" ") if part.startswith("'")]
    assert caught.value.kind is NormalizationErrorKind.NON_NUMERIC_COLUMN
    assert dtype_name not in message
    assert repr(dtype_name[:30]) in message
    assert all(len(echo) <= 32 for echo in echoes)


def test_a_short_dtype_name_is_echoed_whole() -> None:
    with pytest.raises(CandleNormalizationError) as caught:
        normalize_candles(_with_dtype("Close", "object"), H1, calendar=NYSE)

    assert "'object'" in str(caught.value)


def test_an_empty_frame_with_a_valid_structure_passes_the_structural_checks() -> None:
    result = normalize_candles(base_raw().iloc[:0], H1, calendar=NYSE)

    assert result.dropped == ()
    assert len(result.candles) == 0
    assert list(result.candles.columns) == list(OHLCV_COLUMNS)
    assert result.candles.index.dtype == CANONICAL_INDEX


# --- T3: canonical frame (AC3) -----------------------------------------------------------------


def assert_canonical(frame: pd.DataFrame) -> None:
    assert validate_candles(frame) is frame
    assert frame.index.dtype == CANONICAL_INDEX
    assert frame.index.name is None
    assert frame.index.freq is None  # type: ignore[attr-defined]
    assert frame.index.is_monotonic_increasing
    assert frame.index.is_unique
    assert list(frame.columns) == list(OHLCV_COLUMNS)
    assert frame.columns.name is None
    assert all(dtype == np.dtype(np.float64) for dtype in frame.dtypes)


def test_a_provider_shaped_frame_becomes_a_new_canonical_frame_and_raw_is_unchanged() -> None:
    raw = base_raw()
    before = raw.copy(deep=True)

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert_canonical(result.candles)
    assert result.candles is not raw
    assert result.dropped == ()
    pd.testing.assert_frame_equal(raw, before, check_exact=True)
    assert raw.index.dtype == before.index.dtype
    assert raw.index.name == "Datetime"


def test_a_canonical_frame_is_returned_as_a_new_object() -> None:
    frame = grid(H1, "2024-07-02T13:30", "2024-07-02T16:30")
    before = frame.copy(deep=True)

    result = normalize_candles(frame, H1, calendar=NYSE)

    assert result.candles is not frame
    pd.testing.assert_frame_equal(result.candles, before, check_exact=True)
    pd.testing.assert_frame_equal(frame, before, check_exact=True)


def test_the_round_trip_through_provider_shaped_restores_the_candles() -> None:
    for timeframe, start, end in [
        (H1, "2024-06-24T00:00", "2024-07-10T00:00"),
        (H4, "2024-03-01T00:00", "2024-03-20T00:00"),
        (D1, "2023-12-20T00:00", "2024-01-20T00:00"),
    ]:
        candles = grid(timeframe, start, end)

        result = normalize_candles(provider_shaped(candles), timeframe, calendar=NYSE)

        expected = candles.assign(volume=np.rint(candles["volume"]))
        assert result.dropped == ()
        pd.testing.assert_frame_equal(result.candles, expected, check_exact=True)


def test_an_index_with_a_frequency_gives_an_index_without_one() -> None:
    index = pd.date_range("2024-07-02 13:30", periods=7, freq="h", tz="UTC", name="Datetime")
    raw = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0}, index=index
    )
    assert raw.index.freq is not None  # type: ignore[attr-defined]

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.dropped == ()
    assert_canonical(result.candles)
    assert len(result.candles) == 7


def test_columns_match_case_insensitively_after_stripping_and_extras_are_ignored() -> None:
    raw = base_raw()
    renamed = raw.rename(
        columns={
            "Open": " OPEN ",
            "High": "hIgH",
            "Low": "\tlow",
            "Close": "Close ",
            "Volume": "VOLUME",
        }
    )
    renamed[0] = 5.0
    renamed[("tuple", "label")] = "text"
    renamed[None] = "ignored"

    result = normalize_candles(renamed, H1, calendar=NYSE)

    expected = normalize_candles(raw, H1, calendar=NYSE)
    assert result.dropped == ()
    pd.testing.assert_frame_equal(result.candles, expected.candles, check_exact=True)


def test_column_order_does_not_matter() -> None:
    raw = base_raw()
    reordered = raw[
        ["Stock Splits", "Volume", "Close", "Adj Close", "Low", "Dividends", "High", "Open"]
    ]

    result = normalize_candles(reordered, H1, calendar=NYSE)

    pd.testing.assert_frame_equal(
        result.candles, normalize_candles(raw, H1, calendar=NYSE).candles, check_exact=True
    )


@pytest.mark.parametrize("dtype", ["int64", "float32", "Int64", "Float64", "uint32"])
def test_integer_float_and_nullable_numeric_columns_are_cast_to_float64(dtype: str) -> None:
    index = pd.DatetimeIndex(["2024-07-02 09:30", "2024-07-02 10:30"], tz=NEW_YORK)
    raw = pd.DataFrame(
        {
            "Open": [100, 104],
            "High": [108, 112],
            "Low": [96, 100],
            "Close": [104, 108],
            "Volume": [1000, 2000],
        },
        index=index,
    ).astype(dtype)

    result = normalize_candles(raw, H1, calendar=NYSE)

    expected = canonical(
        ["2024-07-02T13:30", "2024-07-02T14:30"],
        [(100.0, 108.0, 96.0, 104.0, 1000.0), (104.0, 112.0, 100.0, 108.0, 2000.0)],
    )
    assert result.dropped == ()
    pd.testing.assert_frame_equal(result.candles, expected, check_exact=True)


def test_float32_values_keep_their_exact_binary_value() -> None:
    index = pd.DatetimeIndex(["2024-07-02 13:30"], tz="UTC")
    raw = pd.DataFrame(
        {"open": [100.1], "high": [101.2], "low": [99.3], "close": [100.4], "volume": [7.0]},
        index=index,
    ).astype("float32")

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.candles.iloc[0].tolist() == [
        float(np.float32(100.1)),
        float(np.float32(101.2)),
        float(np.float32(99.3)),
        float(np.float32(100.4)),
        7.0,
    ]


@pytest.mark.parametrize(
    ("column", "dtype", "value"), [("Volume", "Int64", 10), ("Close", "Float64", 100.5)]
)
def test_pd_na_in_a_nullable_column_becomes_nan_and_drops_the_row(
    column: str, dtype: str, value: float
) -> None:
    index = pd.DatetimeIndex(["2024-07-02 09:30", "2024-07-02 10:30"], tz=NEW_YORK)
    raw = pd.DataFrame(
        {
            "Open": [100, 100],
            "High": [101, 101],
            "Low": [99, 99],
            "Close": [100, 100],
            "Volume": [10, 10],
        },
        index=index,
    )
    raw[column] = pd.array([value, None], dtype=dtype)

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.dropped == (dropped(1, "2024-07-02T14:30", DropReason.MISSING_VALUE),)
    assert result.candles.index.tolist() == [pd.Timestamp("2024-07-02T13:30", tz="UTC")]


def _shapes() -> list[tuple[str, pd.DataFrame]]:
    candles = grid(H1, "2024-07-01T00:00", "2024-07-10T00:00")
    order = np.random.default_rng(11).permutation(len(candles))
    shapes: list[tuple[str, pd.DataFrame]] = []
    for zone_name, zone in [
        ("new-york", NEW_YORK),
        ("utc", UTC),
        ("fixed-minus-4", timezone(timedelta(hours=-4))),
    ]:
        for unit in ("s", "ms", "us", "ns"):
            shaped = provider_shaped(candles, timezone=zone, unit=unit)  # type: ignore[arg-type]
            shapes.append((f"{zone_name}-{unit}", shaped))
            shapes.append((f"{zone_name}-{unit}-shuffled", shaped.iloc[order]))
    return shapes


def test_zones_units_and_row_order_give_identical_frames_and_nominal_closes() -> None:
    shapes = _shapes()
    reference = normalize_candles(shapes[0][1], H1, calendar=NYSE).candles

    for name, raw in shapes:
        result = normalize_candles(raw, H1, calendar=NYSE)

        assert result.dropped == (), name
        assert result.candles.index.dtype == reference.index.dtype, name
        pd.testing.assert_frame_equal(result.candles, reference, check_exact=True)
        assert H1.nominal_close(result.candles.index[-1]) == H1.nominal_close(
            reference.index[-1]
        ), name
    assert len(reference) == 39


def test_normalization_is_idempotent() -> None:
    raw = golden_raw()
    first = normalize_candles(raw, H1, calendar=NYSE)

    second = normalize_candles(first.candles, H1, calendar=NYSE)

    assert second.dropped == ()
    pd.testing.assert_frame_equal(second.candles, first.candles, check_exact=True)


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_zero_rows_with_a_valid_structure_give_an_empty_canonical_frame(
    timeframe: Timeframe,
) -> None:
    raw = provider_shaped(grid(timeframe, "2024-07-02T00:00", "2024-07-02T00:00"), unit="ns")

    result = normalize_candles(raw, timeframe, calendar=NYSE)

    assert len(raw) == 0
    assert result.dropped == ()
    assert len(result.candles) == 0
    assert_canonical(result.candles)


def test_the_toy_calendar_is_honored() -> None:
    raw = provider_shaped(
        session_candles(toy_calendar(), H1, utc("2024-03-13T00:00"), utc("2024-03-14T00:00"))
    )

    result = normalize_candles(raw, H1, calendar=toy_calendar())

    assert result.dropped == ()
    assert [label.isoformat() for label in result.candles.index] == [
        "2024-03-13T14:00:00+00:00",
        "2024-03-13T15:00:00+00:00",
        "2024-03-13T16:00:00+00:00",
        "2024-03-13T17:00:00+00:00",
        "2024-03-13T18:00:00+00:00",
    ]


# --- T4: dropped rows (AC4) --------------------------------------------------------------------

GOLDEN_LABELS_ET: tuple[str | None, ...] = (
    "2024-07-02 09:30",
    "2024-07-02 10:30",
    "2024-07-02 11:30",
    "2024-07-02 12:30",
    "2024-07-02 13:30",
    "2024-07-02 13:30",
    "2024-07-02 14:30",
    "2024-07-02 14:30",
    "2024-07-02 15:30",
    "2024-07-02 16:00",
    "2024-07-02 10:00",
    "2024-07-03 08:00",
    "2024-07-03 09:30",
    "2024-07-03 10:30",
    "2024-07-03 11:30",
    "2024-07-03 12:30",
    "2024-07-04 10:30",
    None,
    "2020-12-31 10:30",
    "2024-07-02 11:00",
    "2024-07-02 09:30",
)
GOLDEN_CHANGES: dict[int, dict[str, float]] = {
    1: {"Close": NAN},
    3: {"High": 98.0},
    7: {"Close": 100.25},
    8: {"Volume": -1},
    12: {"Open": 0.0},
    13: {"High": INF},
    19: {"Close": NAN},
    20: {"Close": NAN},
}
GOLDEN_DROPPED = (
    dropped(1, "2024-07-02T14:30", DropReason.MISSING_VALUE),
    dropped(3, "2024-07-02T16:30", DropReason.INCONSISTENT_RANGE),
    dropped(5, "2024-07-02T17:30", DropReason.DUPLICATE),
    dropped(6, "2024-07-02T18:30", DropReason.CONFLICTING_DUPLICATE),
    dropped(7, "2024-07-02T18:30", DropReason.CONFLICTING_DUPLICATE),
    dropped(8, "2024-07-02T19:30", DropReason.NEGATIVE_VOLUME),
    dropped(9, "2024-07-02T20:00", DropReason.OUTSIDE_SESSION),
    dropped(10, "2024-07-02T14:00", DropReason.OFF_GRID),
    dropped(11, "2024-07-03T12:00", DropReason.OUTSIDE_SESSION),
    dropped(12, "2024-07-03T13:30", DropReason.NON_POSITIVE_PRICE),
    dropped(13, "2024-07-03T14:30", DropReason.INFINITE_VALUE),
    dropped(16, "2024-07-04T14:30", DropReason.NOT_A_SESSION),
    dropped(17, None, DropReason.MISSING_TIMESTAMP),
    dropped(18, "2020-12-31T15:30", DropReason.OUTSIDE_CALENDAR),
    dropped(19, "2024-07-02T15:00", DropReason.OFF_GRID),
    dropped(20, "2024-07-02T13:30", DropReason.MISSING_VALUE),
)
GOLDEN_KEPT = canonical(
    [
        "2024-07-02T13:30",
        "2024-07-02T15:30",
        "2024-07-02T17:30",
        "2024-07-03T15:30",
        "2024-07-03T16:30",
    ],
    [(100.0, 101.0, 99.0, 100.5, 1000.0)] * 5,
)


def golden_raw(unit: str = "s") -> pd.DataFrame:
    """The golden raw frame of spec 010, Design 3.4 (yfinance ``Ticker.history`` shape)."""
    columns: dict[str, list[float]] = {"Open": [], "High": [], "Low": [], "Close": [], "Volume": []}
    valid = {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1000.0}
    for position in range(len(GOLDEN_LABELS_ET)):
        row = valid | GOLDEN_CHANGES.get(position, {})
        for name, values in columns.items():
            values.append(row[name])
    index = pd.DatetimeIndex(list(GOLDEN_LABELS_ET), tz="America/New_York")
    return pd.DataFrame(
        {
            "Open": columns["Open"],
            "High": columns["High"],
            "Low": columns["Low"],
            "Close": columns["Close"],
            "Adj Close": columns["Close"],
            "Volume": np.asarray(columns["Volume"], dtype=np.int64),
            "Dividends": 0.0,
            "Stock Splits": 0.0,
        },
        index=index.as_unit(unit).rename("Datetime"),  # type: ignore[arg-type]
    )


def test_the_golden_raw_frame_has_the_spec_shape() -> None:
    raw = golden_raw()

    assert str(raw.index.dtype) == "datetime64[s, America/New_York]"
    assert raw.index.name == "Datetime"
    assert list(raw.columns) == [
        "Open",
        "High",
        "Low",
        "Close",
        "Adj Close",
        "Volume",
        "Dividends",
        "Stock Splits",
    ]
    assert raw["Volume"].dtype == np.dtype(np.int64)
    assert len(raw) == 21


def test_the_golden_raw_frame_gives_the_spec_dropped_rows_and_kept_rows() -> None:
    result = normalize_candles(golden_raw(), H1, calendar=NYSE)

    assert result.dropped == GOLDEN_DROPPED
    pd.testing.assert_frame_equal(result.candles, GOLDEN_KEPT, check_exact=True)


def test_dropped_labels_are_utc_and_keep_the_input_unit() -> None:
    result = normalize_candles(golden_raw(), H1, calendar=NYSE)

    labels = [row.label for row in result.dropped if row.label is not None]
    assert all(label.tz is not None and label.utcoffset() == timedelta(0) for label in labels)
    assert {str(label.tz) for label in labels} == {"UTC"}
    assert {label.unit for label in labels} == {"s"}


def test_the_ns_variant_reports_a_sub_microsecond_label_as_off_grid() -> None:
    raw = golden_raw("ns")
    extra_index = pd.DatetimeIndex(["2024-07-03 11:30:00.000000001"], tz="America/New_York")
    extra = raw.iloc[[0]].set_axis(extra_index.as_unit("ns").rename("Datetime"), axis=0)
    variant = pd.concat([raw, extra])
    assert str(variant.index.dtype) == "datetime64[ns, America/New_York]"

    result = normalize_candles(variant, H1, calendar=NYSE)

    sub_microsecond = DroppedRow(
        position=21,
        label=pd.Timestamp("2024-07-03 15:30:00.000000001", tz="UTC"),
        reason=DropReason.OFF_GRID,
    )
    assert result.dropped == (*GOLDEN_DROPPED, sub_microsecond)
    assert result.dropped[-1].label is not None
    assert result.dropped[-1].label.nanosecond == 1
    pd.testing.assert_frame_equal(result.candles, GOLDEN_KEPT, check_exact=True)


def _rows_frame(
    labels: list[str | None], rows: list[tuple[float, float, float, float, float]]
) -> pd.DataFrame:
    index = pd.DatetimeIndex(labels, tz="UTC").as_unit("ns")
    return pd.DataFrame(rows, index=index, columns=list(OHLCV_COLUMNS), dtype=np.float64)


VALID = (100.0, 101.0, 99.0, 100.5, 1000.0)


@pytest.mark.parametrize(
    ("label", "row", "reason"),
    [
        pytest.param(
            None, (NAN, INF, -1.0, 0.0, -5.0), DropReason.MISSING_TIMESTAMP, id="nat-first"
        ),
        pytest.param(
            "2020-12-31T15:30:00.000000001",
            VALID,
            DropReason.OUTSIDE_CALENDAR,
            id="outside-calendar-before-sub-us",
        ),
        pytest.param("2028-01-03T15:30", VALID, DropReason.OUTSIDE_CALENDAR, id="after-coverage"),
        pytest.param(
            "2024-07-04T14:30:00.000000001",
            VALID,
            DropReason.OFF_GRID,
            id="sub-us-before-not-a-session",
        ),
        pytest.param(
            "2024-07-02T12:00",
            (NAN, NAN, NAN, NAN, NAN),
            DropReason.OUTSIDE_SESSION,
            id="label-before-values",
        ),
        pytest.param(
            "2024-07-02T13:30",
            (NAN, INF, 0.0, 100.0, -1.0),
            DropReason.MISSING_VALUE,
            id="nan-before-inf",
        ),
        pytest.param(
            "2024-07-02T13:30",
            (100.0, -INF, 0.0, 100.0, -1.0),
            DropReason.INFINITE_VALUE,
            id="inf-before-price",
        ),
        pytest.param(
            "2024-07-02T13:30",
            (0.0, 101.0, 99.0, 100.0, -1.0),
            DropReason.NON_POSITIVE_PRICE,
            id="price-before-volume",
        ),
        pytest.param(
            "2024-07-02T13:30",
            (100.0, 101.0, -99.0, 100.0, 1.0),
            DropReason.NON_POSITIVE_PRICE,
            id="negative-low",
        ),
        pytest.param(
            "2024-07-02T13:30",
            (100.0, 98.0, 99.0, 100.0, -1.0),
            DropReason.NEGATIVE_VOLUME,
            id="volume-before-range",
        ),
        pytest.param(
            "2024-07-02T13:30",
            (100.0, 98.0, 99.0, 98.5, 1.0),
            DropReason.INCONSISTENT_RANGE,
            id="high-below-low",
        ),
        pytest.param(
            "2024-07-02T13:30",
            (102.0, 101.0, 99.0, 100.0, 1.0),
            DropReason.INCONSISTENT_RANGE,
            id="open-above-high",
        ),
        pytest.param(
            "2024-07-02T13:30",
            (98.0, 101.0, 99.0, 100.0, 1.0),
            DropReason.INCONSISTENT_RANGE,
            id="open-below-low",
        ),
        pytest.param(
            "2024-07-02T13:30",
            (100.0, 101.0, 99.0, 101.5, 1.0),
            DropReason.INCONSISTENT_RANGE,
            id="close-above-high",
        ),
        pytest.param(
            "2024-07-02T13:30",
            (100.0, 101.0, 99.0, 98.5, 1.0),
            DropReason.INCONSISTENT_RANGE,
            id="close-below-low",
        ),
    ],
)
def test_a_row_gets_the_first_matching_reason(
    label: str | None, row: tuple[float, float, float, float, float], reason: DropReason
) -> None:
    raw = _rows_frame([label], [row])

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert [(item.position, item.reason) for item in result.dropped] == [(0, reason)]
    assert len(result.candles) == 0


def test_flat_zero_volume_candles_and_negative_zero_volume_are_kept() -> None:
    raw = _rows_frame(
        ["2024-07-02T13:30", "2024-07-02T14:30"],
        [(100.0, 100.0, 100.0, 100.0, 0.0), (100.0, 100.0, 100.0, 100.0, -0.0)],
    )

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.dropped == ()
    assert len(result.candles) == 2


@pytest.mark.parametrize(
    ("timeframe", "label", "reason"),
    [
        pytest.param(H4, "2024-07-02T15:30", DropReason.OFF_GRID, id="4h-off-grid"),
        pytest.param(H4, "2024-07-02T12:00", DropReason.OUTSIDE_SESSION, id="4h-pre-market"),
        pytest.param(H4, "2024-07-04T13:30", DropReason.NOT_A_SESSION, id="4h-holiday"),
        pytest.param(D1, "2024-07-03T00:00", DropReason.OFF_GRID, id="1d-midnight-utc"),
        pytest.param(D1, "2024-07-04T04:00", DropReason.NOT_A_SESSION, id="1d-holiday"),
        pytest.param(D1, "2024-07-06T04:00", DropReason.NOT_A_SESSION, id="1d-saturday"),
        pytest.param(D1, "2024-07-02T13:30", DropReason.OFF_GRID, id="1d-session-open"),
    ],
)
def test_calendar_rejections_on_other_timeframes_use_the_calendar_kind(
    timeframe: Timeframe, label: str, reason: DropReason
) -> None:
    valid_label = {H4: "2024-07-02T13:30", D1: "2024-07-02T04:00"}[timeframe]
    raw = _rows_frame([valid_label, label], [VALID, VALID])

    result = normalize_candles(raw, timeframe, calendar=NYSE)

    assert result.dropped == (dropped(1, label, reason),)
    assert result.candles.index.tolist() == [pd.Timestamp(valid_label, tz="UTC")]


def test_daily_candles_labelled_at_midnight_new_york_are_kept() -> None:
    raw = provider_shaped(grid(D1, "2024-07-01T00:00", "2024-07-10T00:00"))

    result = normalize_candles(raw, D1, calendar=NYSE)

    assert result.dropped == ()
    assert [label.isoformat() for label in result.candles.index] == [
        "2024-07-01T04:00:00+00:00",
        "2024-07-02T04:00:00+00:00",
        "2024-07-03T04:00:00+00:00",
        "2024-07-05T04:00:00+00:00",
        "2024-07-08T04:00:00+00:00",
        "2024-07-09T04:00:00+00:00",
    ]


def test_three_identical_copies_keep_the_first_and_report_two_duplicates() -> None:
    label = "2024-07-02T13:30"
    raw = _rows_frame([label, "2024-07-02T14:30", label, label], [VALID, VALID, VALID, VALID])

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.dropped == (
        dropped(2, label, DropReason.DUPLICATE),
        dropped(3, label, DropReason.DUPLICATE),
    )
    assert len(result.candles) == 2


def test_any_differing_copy_makes_every_surviving_copy_conflicting() -> None:
    label = "2024-07-02T13:30"
    other = (100.0, 101.0, 99.0, 100.5, 1001.0)
    raw = _rows_frame([label, label, "2024-07-02T15:30", label], [VALID, VALID, VALID, other])

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.dropped == (
        dropped(0, label, DropReason.CONFLICTING_DUPLICATE),
        dropped(1, label, DropReason.CONFLICTING_DUPLICATE),
        dropped(3, label, DropReason.CONFLICTING_DUPLICATE),
    )
    assert result.candles.index.tolist() == [pd.Timestamp("2024-07-02T15:30", tz="UTC")]


def test_duplicates_are_judged_among_surviving_rows_only() -> None:
    label = "2024-07-02T13:30"
    other = (100.0, 101.0, 99.0, 100.25, 1000.0)
    raw = _rows_frame(
        [label, label, label, "2024-07-02T14:30", "2024-07-02T14:30"],
        [(NAN, 101.0, 99.0, 100.0, 1.0), VALID, other, (100.0, 101.0, 99.0, 100.0, -1.0), VALID],
    )

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.dropped == (
        dropped(0, label, DropReason.MISSING_VALUE),
        dropped(1, label, DropReason.CONFLICTING_DUPLICATE),
        dropped(2, label, DropReason.CONFLICTING_DUPLICATE),
        dropped(3, "2024-07-02T14:30", DropReason.NEGATIVE_VOLUME),
    )
    assert result.candles.index.tolist() == [pd.Timestamp("2024-07-02T14:30", tz="UTC")]


def test_repeated_rejected_labels_are_reported_once_per_row_with_the_label_reason() -> None:
    after_hours = "2024-07-02T21:00"
    raw = _rows_frame(
        [after_hours, "2024-07-02T13:30", after_hours, after_hours],
        [VALID, VALID, VALID, (NAN, 101.0, 99.0, 100.0, 1.0)],
    )

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.dropped == (
        dropped(0, after_hours, DropReason.OUTSIDE_SESSION),
        dropped(2, after_hours, DropReason.OUTSIDE_SESSION),
        dropped(3, after_hours, DropReason.OUTSIDE_SESSION),
    )
    assert result.candles.index.tolist() == [pd.Timestamp("2024-07-02T13:30", tz="UTC")]


def test_every_calendar_label_kind_is_a_drop_reason() -> None:
    assert {kind.value for kind in CandleLabelErrorKind} <= {reason.value for reason in DropReason}


def test_drop_reasons_are_declared_in_precedence_order() -> None:
    assert [reason.value for reason in DropReason] == [
        "missing_timestamp",
        "outside_calendar",
        "not_a_session",
        "outside_session",
        "off_grid",
        "missing_value",
        "infinite_value",
        "non_positive_price",
        "negative_volume",
        "inconsistent_range",
        "duplicate",
        "conflicting_duplicate",
    ]


def test_normalization_error_kinds_are_declared_in_check_order() -> None:
    assert [kind.value for kind in NormalizationErrorKind] == [
        "index_type",
        "naive_index",
        "multiindex_columns",
        "ambiguous_column",
        "missing_column",
        "non_numeric_column",
    ]


def test_an_unsorted_frame_is_sorted_and_positions_refer_to_the_raw_rows() -> None:
    raw = _rows_frame(
        ["2024-07-02T15:30", "2024-07-02T12:00", "2024-07-02T13:30", "2024-07-02T14:30"],
        [VALID, VALID, VALID, (NAN, 101.0, 99.0, 100.0, 1.0)],
    )

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.dropped == (
        dropped(1, "2024-07-02T12:00", DropReason.OUTSIDE_SESSION),
        dropped(3, "2024-07-02T14:30", DropReason.MISSING_VALUE),
    )
    assert [label.isoformat() for label in result.candles.index] == [
        "2024-07-02T13:30:00+00:00",
        "2024-07-02T15:30:00+00:00",
    ]


def test_labels_spanning_the_whole_coverage_are_classified() -> None:
    raw = _rows_frame(
        ["2021-01-04T14:30", "2027-12-31T20:30", "2027-12-31T21:30", "2021-01-01T15:30"],
        [VALID, VALID, VALID, VALID],
    )

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.dropped == (
        dropped(2, "2027-12-31T21:30", DropReason.OUTSIDE_SESSION),
        dropped(3, "2021-01-01T15:30", DropReason.NOT_A_SESSION),
    )
    assert [label.isoformat() for label in result.candles.index] == [
        "2021-01-04T14:30:00+00:00",
        "2027-12-31T20:30:00+00:00",
    ]


def test_a_frame_with_only_rejected_labels_gives_an_empty_frame() -> None:
    raw = _rows_frame(
        [None, "2020-06-01T13:30", "2024-07-02T13:30:00.000000001"], [VALID, VALID, VALID]
    )

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert [row.reason for row in result.dropped] == [
        DropReason.MISSING_TIMESTAMP,
        DropReason.OUTSIDE_CALENDAR,
        DropReason.OFF_GRID,
    ]
    assert len(result.candles) == 0
    assert result.candles.index.dtype == CANONICAL_INDEX
