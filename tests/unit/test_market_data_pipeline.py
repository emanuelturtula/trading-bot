"""Tests of the provider request, the fetch window and ``prepare_candles`` (spec 010, T10-T11).

AC12: ``CandleRequest`` and ``MAX_LOOKBACK``; AC14: ``candle_window``; AC15: the steps, the
outcome table and the logging of ``prepare_candles``. Every expectation is a literal from the
spec tables (Design 8.1, 8.3 and 8.4); nothing is recomputed with the code under test.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

import trading_bot.data.pipeline as pipeline_module
import trading_bot.data.provider as provider_module
from tests.fixtures.calendars import NEW_YORK, nyse_test_calendar, toy_calendar, utc
from tests.fixtures.session_candles import provider_shaped, session_candles
from trading_bot.data.errors import (
    CandleNotPublishedError,
    InvalidTickerError,
    InvalidTickerReason,
    NoDataError,
    ProviderDataError,
    UnpublishedReason,
)
from trading_bot.data.pipeline import CandleWindow, candle_window, prepare_candles
from trading_bot.data.provider import MAX_LOOKBACK, CandleRequest
from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.market_calendar.sessions import CalendarRangeError
from trading_bot.domain.timeframe import Timeframe

H1 = Timeframe.H1
H4 = Timeframe.H4
D1 = Timeframe.D1
NYSE = nyse_test_calendar()
PIPELINE_LOGGER = "trading_bot.data.pipeline"
CANONICAL_INDEX = pd.DatetimeTZDtype(unit="us", tz="UTC")


def request(**overrides: object) -> CandleRequest:
    fields: dict[str, object] = {
        "ticker": "AAPL",
        "timeframe": D1,
        "lookback": 2,
        "now": utc("2024-07-05T20:00:30"),
    }
    fields.update(overrides)
    return CandleRequest(**fields)  # type: ignore[arg-type]


def iso_labels(frame: pd.DataFrame) -> list[str]:
    return [label.isoformat() for label in frame.index]


# --- T10: CandleRequest and MAX_LOOKBACK (AC12) ------------------------------------------------


def test_the_provider_module_exports_exactly_the_spec_names() -> None:
    assert provider_module.__all__ == ["MAX_LOOKBACK", "CandleRequest", "MarketDataProvider"]


def test_max_lookback_is_5000() -> None:
    assert MAX_LOOKBACK == 5_000


def test_max_lookback_covers_the_largest_stable_warmup_of_the_catalog_plus_a_crossover() -> None:
    warmups: dict[str, int] = {}
    for name in REGISTRY.names:
        maximums = {param.name: param.maximum for param in REGISTRY.get(name).params}
        warmups[name] = REGISTRY.stable_warmup(name, maximums)

    largest = max(warmups.values())
    assert largest + 1 <= MAX_LOOKBACK
    assert largest + 1 == 2_255
    assert warmups["ema"] == largest
    assert (
        REGISTRY.stable_warmup("macd", {"fast": 100, "slow": 200, "signal": 100}) == warmups["macd"]
    )


def test_candle_request_stores_normalized_values() -> None:
    result = request(
        ticker="  brk-b ", lookback=5_000, now=pd.Timestamp("2024-07-05 16:00:30", tz=NEW_YORK)
    )

    assert result.ticker == "BRK-B"
    assert result.timeframe is D1
    assert result.lookback == 5_000
    assert result.now == datetime(2024, 7, 5, 20, 0, 30, tzinfo=UTC)
    assert type(result.now) is datetime


def test_candle_request_is_frozen_keyword_only_equal_by_value_and_hashable() -> None:
    first = request()
    second = request(ticker="aapl", now=pd.Timestamp("2024-07-05 20:00:30", tz="UTC"))

    assert "__slots__" in CandleRequest.__dict__
    assert all(field.kw_only for field in dataclasses.fields(CandleRequest))
    assert first == second
    assert hash(first) == hash(second)
    assert first != request(lookback=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.lookback = 3  # type: ignore[misc]


def test_candle_request_accepts_the_lookback_bounds() -> None:
    assert request(lookback=1).lookback == 1
    assert request(lookback=MAX_LOOKBACK).lookback == MAX_LOOKBACK


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        pytest.param({"ticker": 42}, TypeError, id="ticker-not-str"),
        pytest.param({"ticker": None}, TypeError, id="ticker-none"),
        pytest.param({"timeframe": "1h"}, TypeError, id="timeframe-code"),
        pytest.param({"lookback": True}, TypeError, id="lookback-bool"),
        pytest.param({"lookback": 2.0}, TypeError, id="lookback-float"),
        pytest.param({"lookback": np.int64(2)}, TypeError, id="lookback-numpy"),
        pytest.param({"lookback": 0}, ValueError, id="lookback-zero"),
        pytest.param({"lookback": -1}, ValueError, id="lookback-negative"),
        pytest.param({"lookback": 5_001}, ValueError, id="lookback-above-max"),
        pytest.param({"now": datetime(2024, 7, 5, 20, 0)}, ValueError, id="now-naive"),
        pytest.param({"now": "2024-07-05T20:00Z"}, TypeError, id="now-text"),
        pytest.param(
            {"now": pd.Timestamp("2024-07-05 20:00:00.000000001", tz="UTC")},
            ValueError,
            id="now-sub-microsecond",
        ),
    ],
)
def test_candle_request_rejects_bad_fields(
    overrides: dict[str, object], error_type: type[Exception]
) -> None:
    with pytest.raises(error_type):
        request(**overrides)


def test_a_malformed_ticker_raises_invalid_ticker_error() -> None:
    with pytest.raises(InvalidTickerError) as caught:
        request(ticker="A B")

    assert caught.value.reason is InvalidTickerReason.MALFORMED
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        pytest.param(
            {"ticker": "A B", "timeframe": "1h"}, InvalidTickerError, id="ticker-before-timeframe"
        ),
        pytest.param({"timeframe": "1h", "lookback": 0}, TypeError, id="timeframe-before-lookback"),
        pytest.param(
            {"lookback": True, "now": datetime(2024, 7, 5)},
            TypeError,
            id="lookback-type-before-now",
        ),
        pytest.param(
            {"lookback": 5_001, "now": "text"}, ValueError, id="lookback-range-before-now"
        ),
    ],
)
def test_candle_request_validates_ticker_timeframe_lookback_then_now(
    overrides: dict[str, object], error_type: type[Exception]
) -> None:
    with pytest.raises(error_type):
        request(**overrides)


# --- T10: candle_window (AC14) -----------------------------------------------------------------


def test_the_pipeline_module_exports_exactly_the_spec_names() -> None:
    assert pipeline_module.__all__ == ["CandleWindow", "candle_window", "prepare_candles"]


@pytest.mark.parametrize(
    ("timeframe", "now", "lookback", "first_label", "first_day", "start", "last_label", "end"),
    [
        (
            H1,
            "2024-07-05T14:30",
            5,
            "2024-07-03T13:30",
            date(2024, 7, 3),
            "2024-07-03T13:30",
            "2024-07-05T13:30",
            "2024-07-05T14:30",
        ),
        (
            D1,
            "2024-07-08T12:00",
            3,
            "2024-07-02T04:00",
            date(2024, 7, 2),
            "2024-07-02T13:30",
            "2024-07-05T04:00",
            "2024-07-05T20:00",
        ),
        (
            H4,
            "2024-03-11T20:00",
            4,
            "2024-03-08T14:30",
            date(2024, 3, 8),
            "2024-03-08T14:30",
            "2024-03-11T17:30",
            "2024-03-11T20:00",
        ),
    ],
)
def test_candle_window_matches_the_spec_rows(
    timeframe: Timeframe,
    now: str,
    lookback: int,
    first_label: str,
    first_day: date,
    start: str,
    last_label: str,
    end: str,
) -> None:
    window = candle_window(
        request(timeframe=timeframe, now=utc(now), lookback=lookback), calendar=NYSE
    )

    assert window.first.label == utc(first_label)
    assert window.first.session_day == first_day
    assert window.start == utc(start)
    assert window.last.label == utc(last_label)
    assert window.end == utc(end)
    assert window.last.close_time <= utc(now)


def test_candle_window_is_the_first_and_last_of_the_closed_slots() -> None:
    candle_request = request(timeframe=H1, now=utc("2024-07-05T14:30"), lookback=5)
    slots = NYSE.closed_candles(H1, utc("2024-07-05T14:30"), 5)

    assert candle_window(candle_request, calendar=NYSE) == CandleWindow(
        first=slots[0], last=slots[-1]
    )


def test_candle_window_is_a_frozen_keyword_only_value() -> None:
    window = candle_window(request(), calendar=NYSE)

    assert "__slots__" in CandleWindow.__dict__
    assert all(field.kw_only for field in dataclasses.fields(CandleWindow))
    assert window == candle_window(request(), calendar=NYSE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        window.first = window.last  # type: ignore[misc]


@pytest.mark.parametrize(
    ("candle_request", "calendar"),
    [
        pytest.param(request(lookback=MAX_LOOKBACK), toy_calendar(), id="history-too-short"),
        pytest.param(request(now=utc("2030-01-01T00:00")), NYSE, id="now-after-coverage"),
    ],
)
def test_candle_window_propagates_calendar_range_error(
    candle_request: CandleRequest, calendar: object
) -> None:
    with pytest.raises(CalendarRangeError):
        candle_window(candle_request, calendar=calendar)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("candle_request", "calendar"),
    [
        pytest.param({"ticker": "AAPL"}, NYSE, id="request-mapping"),
        pytest.param(request(), None, id="calendar-none"),
    ],
)
def test_candle_window_and_prepare_candles_reject_wrong_argument_types(
    candle_request: object, calendar: object
) -> None:
    with pytest.raises(TypeError):
        candle_window(candle_request, calendar=calendar)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        prepare_candles(outcome_grid(), candle_request, calendar=calendar)  # type: ignore[arg-type]


# --- T11: prepare_candles outcome table (AC15) -------------------------------------------------


def outcome_grid() -> pd.DataFrame:
    """Design 8.3 grid: ``1d`` labels in ``[2024-06-03T00:00Z, 2024-07-10T00:00Z)``."""
    return session_candles(NYSE, D1, utc("2024-06-03T00:00"), utc("2024-07-10T00:00"))


JULY_5 = pd.Timestamp("2024-07-05T04:00", tz="UTC")


def _without_july_5() -> pd.DataFrame:
    grid = outcome_grid()
    return grid.drop(index=[JULY_5])


def _july_5_close_nan() -> pd.DataFrame:
    grid = outcome_grid()
    grid.loc[JULY_5, "close"] = math.nan
    return grid


def _second_july_5_row(*, identical: bool) -> pd.DataFrame:
    grid = outcome_grid()
    copy = grid.loc[[JULY_5]].copy()
    if not identical:
        low, close = copy.loc[JULY_5, "low"], copy.loc[JULY_5, "close"]
        copy.loc[JULY_5, "close"] = low if low != close else copy.loc[JULY_5, "high"]
    return pd.concat([grid, copy])


def _only_july_8_and_9() -> pd.DataFrame:
    grid = outcome_grid()
    return grid.loc[grid.index >= pd.Timestamp("2024-07-08", tz="UTC")]


def _only_july_5_with_close_nan() -> pd.DataFrame:
    return _july_5_close_nan().loc[[JULY_5]]


def test_the_outcome_grid_has_the_expected_rows() -> None:
    grid = outcome_grid()

    assert len(grid) == 25
    assert iso_labels(grid)[-3:] == [
        "2024-07-05T04:00:00+00:00",
        "2024-07-08T04:00:00+00:00",
        "2024-07-09T04:00:00+00:00",
    ]
    assert len(_only_july_8_and_9()) == 2


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(outcome_grid, id="grid"),
        pytest.param(lambda: _second_july_5_row(identical=True), id="identical-copy"),
    ],
)
def test_the_last_lookback_closed_candles_are_returned(build: object) -> None:
    result = prepare_candles(build(), request(), calendar=NYSE)  # type: ignore[operator]

    assert iso_labels(result) == ["2024-07-03T04:00:00+00:00", "2024-07-05T04:00:00+00:00"]
    assert result.index.dtype == CANONICAL_INDEX


@pytest.mark.parametrize(
    ("build", "reason", "last_label"),
    [
        pytest.param(_without_july_5, UnpublishedReason.MISSING, "2024-07-03T04:00", id="missing"),
        pytest.param(
            _july_5_close_nan, UnpublishedReason.INVALID, "2024-07-03T04:00", id="close-nan"
        ),
        pytest.param(
            lambda: _second_july_5_row(identical=False),
            UnpublishedReason.INVALID,
            "2024-07-03T04:00",
            id="conflicting-copy",
        ),
        pytest.param(
            _only_july_5_with_close_nan, UnpublishedReason.INVALID, None, id="only-invalid-row"
        ),
    ],
)
def test_an_unpublished_last_candle_raises_candle_not_published_error(
    build: object, reason: UnpublishedReason, last_label: str | None
) -> None:
    with pytest.raises(CandleNotPublishedError) as caught:
        prepare_candles(build(), request(), calendar=NYSE)  # type: ignore[operator]

    error = caught.value
    assert error.reason is reason
    assert error.expected_label == utc("2024-07-05T04:00")
    assert error.last_label == (None if last_label is None else utc(last_label))
    assert error.ticker == "AAPL"
    assert error.timeframe is D1
    assert error.retryable is True


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_only_july_8_and_9, id="only-later-rows"),
        pytest.param(lambda: outcome_grid().iloc[:0], id="zero-rows"),
    ],
)
def test_nothing_closed_and_nothing_dropped_at_the_label_raises_no_data_error(
    build: object,
) -> None:
    with pytest.raises(NoDataError) as caught:
        prepare_candles(build(), request(), calendar=NYSE)  # type: ignore[operator]

    assert str(caught.value) == (
        "no valid candle closed by 2024-07-05T20:00:30+00:00 [ticker=AAPL, timeframe=1d]"
    )
    assert caught.value.ticker == "AAPL"
    assert caught.value.timeframe is D1


def test_a_structural_error_becomes_provider_data_error_from_none() -> None:
    raw = outcome_grid().reset_index(drop=True)

    with pytest.raises(ProviderDataError) as caught:
        prepare_candles(raw, request(), calendar=NYSE)

    error = caught.value
    assert error.kind == "index_type"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert error.ticker == "AAPL"
    assert error.timeframe is D1
    assert str(error) == (
        "index_type: the provider response cannot be normalized [ticker=AAPL, timeframe=1d]"
    )


def test_a_column_error_names_the_canonical_column_only() -> None:
    raw = provider_shaped(outcome_grid()).drop(columns=["Volume"])
    raw["secret-label-7f3a"] = 1.0

    with pytest.raises(ProviderDataError) as caught:
        prepare_candles(raw, request(), calendar=NYSE)

    assert caught.value.kind == "missing_column"
    assert str(caught.value) == (
        "missing_column: the provider response cannot be normalized (column volume) "
        "[ticker=AAPL, timeframe=1d]"
    )


def test_a_lookback_above_the_available_history_returns_every_closed_candle() -> None:
    result = prepare_candles(outcome_grid(), request(lookback=5_000), calendar=NYSE)

    assert len(result) == 23
    assert iso_labels(result)[0] == "2024-06-03T04:00:00+00:00"
    assert iso_labels(result)[-1] == "2024-07-05T04:00:00+00:00"


def test_a_structural_error_wins_over_a_now_outside_the_calendar() -> None:
    raw = outcome_grid().reset_index(drop=True)

    with pytest.raises(ProviderDataError):
        prepare_candles(raw, request(now=utc("2030-01-01T00:00")), calendar=NYSE)


def test_a_now_outside_the_calendar_propagates_calendar_range_error() -> None:
    with pytest.raises(CalendarRangeError):
        prepare_candles(outcome_grid(), request(now=utc("2030-01-01T00:00")), calendar=NYSE)


def test_a_non_frame_raw_propagates_type_error() -> None:
    with pytest.raises(TypeError):
        prepare_candles([1, 2, 3], request(), calendar=NYSE)  # type: ignore[arg-type]


def test_raw_is_never_modified_and_equal_calls_return_equal_frames() -> None:
    raw = provider_shaped(outcome_grid())
    before = raw.copy(deep=True)

    first = prepare_candles(raw, request(lookback=10), calendar=NYSE)
    second = prepare_candles(raw, request(lookback=10), calendar=NYSE)

    pd.testing.assert_frame_equal(first, second, check_exact=True)
    pd.testing.assert_frame_equal(raw, before, check_exact=True)
    assert len(first) == 10


# --- T11: logging (AC15, Design 8.4) -----------------------------------------------------------

GOLDEN_ROWS: tuple[tuple[str | None, dict[str, float]], ...] = (
    ("2024-07-02 09:30", {}),
    ("2024-07-02 10:30", {"Close": math.nan}),
    ("2024-07-02 11:30", {}),
    ("2024-07-02 12:30", {"High": 98.0}),
    ("2024-07-02 13:30", {}),
    ("2024-07-02 13:30", {}),
    ("2024-07-02 14:30", {}),
    ("2024-07-02 14:30", {"Close": 100.25}),
    ("2024-07-02 15:30", {"Volume": -1}),
    ("2024-07-02 16:00", {}),
    ("2024-07-02 10:00", {}),
    ("2024-07-03 08:00", {}),
    ("2024-07-03 09:30", {"Open": 0.0}),
    ("2024-07-03 10:30", {"High": math.inf}),
    ("2024-07-03 11:30", {}),
    ("2024-07-03 12:30", {}),
    ("2024-07-04 10:30", {}),
    (None, {}),
    ("2020-12-31 10:30", {}),
    ("2024-07-02 11:00", {"Close": math.nan}),
    ("2024-07-02 09:30", {"Close": math.nan}),
)


def golden_raw() -> pd.DataFrame:
    """The golden raw frame of spec 010, Design 3.4 (the same table as the normalization tests)."""
    valid = {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1000.0}
    rows = [valid | changes for _, changes in GOLDEN_ROWS]
    index = pd.DatetimeIndex([label for label, _ in GOLDEN_ROWS], tz="America/New_York")
    closes = [row["Close"] for row in rows]
    return pd.DataFrame(
        {
            "Open": [row["Open"] for row in rows],
            "High": [row["High"] for row in rows],
            "Low": [row["Low"] for row in rows],
            "Close": closes,
            "Adj Close": closes,
            "Volume": np.asarray([row["Volume"] for row in rows], dtype=np.int64),
            "Dividends": 0.0,
            "Stock Splits": 0.0,
        },
        index=index.as_unit("s").rename("Datetime"),
    )


GOLDEN_REQUEST = CandleRequest(
    ticker="AAPL", timeframe=H1, lookback=3, now=utc("2024-07-03T17:00:30")
)
GOLDEN_RECORDS = [
    ("WARNING", "dropped 1 missing_timestamp candle rows for AAPL 1h (first none, last none)"),
    (
        "WARNING",
        "dropped 1 outside_calendar candle rows for AAPL 1h (first 2020-12-31T15:30:00+00:00, "
        "last 2020-12-31T15:30:00+00:00)",
    ),
    (
        "DEBUG",
        "dropped 1 not_a_session candle rows for AAPL 1h (first 2024-07-04T14:30:00+00:00, "
        "last 2024-07-04T14:30:00+00:00)",
    ),
    (
        "WARNING",
        "dropped 2 outside_session candle rows for AAPL 1h (first 2024-07-02T20:00:00+00:00, "
        "last 2024-07-03T12:00:00+00:00)",
    ),
    (
        "WARNING",
        "dropped 2 off_grid candle rows for AAPL 1h (first 2024-07-02T14:00:00+00:00, "
        "last 2024-07-02T15:00:00+00:00)",
    ),
    (
        "WARNING",
        "dropped 2 missing_value candle rows for AAPL 1h (first 2024-07-02T13:30:00+00:00, "
        "last 2024-07-02T14:30:00+00:00)",
    ),
    (
        "WARNING",
        "dropped 1 infinite_value candle rows for AAPL 1h (first 2024-07-03T14:30:00+00:00, "
        "last 2024-07-03T14:30:00+00:00)",
    ),
    (
        "WARNING",
        "dropped 1 non_positive_price candle rows for AAPL 1h (first 2024-07-03T13:30:00+00:00, "
        "last 2024-07-03T13:30:00+00:00)",
    ),
    (
        "WARNING",
        "dropped 1 negative_volume candle rows for AAPL 1h (first 2024-07-02T19:30:00+00:00, "
        "last 2024-07-02T19:30:00+00:00)",
    ),
    (
        "WARNING",
        "dropped 1 inconsistent_range candle rows for AAPL 1h (first 2024-07-02T16:30:00+00:00, "
        "last 2024-07-02T16:30:00+00:00)",
    ),
    (
        "DEBUG",
        "dropped 1 duplicate candle rows for AAPL 1h (first 2024-07-02T17:30:00+00:00, "
        "last 2024-07-02T17:30:00+00:00)",
    ),
    (
        "WARNING",
        "dropped 2 conflicting_duplicate candle rows for AAPL 1h (first 2024-07-02T18:30:00+00:00, "
        "last 2024-07-02T18:30:00+00:00)",
    ),
]


def test_the_golden_frame_returns_the_last_three_closed_candles(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=PIPELINE_LOGGER)

    result = prepare_candles(golden_raw(), GOLDEN_REQUEST, calendar=NYSE)

    assert iso_labels(result) == [
        "2024-07-02T17:30:00+00:00",
        "2024-07-03T15:30:00+00:00",
        "2024-07-03T16:30:00+00:00",
    ]


def test_the_golden_frame_logs_one_record_per_reason_and_level_literally(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=PIPELINE_LOGGER)

    prepare_candles(golden_raw(), GOLDEN_REQUEST, calendar=NYSE)

    records = [record for record in caplog.records if record.name == PIPELINE_LOGGER]
    assert [(record.levelname, record.getMessage()) for record in records] == GOLDEN_RECORDS
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and record.name != PIPELINE_LOGGER
    ]


class _RecordList(logging.Handler):
    """Keeps the records it receives untouched, before any other handler can rewrite them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_records_use_percent_style_arguments_with_plain_values() -> None:
    # A non-propagating injected logger: root handlers (for example the redaction filter of
    # logging_setup, which formats and clears record arguments) never see these records.
    logger = logging.getLogger("tests.market_data.percent_style")
    handler = _RecordList()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        prepare_candles(golden_raw(), GOLDEN_REQUEST, calendar=NYSE, logger=logger)
    finally:
        logger.removeHandler(handler)

    assert len(handler.records) == 12
    assert {record.msg for record in handler.records} == {
        "dropped %d %s candle rows for %s %s (first %s, last %s)"
    }
    assert handler.records[0].args == (1, "missing_timestamp", "AAPL", "1h", "none", "none")
    for record in handler.records:
        assert isinstance(record.args, tuple)
        assert [type(argument) for argument in record.args] == [int, str, str, str, str, str]


