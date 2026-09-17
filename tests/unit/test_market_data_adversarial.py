"""Adversarial arguments and edges of the market data pipeline (spec 010, T16).

``test_candle_normalization.py``, ``test_closed_candles.py``, ``test_market_data_errors.py``,
``test_tickers.py``, ``test_market_data_pipeline.py`` and ``test_fake_market_data_provider.py``
are the developer's TDD tests; this file re-probes the same surface from the outside with inputs
the Test plan specifically calls out for the tester: DST fold and spring-gap labels given in ET,
microsecond boundaries around every edge of the 2024-07-03 half day, a 100 000-row raw frame with
one defect in its last row, a volume value that loses precision as ``float64``, subnormal prices,
adversarial column labels, a bool ``lookback``, ``now`` as a ``pd.Timestamp`` in a fixed-offset
zone, a fake provider with 1 000 scripted failures, and normalization under warnings-as-errors.
No network, no wall clock: every instant is a literal or built from one, and the NYSE test
calendar is built once per process.
"""

from __future__ import annotations

import asyncio
import logging
import math
import warnings
from datetime import date, timedelta, timezone
from datetime import time as dtime
from functools import cache

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.calendars import NEW_YORK, nyse_test_calendar, toy_calendar, utc, wall_session
from tests.fixtures.fake_provider import FakeMarketDataProvider
from tests.fixtures.session_candles import provider_shaped, session_candles
from trading_bot.data.errors import (
    CandleNotPublishedError,
    ProviderFailure,
    ProviderUnavailableError,
)
from trading_bot.data.pipeline import prepare_candles
from trading_bot.data.provider import CandleRequest
from trading_bot.domain.candle_normalization import (
    CandleNormalizationError,
    DropReason,
    normalize_candles,
)
from trading_bot.domain.candles import OHLCV_COLUMNS
from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.domain.timeframe import Timeframe

H1 = Timeframe.H1
H4 = Timeframe.H4
D1 = Timeframe.D1
NYSE = nyse_test_calendar()
CANONICAL_INDEX = pd.DatetimeTZDtype(unit="us", tz="UTC")
VALID: dict[str, float] = {
    "open": 100.0,
    "high": 101.0,
    "low": 99.0,
    "close": 100.5,
    "volume": 1000.0,
}
NOW = utc("2024-07-05T20:00:30")
PIPELINE_LOGGER = "trading_bot.data.pipeline"


def _rows_frame(labels: pd.DatetimeIndex, rows: list[dict[str, float]]) -> pd.DataFrame:
    values = np.asarray([[row[name] for name in OHLCV_COLUMNS] for row in rows], dtype=np.float64)
    return pd.DataFrame(values, index=labels, columns=list(OHLCV_COLUMNS))


# --- DST fold and spring-gap labels given in ET (AC2, AC4) --------------------------------------


def test_fall_back_fold_and_spring_gap_labels_are_rejected_as_not_a_session() -> None:
    # 2024-11-03: clocks fall back from 02:00 EDT to 01:00 EST, so 01:30 ET names two instants.
    fold = pd.DatetimeIndex(
        ["2024-11-03 01:30", "2024-11-03 01:30"], tz=NEW_YORK, ambiguous=[True, False]
    ).as_unit("s")
    # 2024-03-10: clocks spring forward from 02:00 to 03:00, so 02:30 ET names no instant.
    gap = (
        pd.DatetimeIndex(["2024-03-10 02:30"])
        .tz_localize(NEW_YORK, nonexistent="shift_forward")
        .as_unit("s")
    )
    index = fold.append(gap)
    assert index.is_unique  # the two fold instants are an hour apart in UTC
    raw = _rows_frame(index, [VALID, VALID, VALID])

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert [row.reason for row in result.dropped] == [DropReason.NOT_A_SESSION] * 3
    assert len(result.candles) == 0


# --- Labels exactly at the calendar coverage edges (AC2, AC4) -----------------------------------


@pytest.mark.parametrize(
    ("timeframe", "valid_label"),
    [
        pytest.param(H1, "2024-03-07T14:30", id="1h"),
        pytest.param(H4, "2024-03-07T14:30", id="4h"),
        pytest.param(D1, "2024-03-07T05:00", id="1d"),
    ],
)
def test_labels_exactly_at_the_coverage_edges_are_dropped_not_raised(
    timeframe: Timeframe, valid_label: str
) -> None:
    """A label exactly at ``coverage_end`` is outside the calendar (half-open, D19), not a grid
    label to classify further.

    ``coverage_end`` is the smallest label the calendar never covers, so a label there must never
    reach ``calendar.candle_slots``: asking it about an instant past ``coverage_end`` raises
    ``CalendarRangeError`` instead of letting normalization report the row as ``outside_calendar``
    and keep going. One microsecond before ``coverage_start`` is the symmetric lower-edge case.
    """
    calendar = toy_calendar()
    index = pd.DatetimeIndex(
        [
            utc(valid_label),
            calendar.coverage_end,
            calendar.coverage_start - timedelta(microseconds=1),
        ]
    ).as_unit("s")
    raw = _rows_frame(index, [VALID, VALID, VALID])

    result = normalize_candles(raw, timeframe, calendar=calendar)

    assert [row.reason for row in result.dropped] == [
        DropReason.OUTSIDE_CALENDAR,
        DropReason.OUTSIDE_CALENDAR,
    ]
    assert [row.position for row in result.dropped] == [1, 2]
    assert list(result.candles.index) == [utc(valid_label)]


# --- Microsecond boundaries of the 2024-07-03 half day, through normalize_candles (AC2, AC4) ----


def test_half_day_boundaries_at_microsecond_precision_through_normalize_candles() -> None:
    labels = [
        "2024-07-03T13:29:59.999999",  # 1us before the open: pre-market
        "2024-07-03T13:30:00",  # the open: kept
        "2024-07-03T16:29:59.999999",  # 1us before the last (30-minute) slot's open: off_grid
        "2024-07-03T16:30:00",  # the last slot's open: kept
        "2024-07-03T16:59:59.999999",  # 1us before the half-day close: off_grid
        "2024-07-03T17:00:00",  # the half-day close itself: no candle opens there
    ]
    index = pd.DatetimeIndex([utc(label) for label in labels])
    raw = _rows_frame(index, [VALID] * len(labels))

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert [row.reason for row in result.dropped] == [
        DropReason.OUTSIDE_SESSION,
        DropReason.OFF_GRID,
        DropReason.OFF_GRID,
        DropReason.OUTSIDE_SESSION,
    ]
    assert [row.position for row in result.dropped] == [0, 2, 4, 5]
    assert list(result.candles.index) == [utc(labels[1]), utc(labels[3])]


# --- A 100 000-row raw frame with one defect in the last row (AC4, AC15, AC21) -------------------


@cache
def _huge_calendar() -> MarketCalendar:
    """A synthetic calendar of 100 001 consecutive weekday-shaped sessions, built once.

    Real calendars (NYSE, the toy calendar) hold far fewer sessions than this test needs, so a
    dedicated calendar of plain daily sessions gives 100 001 distinct, valid ``1d`` labels without
    duplicates or off-grid rows diluting the single defect this test cares about.
    """
    count = 100_001
    start = date(1800, 1, 1)
    sessions = tuple(
        wall_session(start + timedelta(days=day), dtime(9, 30), dtime(16, 0))
        for day in range(count)
    )
    return MarketCalendar(
        name="HUGE",
        timezone=NEW_YORK,
        first_day=start,
        last_day=start + timedelta(days=count),
        sessions=sessions,
    )