def test_an_injected_logger_receives_the_records(caplog: pytest.LogCaptureFixture) -> None:
    injected = logging.getLogger("tests.market_data.injected")
    caplog.set_level(logging.DEBUG, logger=injected.name)
    caplog.set_level(logging.DEBUG, logger=PIPELINE_LOGGER)

    prepare_candles(golden_raw(), GOLDEN_REQUEST, calendar=NYSE, logger=injected)

    names = {record.name for record in caplog.records}
    assert injected.name in names
    assert PIPELINE_LOGGER not in names
    assert len([record for record in caplog.records if record.name == injected.name]) == 12


def test_a_reason_with_rows_before_and_after_the_last_closed_slot_logs_warning_then_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=PIPELINE_LOGGER)
    grid = session_candles(NYSE, H1, utc("2024-07-02T00:00"), utc("2024-07-03T00:00"))
    after_hours = pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 1000.0]] * 3,
        index=pd.DatetimeIndex(
            ["2024-07-02T22:00", "2024-07-02T12:00", "2024-07-03T21:00"], tz="UTC"
        ).as_unit("us"),
        columns=grid.columns,
    )
    raw = pd.concat([grid, after_hours])
    candle_request = CandleRequest(
        ticker="SPY", timeframe=H1, lookback=7, now=utc("2024-07-02T20:00")
    )

    result = prepare_candles(raw, candle_request, calendar=NYSE)

    assert len(result) == 7
    records = [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == PIPELINE_LOGGER
    ]
    assert records == [
        (
            "WARNING",
            "dropped 1 outside_session candle rows for SPY 1h (first 2024-07-02T12:00:00+00:00, "
            "last 2024-07-02T12:00:00+00:00)",
        ),
        (
            "DEBUG",
            "dropped 2 outside_session candle rows for SPY 1h (first 2024-07-02T22:00:00+00:00, "
            "last 2024-07-03T21:00:00+00:00)",
        ),
    ]


def test_a_clean_frame_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=PIPELINE_LOGGER)

    prepare_candles(provider_shaped(outcome_grid()), request(), calendar=NYSE)

    assert [record for record in caplog.records if record.name == PIPELINE_LOGGER] == []


def test_duplicates_before_the_last_closed_slot_log_at_debug_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=PIPELINE_LOGGER)

    prepare_candles(_second_july_5_row(identical=True), request(), calendar=NYSE)

    records = [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == PIPELINE_LOGGER
    ]
    assert records == [
        (
            "DEBUG",
            "dropped 1 duplicate candle rows for AAPL 1d (first 2024-07-05T04:00:00+00:00, "
            "last 2024-07-05T04:00:00+00:00)",
        )
    ]


def test_dropped_rows_are_logged_before_an_unpublished_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=PIPELINE_LOGGER)

    with pytest.raises(CandleNotPublishedError):
        prepare_candles(_july_5_close_nan(), request(), calendar=NYSE)

    assert [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == PIPELINE_LOGGER
    ] == [
        (
            "WARNING",
            "dropped 1 missing_value candle rows for AAPL 1d (first 2024-07-05T04:00:00+00:00, "
            "last 2024-07-05T04:00:00+00:00)",
        )
    ]