@cache
def _huge_raw_frame() -> pd.DataFrame:
    calendar = _huge_calendar()
    slots = calendar.candle_slots(D1, calendar.coverage_start, calendar.coverage_end)
    index = pd.DatetimeIndex([slot.label for slot in slots], dtype=CANONICAL_INDEX)
    values = np.tile(np.asarray(list(VALID.values()), dtype=np.float64), (len(slots), 1))
    values[-1, OHLCV_COLUMNS.index("close")] = math.nan  # the only defect: the last row
    return pd.DataFrame(values, index=index, columns=list(OHLCV_COLUMNS))


def test_a_100000_row_frame_reports_one_defect_in_the_last_row_once() -> None:
    calendar = _huge_calendar()
    raw = _huge_raw_frame()
    assert len(raw) == 100_001

    result = normalize_candles(raw, D1, calendar=calendar)

    assert len(result.candles) == 100_000
    assert len(result.dropped) == 1
    assert result.dropped[0].position == 100_000
    assert result.dropped[0].reason is DropReason.MISSING_VALUE
    assert result.candles.index[-1] == raw.index[-2]


def test_a_100000_row_frame_logs_the_one_defect_as_a_single_warning_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=PIPELINE_LOGGER)
    calendar = _huge_calendar()
    raw = _huge_raw_frame()
    slots = calendar.candle_slots(D1, calendar.coverage_start, calendar.coverage_end)
    # `now` is the close of the last slot, which is also the defective (NaN close) row, so the
    # fetch is unpublished: the pipeline still logs the single dropped row before raising.
    request = CandleRequest(ticker="AAPL", timeframe=D1, lookback=1, now=slots[-1].close_time)

    with pytest.raises(CandleNotPublishedError):
        prepare_candles(raw, request, calendar=calendar)

    records = [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == PIPELINE_LOGGER
    ]
    assert len(records) == 1
    level, message = records[0]
    assert level == "WARNING"
    assert message.startswith("dropped 1 missing_value candle rows for AAPL 1d")


# --- Volume precision loss and subnormal prices (AC3) --------------------------------------------


def test_a_volume_that_float64_cannot_represent_exactly_is_kept() -> None:
    huge_volume = 2**53 + 1  # the first integer float64 cannot represent exactly
    index = pd.DatetimeIndex([utc("2024-07-02T13:30")])
    row = VALID | {"volume": float(huge_volume)}
    raw = _rows_frame(index, [row])

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.dropped == ()
    assert result.candles["volume"].iloc[0] == float(np.float64(huge_volume))


def test_subnormal_positive_prices_are_kept() -> None:
    subnormal = 5e-320  # positive but far below any normal float64 magnitude
    index = pd.DatetimeIndex([utc("2024-07-02T13:30")])
    row = {
        "open": subnormal,
        "high": subnormal,
        "low": subnormal,
        "close": subnormal,
        "volume": 1.0,
    }
    raw = _rows_frame(index, [row])

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.dropped == ()
    assert (result.candles.iloc[0][["open", "high", "low", "close"]] > 0.0).all()


# --- Adversarial column labels: never echoed, always ignored when extra (AC2, AC3) --------------


def test_extra_columns_with_adversarial_labels_are_ignored() -> None:
    index = pd.DatetimeIndex([utc("2024-07-02T13:30")])
    raw = pd.DataFrame(
        {
            "open": [VALID["open"]],
            "high": [VALID["high"]],
            "low": [VALID["low"]],
            "close": [VALID["close"]],
            "volume": [VALID["volume"]],
            "z" * 10_000: [1.0],
            "column\nwith\nnewlines": [2.0],
            ("tuple", "label"): [3.0],
            42: [4.0],
            None: [5.0],
        },
        index=index,
    )

    result = normalize_candles(raw, H1, calendar=NYSE)

    assert result.dropped == ()
    assert list(result.candles.columns) == list(OHLCV_COLUMNS)
    assert result.candles.iloc[0].tolist() == list(VALID.values())


def test_adversarial_column_labels_never_appear_in_a_structural_error_message() -> None:
    long_label = "s" * 10_000
    index = pd.DatetimeIndex([utc("2024-07-02T13:30")])
    raw = pd.DataFrame(
        {
            "high": [VALID["high"]],
            "low": [VALID["low"]],
            "close": [VALID["close"]],
            "volume": [VALID["volume"]],
            long_label: [1.0],  # never matches "open": the column stays missing
            "secret\ncolumn": [2.0],
        },
        index=index,
    )

    with pytest.raises(CandleNormalizationError) as caught:
        normalize_candles(raw, H1, calendar=NYSE)

    message = str(caught.value)
    assert long_label not in message
    assert "secret" not in message
    assert "\n" not in message
    assert len(message) < 300


# --- A bool lookback through the fake provider (AC12, AC16) --------------------------------------


def test_fake_provider_rejects_a_bool_lookback_before_recording_the_call() -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)

    with pytest.raises(TypeError):
        asyncio.run(provider.fetch_candles("XMPL", D1, True, now=NOW))

    assert provider.fetch_calls == ()


# --- now as a pd.Timestamp in a fixed-offset zone, through prepare_candles (AC15) ---------------


def test_prepare_candles_agrees_for_now_in_any_representation_of_the_same_instant() -> None:
    grid = session_candles(NYSE, D1, utc("2024-06-03T00:00"), utc("2024-07-10T00:00"))
    raw = provider_shaped(grid)
    reference = CandleRequest(ticker="AAPL", timeframe=D1, lookback=3, now=NOW)
    # A fixed offset (Tokyo-like, not America/New_York and not a ZoneInfo) naming the same
    # instant as the stdlib UTC value.
    fixed_offset = pd.Timestamp(NOW).tz_convert(timezone(timedelta(hours=9)))
    assert fixed_offset.tzinfo != NOW.tzinfo
    offset_request = CandleRequest(ticker="AAPL", timeframe=D1, lookback=3, now=fixed_offset)

    result_utc = prepare_candles(raw, reference, calendar=NYSE)
    result_offset = prepare_candles(raw, offset_request, calendar=NYSE)

    pd.testing.assert_frame_equal(result_utc, result_offset, check_exact=True)
    assert offset_request.now == reference.now
    assert type(offset_request.now) is type(reference.now)


# --- The fake provider with 1 000 scripted failures (AC16) --------------------------------------


def test_fake_provider_serves_one_thousand_scripted_failures_fifo_then_the_frame() -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)
    grid = session_candles(NYSE, D1, utc("2024-06-03T00:00"), utc("2024-07-10T00:00"))
    provider.set_candles("XMPL", D1, provider_shaped(grid))
    error = ProviderUnavailableError(ProviderFailure.CONNECTION, ticker="XMPL")
    provider.fail_next(error, ticker="XMPL", times=1_000)

    for _ in range(1_000):
        with pytest.raises(ProviderUnavailableError) as caught:
            asyncio.run(provider.fetch_candles("XMPL", D1, 2, now=NOW))
        assert caught.value is error
    result = asyncio.run(provider.fetch_candles("XMPL", D1, 2, now=NOW))

    assert len(result) == 2
    assert len(provider.fetch_calls) == 1_001


# --- Normalization under warnings-as-errors (AC21, "no test asserts elapsed time") --------------


def test_normalization_raises_no_warning_on_nan_inf_and_negative_values() -> None:
    index = pd.DatetimeIndex(
        [utc("2024-07-02T13:30"), utc("2024-07-02T14:30"), utc("2024-07-02T15:30")]
    )
    rows = [
        VALID | {"close": math.nan},
        VALID | {"high": math.inf},
        VALID | {"volume": -1.0, "high": -1.0, "low": -2.0, "open": -1.5, "close": -1.5},
    ]
    raw = _rows_frame(index, rows)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = normalize_candles(raw, H1, calendar=NYSE)

    assert len(result.dropped) == 3
    assert len(result.candles) == 0
