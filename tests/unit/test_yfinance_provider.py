"""Tests of the Yahoo provider (spec 011, T9-T11, AC20-AC27; decisions D48, D51, D56-D58, D65).

T9: ``plan_history`` (Design 10.1). T10: the ``fetch_candles`` flow (Design 10.2), the recorded
outcomes (Design 10.3, after the structural preconditions of Design 13.2) and the capped-history
records (Design 10.6). T11: ``is_unpublished_hour`` and ``publishable_4h`` (Design 10.4), the
``4h`` records (Design 10.5), ``validate_ticker`` (Design 10.7), the error surface through the
real client and transport (AC26), Protocol conformance and the factory (Design 11). Clients are
replay fakes; sleeps and clocks are fake; no test reaches the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
import threading
from collections.abc import Callable, Iterator, Mapping
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import pytest
import requests
import requests.exceptions as requests_exceptions
import yfinance
import yfinance.exceptions as yf_exceptions
from curl_cffi.requests import exceptions as curl_exceptions

import trading_bot.data.yahoo.client as client_module
import trading_bot.data.yahoo.provider as provider_module
from tests.fixtures.calendars import NEW_YORK, nyse_test_calendar, utc, wall_session
from tests.fixtures.network_guard import NetworkAccessError
from tests.fixtures.provider_contract import assert_closed_candles
from tests.fixtures.session_candles import provider_shaped, session_candles
from tests.fixtures.yahoo_recordings import (
    RecordedYahooClient,
    hand_frame,
    load_metadata,
    load_recording,
    yfinance_as_market_data_provider,
)
from trading_bot.data.errors import (
    CandleNotPublishedError,
    InvalidTickerError,
    InvalidTickerReason,
    MarketDataError,
    NoDataError,
    ProviderDataError,
    ProviderFailure,
    ProviderUnavailableError,
    UnpublishedReason,
)
from trading_bot.data.pipeline import prepare_candles
from trading_bot.data.provider import CandleRequest
from trading_bot.data.tickers import AssetType, Exchange, TickerInfo
from trading_bot.data.transport import ProviderTransport, RateLimit, RetryPolicy, TokenBucket
from trading_bot.data.yahoo.factory import build_yfinance_provider
from trading_bot.data.yahoo.history import HistoryQuery, YahooInterval
from trading_bot.data.yahoo.provider import (
    INTRADAY_HISTORY,
    HistoryPlan,
    YFinanceProvider,
    is_unpublished_hour,
    plan_history,
    publishable_4h,
)
from trading_bot.domain.candle_normalization import normalize_candles
from trading_bot.domain.candle_resampling import resample_hourly_to_4h
from trading_bot.domain.market_calendar.sessions import CalendarRangeError, MarketCalendar
from trading_bot.domain.timeframe import Timeframe

NYSE = nyse_test_calendar()
H1 = Timeframe.H1
H4 = Timeframe.H4
D1 = Timeframe.D1
PROVIDER_LOGGER = "trading_bot.data.yahoo.provider"
PIPELINE_LOGGER = "trading_bot.data.pipeline"
URL = "https://" + "query.example.invalid" + "/v8/finance/chart/SPY?crumb=" + "c" * 11

FETCH_ERRORS = (
    TypeError,
    ValueError,
    InvalidTickerError,
    NoDataError,
    CandleNotPublishedError,
    ProviderDataError,
    ProviderUnavailableError,
    CalendarRangeError,
)
VALIDATE_ERRORS = (TypeError, InvalidTickerError, ProviderDataError, ProviderUnavailableError)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def make_transport(sleep: RecordingSleep | None = None) -> ProviderTransport:
    clock = FakeClock()
    return ProviderTransport(
        policy=RetryPolicy(),
        limiter=TokenBucket(RateLimit(), clock=clock, sleep=clock.sleep),
        sleep=sleep or RecordingSleep(),
        jitter=lambda: 0.5,
    )


def make_provider(
    client: RecordedYahooClient, *, sleep: RecordingSleep | None = None
) -> YFinanceProvider:
    return YFinanceProvider(client=client, transport=make_transport(sleep), calendar=NYSE)


def recorded_client(name: str) -> RecordedYahooClient:
    recording = load_recording(name)
    return RecordedYahooClient(histories={(recording.symbol, recording.interval): recording.frame})


def fetch(
    provider: YFinanceProvider, ticker: str, timeframe: Timeframe, lookback: int, now: datetime
) -> pd.DataFrame:
    return asyncio.run(provider.fetch_candles(ticker, timeframe, lookback, now=now))


def iso(frame: pd.DataFrame) -> list[str]:
    return [pd.Timestamp(label).strftime("%Y-%m-%dT%H:%MZ") for label in frame.index]


def provider_records(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == PROVIDER_LOGGER
    ]


# --- T9: plan_history (Design 10.1) -----------------------------------------------------------

PLAN_TABLE: tuple[tuple[Timeframe, str, int, YahooInterval, str, str, bool], ...] = (
    (H1, "2025-11-28T18:00:30", 5, "1h", "2025-11-26T20:30", "2025-11-26T20:30", False),
    (H4, "2025-11-28T18:00:30", 5, "1h", "2025-11-25T14:30", "2025-11-25T14:30", False),
    (D1, "2025-12-02T21:00:30", 5, "1d", "2025-11-25T05:00", "2025-11-25T05:00", False),
    (D1, "2026-09-17T15:00:00", 1000, "1d", "2022-09-21T04:00", "2022-09-21T04:00", False),
    (H1, "2026-09-17T15:00:00", 3435, "1h", "2024-09-27T15:30", "2024-09-27T15:30", False),
    (H1, "2026-09-17T15:00:00", 3436, "1h", "2024-09-27T14:30", "2024-09-27T15:30", True),
    (H1, "2026-09-17T15:00:00", 5000, "1h", "2023-11-03T19:30", "2024-09-27T15:30", True),
    (H4, "2026-09-17T15:00:00", 980, "1h", "2024-09-27T17:30", "2024-09-27T17:30", False),
    (H4, "2026-09-17T15:00:00", 981, "1h", "2024-09-27T13:30", "2024-09-27T17:30", True),
)


def test_the_module_exports_exactly_the_spec_names() -> None:
    assert provider_module.__all__ == [
        "INTRADAY_HISTORY",
        "HistoryPlan",
        "YFinanceProvider",
        "is_unpublished_hour",
        "plan_history",
        "publishable_4h",
    ]
    assert timedelta(days=720) == INTRADAY_HISTORY


@pytest.mark.parametrize(
    ("timeframe", "now", "lookback", "interval", "requested", "start", "capped"), PLAN_TABLE
)
def test_plan_history_gives_the_spec_table(
    timeframe: Timeframe,
    now: str,
    lookback: int,
    interval: YahooInterval,
    requested: str,
    start: str,
    capped: bool,
) -> None:
    request = CandleRequest(ticker="SPY", timeframe=timeframe, lookback=lookback, now=utc(now))

    plan = plan_history(request, calendar=NYSE)

    assert plan == HistoryPlan(
        interval=interval, start=utc(start), requested_start=utc(requested), capped=capped
    )


def test_the_intraday_cap_is_720_days_before_now() -> None:
    now = utc("2026-09-17T15:00:00")

    assert now - INTRADAY_HISTORY == utc("2024-09-27T15:00:00")


def test_a_window_before_the_calendar_raises_calendar_range_error() -> None:
    request = CandleRequest(ticker="SPY", timeframe=H4, lookback=5000, now=utc("2026-09-17T15:00"))

    with pytest.raises(CalendarRangeError):
        plan_history(request, calendar=NYSE)


def test_plan_history_rejects_arguments_of_the_wrong_type() -> None:
    request = CandleRequest(ticker="SPY", timeframe=H1, lookback=5, now=utc("2025-11-28T18:00:30"))

    with pytest.raises(TypeError):
        plan_history("SPY", calendar=NYSE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        plan_history(request, calendar="NYSE")  # type: ignore[arg-type]


def test_history_plan_is_frozen_and_keyword_only() -> None:
    plan = HistoryPlan(
        interval="1d",
        start=utc("2025-11-25T05:00"),
        requested_start=utc("2025-11-25T05:00"),
        capped=False,
    )

    with pytest.raises(AttributeError):
        plan.capped = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        HistoryPlan("1d", utc("2025-11-25T05:00"), utc("2025-11-25T05:00"), False)  # type: ignore[misc]


# --- T10: the fetch flow (Design 10.2) --------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ((42, H1, 5, utc("2025-11-26T21:00:30")), TypeError),
        (("SPY|X", H1, 5, utc("2025-11-26T21:00:30")), InvalidTickerError),
        (("SPY", "1h", 5, utc("2025-11-26T21:00:30")), TypeError),
        (("SPY", H1, 0, utc("2025-11-26T21:00:30")), ValueError),
        (("SPY", H1, True, utc("2025-11-26T21:00:30")), TypeError),
        (("SPY", H1, 5, datetime(2025, 11, 26, 21, 0, 30)), ValueError),
        (("M&M.NS", H1, 5, utc("2025-11-26T21:00:30")), InvalidTickerError),
        (("US0378331005", H1, 5, utc("2025-11-26T21:00:30")), InvalidTickerError),
        (("SPY", H1, 5, utc("2030-01-01T00:00")), CalendarRangeError),
        (("SPY", H4, 5000, utc("2026-09-17T15:00")), CalendarRangeError),
    ],
)
def test_argument_symbol_and_calendar_errors_are_raised_before_any_client_call(
    arguments: tuple[object, object, object, datetime], error: type[Exception]
) -> None:
    client = recorded_client("spy_1h_2025-11-24")
    provider = make_provider(client)
    ticker, timeframe, lookback, now = arguments

    with pytest.raises(error):
        asyncio.run(provider.fetch_candles(ticker, timeframe, lookback, now=now))  # type: ignore[arg-type]

    assert client.queries == ()


def test_the_malformed_and_isin_reasons_come_from_the_symbol_check() -> None:
    # One provider per event loop (D68).
    with pytest.raises(InvalidTickerError) as malformed:
        fetch(
            make_provider(recorded_client("spy_1h_2025-11-24")),
            "M&M.NS",
            H1,
            5,
            utc("2025-11-26T21:00:30"),
        )
    with pytest.raises(InvalidTickerError) as isin:
        fetch(
            make_provider(recorded_client("spy_1h_2025-11-24")),
            "US0378331005",
            H1,
            5,
            utc("2025-11-26T21:00:30"),
        )

    assert malformed.value.reason is InvalidTickerReason.MALFORMED
    assert isin.value.reason is InvalidTickerReason.NOT_FOUND


def test_the_client_is_asked_once_with_the_planned_query() -> None:
    client = recorded_client("spy_1h_2025-11-24")
    provider = make_provider(client)

    fetch(provider, " spy ", H1, 7, utc("2025-11-26T21:00:30"))

    assert client.queries == (
        HistoryQuery(symbol="SPY", interval="1h", start=utc("2025-11-26T14:30")),
    )


def test_a_4h_fetch_asks_for_hourly_bars_from_the_planned_start() -> None:
    client = recorded_client("spy_1h_2025-11-24")
    provider = make_provider(client)

    fetch(provider, "SPY", H4, 5, utc("2025-11-28T18:00:30"))

    assert client.queries == (
        HistoryQuery(symbol="SPY", interval="1h", start=utc("2025-11-25T14:30")),
    )


def test_there_is_one_client_call_per_attempt() -> None:
    client = recorded_client("spy_1d_2025-11-24")
    sleep = RecordingSleep()
    provider = make_provider(client, sleep=sleep)
    client.fail_next(ProviderUnavailableError(ProviderFailure.CONNECTION, ticker="SPY"), times=2)

    frame = fetch(provider, "SPY", D1, 2, utc("2025-11-28T17:59:59"))

    assert len(frame) == 2
    assert len(client.queries) == 3
    assert len(set(client.queries)) == 1
    assert sleep.delays == [1.5, 3.0]


@pytest.mark.parametrize(
    ("name", "timeframe", "now", "lookback"),
    [
        ("spy_1h_2025-11-24", H1, "2025-11-26T21:00:30", 7),
        ("spy_1h_2025-11-24", H1, "2025-12-01T15:30:30", 3),
        ("spy_1d_2025-11-24", D1, "2025-11-28T18:00:30", 3),
        ("nvda_1d_2024-06-03", D1, "2024-06-14T20:00:30", 10),
    ],
)
def test_an_hourly_or_daily_result_equals_prepare_candles_on_the_client_frame(
    name: str, timeframe: Timeframe, now: str, lookback: int
) -> None:
    recording = load_recording(name)
    request = CandleRequest(
        ticker=recording.symbol, timeframe=timeframe, lookback=lookback, now=utc(now)
    )

    frame = fetch(
        make_provider(recorded_client(name)), recording.symbol, timeframe, lookback, utc(now)
    )

    pd.testing.assert_frame_equal(
        frame, prepare_candles(recording.frame, request, calendar=NYSE), check_exact=True
    )


@pytest.mark.parametrize(
    ("name", "now", "lookback"),
    [
        ("spy_1h_2025-11-24", "2025-11-28T18:00:30", 5),
        ("aapl_1h_2026-01-28", "2026-02-03T21:00:30", 6),
        ("spy_1h_2025-03-06", "2025-03-10T20:00:30", 4),
    ],
)
def test_a_4h_result_equals_prepare_candles_on_the_publishable_resampled_frame(
    name: str, now: str, lookback: int
) -> None:
    recording = load_recording(name)
    request = CandleRequest(ticker=recording.symbol, timeframe=H4, lookback=lookback, now=utc(now))
    hourly = normalize_candles(recording.frame, H1, calendar=NYSE).candles
    resampled = resample_hourly_to_4h(hourly, utc(now), calendar=NYSE)
    last = NYSE.closed_candles(H4, utc(now), 1)[-1]
    expected = prepare_candles(
        publishable_4h(resampled, last=last, calendar=NYSE), request, calendar=NYSE
    )

    frame = fetch(make_provider(recorded_client(name)), recording.symbol, H4, lookback, utc(now))

    pd.testing.assert_frame_equal(frame, expected, check_exact=True)


def test_the_same_recording_always_gives_the_same_frame() -> None:
    async def scenario() -> tuple[pd.DataFrame, pd.DataFrame]:
        provider = make_provider(recorded_client("aapl_1h_2026-01-28"))  # inside the loop (D68)
        now = utc("2026-02-03T21:00:30")
        return (
            await provider.fetch_candles("AAPL", H4, 6, now=now),
            await provider.fetch_candles("AAPL", H4, 6, now=now),
        )

    first, second = asyncio.run(scenario())

    pd.testing.assert_frame_equal(first, second, check_exact=True)


# --- T10: the post-fetch processing runs off the event loop (AC21, D67) -------------------------


@pytest.mark.parametrize(
    ("name", "timeframe", "now", "lookback"),
    [
        ("spy_1d_2025-11-24", D1, "2025-11-28T17:59:59", 2),
        ("spy_1h_2025-11-24", H1, "2025-11-26T21:00:30", 7),
        ("spy_1h_2025-11-24", H4, "2025-11-28T18:00:30", 5),
    ],
    ids=["1d", "1h", "4h"],
)
def test_the_post_fetch_processing_runs_in_a_worker_thread(
    monkeypatch: pytest.MonkeyPatch, name: str, timeframe: Timeframe, now: str, lookback: int
) -> None:
    prepare_threads: list[int] = []
    normalize_threads: list[int] = []
    real_prepare = provider_module.prepare_candles
    real_normalize = provider_module.normalize_candles

    def prepare_spy(
        frame: pd.DataFrame,
        request: CandleRequest,
        *,
        calendar: object,
        logger: object = None,
    ) -> pd.DataFrame:
        prepare_threads.append(threading.get_ident())
        return real_prepare(frame, request, calendar=calendar)  # type: ignore[arg-type]

    def normalize_spy(
        raw: pd.DataFrame, candle_timeframe: Timeframe, *, calendar: object
    ) -> object:
        normalize_threads.append(threading.get_ident())
        return real_normalize(raw, candle_timeframe, calendar=calendar)  # type: ignore[arg-type]

    monkeypatch.setattr(provider_module, "prepare_candles", prepare_spy)
    monkeypatch.setattr(provider_module, "normalize_candles", normalize_spy)
    symbol = load_recording(name).symbol
    main_thread = threading.get_ident()

    frame = fetch(make_provider(recorded_client(name)), symbol, timeframe, lookback, utc(now))

    assert len(frame) == lookback
    assert prepare_threads == [prepare_threads[0]]
    assert prepare_threads[0] != main_thread
    if timeframe is H4:
        assert normalize_threads == [prepare_threads[0]]  # the same worker does the whole job
    else:
        assert normalize_threads == []


@pytest.mark.parametrize(
    "failure",
    [
        ProviderDataError("invalid_response", "the response cannot be used", ticker="SPY"),
        ProviderUnavailableError(ProviderFailure.INVALID_RESPONSE, ticker="SPY"),
    ],
    ids=["provider_data_error", "retryable_error"],
)
def test_a_processing_error_reaches_the_caller_after_a_single_client_call(
    monkeypatch: pytest.MonkeyPatch, failure: MarketDataError
) -> None:
    def prepare_boom(
        frame: pd.DataFrame,
        request: CandleRequest,
        *,
        calendar: object,
        logger: object = None,
    ) -> pd.DataFrame:
        raise failure

    monkeypatch.setattr(provider_module, "prepare_candles", prepare_boom)
    client = recorded_client("spy_1d_2025-11-24")
    sleep = RecordingSleep()

    with pytest.raises(type(failure)) as caught:
        fetch(make_provider(client, sleep=sleep), "SPY", D1, 2, utc("2025-11-28T17:59:59"))

    assert caught.value is failure
    assert len(client.queries) == 1  # the worker is outside the transport: no attempt is consumed
    assert sleep.delays == []


def test_an_unpublished_candle_error_also_costs_a_single_client_call() -> None:
    client = recorded_client("spy_1h_2025-11-24")
    sleep = RecordingSleep()

    with pytest.raises(CandleNotPublishedError):
        fetch(make_provider(client, sleep=sleep), "SPY", H1, 5, utc("2025-11-28T18:00:30"))

    assert len(client.queries) == 1
    assert sleep.delays == []


def test_a_4h_normalization_error_becomes_a_provider_data_error_from_none() -> None:
    frame = load_recording("spy_1h_2025-11-24").frame
    frame.index = pd.DatetimeIndex(frame.index).tz_localize(None)
    client = RecordedYahooClient(histories={("SPY", "1h"): frame})

    with pytest.raises(ProviderDataError) as caught:
        fetch(make_provider(client), "SPY", H4, 5, utc("2025-11-28T18:00:30"))

    assert caught.value.kind == "naive_index"
    assert caught.value.ticker == "SPY"
    assert caught.value.timeframe is H4
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_a_4h_normalization_error_names_the_column() -> None:
    frame = load_recording("spy_1h_2025-11-24").frame.drop(columns=["Volume"])
    client = RecordedYahooClient(histories={("SPY", "1h"): frame})

    with pytest.raises(ProviderDataError) as caught:
        fetch(make_provider(client), "SPY", H4, 5, utc("2025-11-28T18:00:30"))

    assert caught.value.kind == "missing_column"
    assert "(column volume)" in str(caught.value)


def test_an_empty_hourly_frame_gives_no_data_for_4h() -> None:
    frame = load_recording("spy_1h_2025-11-24").frame.iloc[:0]
    client = RecordedYahooClient(histories={("SPY", "1h"): frame})

    with pytest.raises(NoDataError):
        fetch(make_provider(client), "SPY", H4, 5, utc("2025-11-28T18:00:30"))


# --- T10: recorded outcomes (Design 10.3) --------------------------------------------------------


def assert_structural_preconditions(name: str) -> None:
    """Design 13.2: the recorded structure every outcome depends on."""
    frame = load_recording(name).frame
    days: dict[str, list[str]] = {}
    for label in pd.DatetimeIndex(frame.index):
        days.setdefault(label.strftime("%Y-%m-%d"), []).append(label.strftime("%H:%M"))
    if name == "spy_1h_2025-11-24":
        assert len(frame) == 38
        assert "2025-11-27" not in days
        assert days["2025-11-28"] == ["09:30", "10:30", "11:30"]
    elif name == "spy_1d_2025-11-24":
        assert len(frame) == 6
        assert "2025-11-27" not in days
    elif name == "aapl_1h_2026-01-28":
        assert len(frame) == 33
        assert days["2026-01-30"] == ["09:30", "10:30"]
        assert min(days["2026-02-02"]) == "13:30"
    elif name == "spy_1h_2025-03-06":
        assert len(frame) == 28
        assert days["2025-03-07"][0] == "09:30"
        assert days["2025-03-10"][0] == "09:30"
    else:
        assert name == "nvda_1d_2024-06-03"
        assert len(frame) == 10


OUTCOMES: tuple[tuple[str, Timeframe, str, int, list[str]], ...] = (
    (
        "spy_1h_2025-11-24",
        H1,
        "2025-11-26T21:00:30",
        7,
        [f"2025-11-26T{hour}:30Z" for hour in ("14", "15", "16", "17", "18", "19", "20")],
    ),
    (
        "spy_1h_2025-11-24",
        H1,
        "2025-12-01T15:30:30",
        3,
        ["2025-11-28T15:30Z", "2025-11-28T16:30Z", "2025-12-01T14:30Z"],
    ),
    (
        "spy_1h_2025-11-24",
        H4,
        "2025-11-28T18:00:30",
        5,
        [
            "2025-11-25T14:30Z",
            "2025-11-25T18:30Z",
            "2025-11-26T14:30Z",
            "2025-11-26T18:30Z",
            "2025-11-28T14:30Z",
        ],
    ),
    ("spy_1h_2025-11-24", H4, "2025-12-01T14:30:00", 2, ["2025-11-26T18:30Z", "2025-11-28T14:30Z"]),
    (
        "spy_1h_2025-11-24",
        H4,
        "2025-12-02T21:00:30",
        6,
        [
            "2025-11-26T18:30Z",
            "2025-11-28T14:30Z",
            "2025-12-01T14:30Z",
            "2025-12-01T18:30Z",
            "2025-12-02T14:30Z",
            "2025-12-02T18:30Z",
        ],
    ),
    ("spy_1d_2025-11-24", D1, "2025-11-28T17:59:59", 2, ["2025-11-25T05:00Z", "2025-11-26T05:00Z"]),
    (
        "spy_1d_2025-11-24",
        D1,
        "2025-11-28T18:00:30",
        3,
        ["2025-11-25T05:00Z", "2025-11-26T05:00Z", "2025-11-28T05:00Z"],
    ),
    (
        "aapl_1h_2026-01-28",
        H4,
        "2026-02-03T21:00:30",
        6,
        [
            "2026-01-29T14:30Z",
            "2026-01-29T18:30Z",
            "2026-01-30T14:30Z",
            "2026-02-02T18:30Z",
            "2026-02-03T14:30Z",
            "2026-02-03T18:30Z",
        ],
    ),
    ("spy_1h_2025-03-06", H1, "2025-03-10T14:30:00", 2, ["2025-03-07T20:30Z", "2025-03-10T13:30Z"]),
    (
        "spy_1h_2025-03-06",
        H4,
        "2025-03-10T20:00:30",
        4,
        ["2025-03-07T14:30Z", "2025-03-07T18:30Z", "2025-03-10T13:30Z", "2025-03-10T17:30Z"],
    ),
    (
        "nvda_1d_2024-06-03",
        D1,
        "2024-06-14T20:00:30",
        10,
        [
            f"2024-06-{day}T04:00Z"
            for day in ("03", "04", "05", "06", "07", "10", "11", "12", "13", "14")
        ],
    ),
)
UNPUBLISHED: tuple[tuple[str, Timeframe, str, int, str, str], ...] = (
    ("spy_1h_2025-11-24", H1, "2025-11-28T18:00:30", 5, "2025-11-28T17:30", "2025-11-28T16:30"),
    ("aapl_1h_2026-01-28", H1, "2026-01-30T21:00:30", 3, "2026-01-30T20:30", "2026-01-30T15:30"),
    ("aapl_1h_2026-01-28", H4, "2026-01-30T21:00:30", 3, "2026-01-30T18:30", "2026-01-30T14:30"),
    ("aapl_1h_2026-01-28", H4, "2026-02-02T18:30:30", 3, "2026-02-02T14:30", "2026-01-30T14:30"),
)


@pytest.mark.parametrize(("name", "timeframe", "now", "lookback", "labels"), OUTCOMES)
def test_recorded_fetches_return_the_spec_labels(
    name: str, timeframe: Timeframe, now: str, lookback: int, labels: list[str]
) -> None:
    assert_structural_preconditions(name)
    symbol = load_recording(name).symbol
    request = CandleRequest(ticker=symbol, timeframe=timeframe, lookback=lookback, now=utc(now))

    frame = fetch(make_provider(recorded_client(name)), symbol, timeframe, lookback, utc(now))

    assert iso(frame) == labels
    assert_closed_candles(frame, request, calendar=NYSE)


@pytest.mark.parametrize(("name", "timeframe", "now", "lookback", "expected", "last"), UNPUBLISHED)
def test_recorded_fetches_raise_the_spec_unpublished_errors(
    name: str, timeframe: Timeframe, now: str, lookback: int, expected: str, last: str
) -> None:
    assert_structural_preconditions(name)
    symbol = load_recording(name).symbol

    with pytest.raises(CandleNotPublishedError) as caught:
        fetch(make_provider(recorded_client(name)), symbol, timeframe, lookback, utc(now))

    assert caught.value.reason is UnpublishedReason.MISSING
    assert caught.value.expected_label == utc(expected)
    assert caught.value.last_label == utc(last)
    assert caught.value.ticker == symbol
    assert caught.value.timeframe is timeframe


def test_the_daily_close_is_the_recorded_close_and_not_the_adjusted_close() -> None:
    assert_structural_preconditions("nvda_1d_2024-06-03")
    recording = load_recording("nvda_1d_2024-06-03")

    frame = fetch(
        make_provider(recorded_client("nvda_1d_2024-06-03")),
        "NVDA",
        D1,
        10,
        utc("2024-06-14T20:00:30"),
    )

    assert frame["close"].tolist() == recording.frame["Close"].tolist()
    assert all(
        close != adjusted
        for close, adjusted in zip(frame["close"], recording.frame["Adj Close"], strict=True)
    )


# --- T10: capped history (Design 10.6) --------------------------------------------------------


def recent_hourly_client() -> RecordedYahooClient:
    frame = provider_shaped(
        session_candles(NYSE, H1, utc("2026-09-08T00:00"), utc("2026-09-17T14:00"))
    )
    return RecordedYahooClient(histories={("SPY", "1h"): frame, ("QQQ", "1h"): frame})


def test_capped_fetches_log_info_once_per_symbol_and_timeframe_then_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=PROVIDER_LOGGER)
    now = utc("2026-09-17T15:00:00")

    async def scenario() -> None:
        provider = make_provider(recent_hourly_client())  # both providers live in this loop (D68)
        await provider.fetch_candles("SPY", H1, 5000, now=now)
        await provider.fetch_candles("SPY", H1, 5000, now=now)
        await provider.fetch_candles("SPY", H4, 981, now=now)
        await provider.fetch_candles("QQQ", H1, 5000, now=now)
        await provider.fetch_candles("SPY", H1, 3435, now=now)
        fresh = make_provider(recent_hourly_client())
        await fresh.fetch_candles("SPY", H1, 5000, now=now)

    asyncio.run(scenario())

    spy_1h = (
        "Yahoo history for SPY 1h capped: requested from 2023-11-03T19:30:00+00:00, "
        "available from 2024-09-27T15:30:00+00:00"
    )
    assert provider_records(caplog) == [
        ("INFO", spy_1h),
        ("DEBUG", spy_1h),
        (
            "INFO",
            "Yahoo history for SPY 4h capped: requested from 2024-09-27T13:30:00+00:00, "
            "available from 2024-09-27T17:30:00+00:00",
        ),
        ("INFO", spy_1h.replace("SPY", "QQQ")),
        ("INFO", spy_1h),
    ]


def test_the_capped_record_is_emitted_before_the_call_even_when_the_fetch_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=PROVIDER_LOGGER)
    client = recent_hourly_client()
    client.fail_next(NoDataError("nothing", ticker="SPY"))

    with pytest.raises(NoDataError):
        fetch(make_provider(client), "SPY", H1, 5000, utc("2026-09-17T15:00:00"))

    assert [level for level, _ in provider_records(caplog)] == ["INFO"]
    assert len(client.queries) == 1
    query = client.queries[0]
    assert query.start == utc("2024-09-27T15:30")


# --- T11: is_unpublished_hour and publishable_4h (Design 10.4) ------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("2024-11-29T17:30", True),
        ("2024-07-03T16:30", True),
        ("2024-11-27T20:30", False),
        ("2024-11-29T16:30", False),
    ],
)
def test_is_unpublished_hour_gives_the_spec_table(label: str, expected: bool) -> None:
    assert is_unpublished_hour(NYSE.candle_slot(H1, utc(label)), calendar=NYSE) is expected


@pytest.mark.parametrize(
    ("timeframe", "label"), [(H4, "2024-11-29T14:30"), (D1, "2024-11-29T05:00")]
)
def test_is_unpublished_hour_rejects_other_timeframes(timeframe: Timeframe, label: str) -> None:
    with pytest.raises(ValueError, match="1h"):
        is_unpublished_hour(NYSE.candle_slot(timeframe, utc(label)), calendar=NYSE)


def test_is_unpublished_hour_rejects_arguments_of_the_wrong_type() -> None:
    with pytest.raises(TypeError):
        is_unpublished_hour("2024-11-29T17:30", calendar=NYSE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        is_unpublished_hour(NYSE.candle_slot(H1, utc("2024-11-29T17:30")), calendar="NYSE")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("now", "drop", "rows"),
    [
        ("2024-11-27T18:30:30", None, 1),
        ("2024-11-29T18:00:30", None, 3),
        ("2024-11-29T18:00:30", "2024-11-29T16:30", 2),
        ("2024-12-02T21:00:30", None, 4),
        ("2024-12-03T18:30:30", None, 5),
    ],
)
def test_publishable_4h_gives_the_spec_table(now: str, drop: str | None, rows: int) -> None:
    hourly = hand_frame()
    if drop is not None:
        hourly = hourly.drop(index=pd.Timestamp(utc(drop)))
    resampled = resample_hourly_to_4h(hourly, utc(now), calendar=NYSE)
    last = NYSE.closed_candles(H4, utc(now), 1)[-1]

    frame = publishable_4h(resampled, last=last, calendar=NYSE)

    assert len(frame) == rows
    pd.testing.assert_frame_equal(frame, resampled.candles.iloc[:rows], check_exact=True)


def test_publishable_4h_on_an_empty_resample_returns_it() -> None:
    resampled = resample_hourly_to_4h(
        hand_frame().iloc[:0], utc("2024-12-02T21:00:30"), calendar=NYSE
    )
    last = NYSE.closed_candles(H4, utc("2024-12-02T21:00:30"), 1)[-1]

    assert publishable_4h(resampled, last=last, calendar=NYSE) is resampled.candles


def test_a_slot_whose_only_hour_is_unpublished_has_no_row_and_keeps_the_frame() -> None:
    """Design 10.4: such a slot never reaches step 2, so ``hours[-2]`` needs no guard."""
    calendar = MarketCalendar(
        name="EARLY",
        timezone=NEW_YORK,
        first_day=date(2024, 3, 4),
        last_day=date(2024, 3, 6),
        sessions=(
            wall_session(date(2024, 3, 4), time(9, 30), time(16, 0)),
            wall_session(date(2024, 3, 5), time(9, 30), time(14, 0)),  # closes at 14:00 ET
        ),
    )
    now = utc("2024-03-05T19:00:30")
    last = calendar.closed_candles(H4, now, 1)[-1]
    only_hour = calendar.candle_slots(H1, last.open_time, last.close_time)
    hourly = session_candles(calendar, H1, calendar.coverage_start, calendar.coverage_end)
    hourly = hourly.drop(index=pd.Timestamp(utc("2024-03-05T18:30")))  # Yahoo never publishes it

    resampled = resample_hourly_to_4h(hourly, now, calendar=calendar)

    assert last.label == utc("2024-03-05T18:30")
    assert [slot.label for slot in only_hour] == [utc("2024-03-05T18:30")]
    assert is_unpublished_hour(only_hour[0], calendar=calendar) is True
    assert last not in [entry.slot for entry in resampled.slots]
    assert publishable_4h(resampled, last=last, calendar=calendar) is resampled.candles


def test_publishable_4h_keeps_rows_when_the_last_slot_is_not_the_last_closed_slot() -> None:
    resampled = resample_hourly_to_4h(hand_frame(), utc("2024-12-02T21:00:30"), calendar=NYSE)
    earlier = NYSE.candle_slot(H4, utc("2024-12-02T14:30"))

    assert publishable_4h(resampled, last=earlier, calendar=NYSE) is resampled.candles


def test_publishable_4h_rejects_arguments_of_the_wrong_type() -> None:
    resampled = resample_hourly_to_4h(hand_frame(), utc("2024-12-02T21:00:30"), calendar=NYSE)
    last = NYSE.closed_candles(H4, utc("2024-12-02T21:00:30"), 1)[-1]

    with pytest.raises(TypeError):
        publishable_4h(resampled.candles, last=last, calendar=NYSE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        publishable_4h(resampled, last="2024-12-02T18:30", calendar=NYSE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        publishable_4h(resampled, last=last, calendar="NYSE")  # type: ignore[arg-type]


# --- T11: 4h records (Design 10.5) ----------------------------------------------------------------


def hand_client(frame: pd.DataFrame | None = None) -> RecordedYahooClient:
    source = provider_shaped(hand_frame()) if frame is None else frame
    return RecordedYahooClient(histories={("SPY", "1h"): source})


def test_the_hand_frame_logs_the_final_row_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    frame = fetch(make_provider(hand_client()), "SPY", H4, 10, utc("2024-11-27T18:30:30"))

    assert iso(frame)[-1] == "2024-11-27T14:30Z"
    assert provider_records(caplog) == [
        (
            "WARNING",
            "built SPY 4h candle 2024-11-27T14:30:00+00:00 from 3 of 4 hourly bars "
            "(missing 2024-11-27T16:30:00+00:00)",
        )
    ]
    assert [
        r for r in caplog.records if r.name == PIPELINE_LOGGER and r.levelno >= logging.WARNING
    ] == []


def test_the_half_day_candle_is_published_and_only_earlier_gaps_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    frame = fetch(make_provider(hand_client()), "SPY", H4, 10, utc("2024-11-29T18:00:30"))

    assert iso(frame)[-1] == "2024-11-29T14:30Z"
    assert provider_records(caplog) == [
        (
            "DEBUG",
            "built 1 earlier SPY 4h candles from incomplete hourly bars "
            "(first 2024-11-27T14:30:00+00:00, last 2024-11-27T14:30:00+00:00)",
        )
    ]
    assert [
        r for r in caplog.records if r.name == PIPELINE_LOGGER and r.levelno >= logging.WARNING
    ] == []


def test_a_withheld_candle_is_logged_and_raises_unpublished(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(CandleNotPublishedError) as caught:
        fetch(make_provider(hand_client()), "SPY", H4, 10, utc("2024-12-02T21:00:30"))

    assert provider_records(caplog) == [
        (
            "DEBUG",
            "withheld SPY 4h candle 2024-12-02T18:30:00+00:00: hourly bar "
            "2024-12-02T20:30:00+00:00 is missing",
        ),
        (
            "DEBUG",
            "built 1 earlier SPY 4h candles from incomplete hourly bars "
            "(first 2024-11-27T14:30:00+00:00, last 2024-11-27T14:30:00+00:00)",
        ),
    ]
    assert caught.value.reason is UnpublishedReason.MISSING
    assert caught.value.expected_label == utc("2024-12-02T18:30")
    assert caught.value.last_label == utc("2024-12-02T14:30")


def test_a_slot_without_hourly_rows_is_missing_and_earlier_gaps_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(CandleNotPublishedError) as caught:
        fetch(make_provider(hand_client()), "SPY", H4, 10, utc("2024-12-03T18:30:30"))

    assert provider_records(caplog) == [
        (
            "DEBUG",
            "built 2 earlier SPY 4h candles from incomplete hourly bars "
            "(first 2024-11-27T14:30:00+00:00, last 2024-12-02T18:30:00+00:00)",
        )
    ]
    assert caught.value.expected_label == utc("2024-12-03T14:30")
    assert caught.value.last_label == utc("2024-12-02T18:30")


def test_a_withheld_half_day_candle_names_its_last_missing_hour(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=PROVIDER_LOGGER)
    hourly = hand_frame().drop(index=pd.Timestamp(utc("2024-11-29T16:30")))

    with pytest.raises(CandleNotPublishedError):
        fetch(
            make_provider(hand_client(provider_shaped(hourly))),
            "SPY",
            H4,
            10,
            utc("2024-11-29T18:00:30"),
        )

    assert provider_records(caplog)[0] == (
        "DEBUG",
        "withheld SPY 4h candle 2024-11-29T14:30:00+00:00: hourly bar "
        "2024-11-29T17:30:00+00:00 is missing",
    )


def test_a_final_row_warning_lists_only_published_missing_hours(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=PROVIDER_LOGGER)
    hourly = hand_frame().drop(index=pd.Timestamp(utc("2024-11-29T15:30")))

    fetch(
        make_provider(hand_client(provider_shaped(hourly))),
        "SPY",
        H4,
        10,
        utc("2024-11-29T18:00:30"),
    )

    assert provider_records(caplog)[-1] == (
        "WARNING",
        "built SPY 4h candle 2024-11-29T14:30:00+00:00 from 2 of 4 hourly bars "
        "(missing 2024-11-29T15:30:00+00:00)",
    )


def test_dropped_hourly_rows_are_logged_with_the_1h_timeframe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    shaped = provider_shaped(hand_frame())
    off_grid = shaped.iloc[[0]].copy()
    off_grid.index = (
        pd.DatetimeIndex([pd.Timestamp("2024-11-27 10:00", tz="America/New_York")])
        .as_unit("s")
        .rename("Datetime")
    )
    raw = pd.concat([shaped, off_grid]).sort_index()

    fetch(make_provider(hand_client(raw)), "SPY", H4, 10, utc("2024-11-29T18:00:30"))

    assert [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == PIPELINE_LOGGER
    ] == [
        (
            "WARNING",
            "dropped 1 off_grid candle rows for SPY 1h (first 2024-11-27T15:00:00+00:00, "
            "last 2024-11-27T15:00:00+00:00)",
        )
    ]


def test_an_injected_logger_receives_the_provider_records(caplog: pytest.LogCaptureFixture) -> None:
    injected = logging.getLogger("tests.yahoo_provider.injected")
    caplog.set_level(logging.DEBUG, logger=injected.name)
    caplog.set_level(logging.DEBUG, logger=PROVIDER_LOGGER)
    provider = YFinanceProvider(
        client=hand_client(), transport=make_transport(), calendar=NYSE, logger=injected
    )

    asyncio.run(provider.fetch_candles("SPY", H4, 10, now=utc("2024-11-27T18:30:30")))

    assert [
        record.name for record in caplog.records if record.name in {injected.name, PROVIDER_LOGGER}
    ] == [injected.name]


# --- T11: validate_ticker (Design 10.7) ---------------------------------------------------------


def metadata_client(**overrides: Mapping[str, object]) -> RecordedYahooClient:
    metadata = dict(load_metadata())
    metadata.update(overrides)
    return RecordedYahooClient(metadata=metadata)


def validate(provider: YFinanceProvider, ticker: object) -> TickerInfo:
    return asyncio.run(provider.validate_ticker(ticker))  # type: ignore[arg-type]


def test_validate_ticker_returns_the_recorded_spy_info_with_one_query() -> None:
    client = metadata_client()

    info = validate(make_provider(client), " spy ")

    long_name = load_metadata()["SPY"]["longName"]
    assert isinstance(long_name, str)
    assert info == TickerInfo(
        symbol="SPY",
        asset_type=AssetType.ETF,
        exchange=Exchange.NYSE_ARCA,
        currency="USD",
        name=long_name,
    )
    assert client.queries == (HistoryQuery(symbol="SPY", interval="1d", period="1mo"),)


@pytest.mark.parametrize(
    ("symbol", "asset_type", "exchange"),
    [
        ("AAPL", AssetType.EQUITY, Exchange.NASDAQ),
        ("QQQ", AssetType.ETF, Exchange.NASDAQ),
        ("BRK-B", AssetType.EQUITY, Exchange.NYSE),
        ("UEC", AssetType.EQUITY, Exchange.NYSE_AMERICAN),
        ("ARKB", AssetType.ETF, Exchange.CBOE_BZX),
    ],
)
def test_supported_recorded_symbols_are_validated(
    symbol: str, asset_type: AssetType, exchange: Exchange
) -> None:
    info = validate(make_provider(metadata_client()), symbol)

    assert (info.symbol, info.asset_type, info.exchange, info.currency) == (
        symbol,
        asset_type,
        exchange,
        "USD",
    )


@pytest.mark.parametrize(
    ("symbol", "reason"),
    [
        ("^GSPC", InvalidTickerReason.UNSUPPORTED_ASSET_TYPE),
        ("VFIAX", InvalidTickerReason.UNSUPPORTED_ASSET_TYPE),
        ("BTC-USD", InvalidTickerReason.UNSUPPORTED_ASSET_TYPE),
        ("EURUSD=X", InvalidTickerReason.UNSUPPORTED_ASSET_TYPE),
        ("ES=F", InvalidTickerReason.UNSUPPORTED_ASSET_TYPE),
        ("RELIANCE.NS", InvalidTickerReason.UNSUPPORTED_EXCHANGE),
        ("TCEHY", InvalidTickerReason.UNSUPPORTED_EXCHANGE),
        ("NSRGY", InvalidTickerReason.UNSUPPORTED_EXCHANGE),
        ("SHOP.TO", InvalidTickerReason.UNSUPPORTED_EXCHANGE),
    ],
)
def test_unsupported_recorded_symbols_are_rejected(
    symbol: str, reason: InvalidTickerReason
) -> None:
    with pytest.raises(InvalidTickerError) as caught:
        validate(make_provider(metadata_client()), symbol)

    assert caught.value.reason is reason


def test_an_unsupported_currency_is_rejected() -> None:
    aapl = dict(load_metadata()["AAPL"]) | {"exchangeName": "NYQ", "currency": "GBp"}

    with pytest.raises(InvalidTickerError) as caught:
        validate(make_provider(metadata_client(AAPL=aapl)), "AAPL")

    assert caught.value.reason is InvalidTickerReason.UNSUPPORTED_CURRENCY


@pytest.mark.parametrize(
    ("ticker", "error", "reason"),
    [
        ("US0378331005", InvalidTickerError, InvalidTickerReason.NOT_FOUND),
        ("M&M.NS", InvalidTickerError, InvalidTickerReason.MALFORMED),
        ("AAPL|X", InvalidTickerError, InvalidTickerReason.MALFORMED),
        ("", InvalidTickerError, InvalidTickerReason.MALFORMED),
        (42, TypeError, None),
    ],
)
def test_malformed_isin_and_non_text_tickers_make_no_client_call(
    ticker: object, error: type[Exception], reason: InvalidTickerReason | None
) -> None:
    client = metadata_client()

    with pytest.raises(error) as caught:
        validate(make_provider(client), ticker)

    if reason is not None:
        assert isinstance(caught.value, InvalidTickerError)
        assert caught.value.reason is reason
    assert client.queries == ()


def test_an_unknown_symbol_raises_the_client_error() -> None:
    client = metadata_client()

    with pytest.raises(InvalidTickerError) as caught:
        validate(make_provider(client), "ZZZZNOTREAL")

    assert caught.value.reason is InvalidTickerReason.NOT_FOUND
    assert len(client.queries) == 1


def test_no_data_becomes_not_found_from_none() -> None:
    client = metadata_client()
    client.fail_next(NoDataError("nothing", ticker="SPY"))

    with pytest.raises(InvalidTickerError) as caught:
        validate(make_provider(client), "SPY")

    assert caught.value.reason is InvalidTickerReason.NOT_FOUND
    assert caught.value.ticker == "SPY"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_an_unavailable_provider_is_retried_three_times_then_raised() -> None:
    client = metadata_client()
    error = ProviderUnavailableError(ProviderFailure.CONNECTION, ticker="SPY")
    client.fail_next(error, times=3)
    sleep = RecordingSleep()

    with pytest.raises(ProviderUnavailableError) as caught:
        validate(make_provider(client, sleep=sleep), "SPY")

    assert caught.value is error
    assert len(client.queries) == 3
    assert sleep.delays == [1.5, 3.0]


def test_a_metadata_symbol_mismatch_is_a_provider_data_error() -> None:
    spy = dict(load_metadata()["SPY"]) | {"symbol": "SPYX"}

    with pytest.raises(ProviderDataError) as caught:
        validate(make_provider(metadata_client(SPY=spy)), "SPY")

    assert caught.value.kind == "symbol_mismatch"


# --- T11: the error surface through the real client and transport (AC26) -------------------------


def http_error(status: int, retry_after: str | None = None) -> Exception:
    response = requests.Response()
    response.status_code = status
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return requests_exceptions.HTTPError(URL, response=response)


RETRIED = [1.5, 3.0]  # the default policy with jitter 0.5, attempts 1 and 2
RATE_LIMITED = [15.0, 15.0]  # the rate-limited floor
NOT_RETRIED: list[float] = []

# (id, factory, sleeps): two sleeps mean max_attempts (3) client calls, none means one call.
SURFACE: tuple[tuple[str, Callable[[], BaseException], list[float]], ...] = (
    ("market_data_error", lambda: NoDataError("nothing", ticker="SPY"), NOT_RETRIED),
    ("yf_rate_limit", yf_exceptions.YFRateLimitError, RATE_LIMITED),
    ("yf_tz_missing", lambda: yf_exceptions.YFTzMissingError("SPY"), NOT_RETRIED),
    (
        "yf_delisted",
        lambda: yf_exceptions.YFPricesMissingError(
            "SPY", "", yahoo_reason="No data found, symbol may be delisted"
        ),
        NOT_RETRIED,
    ),
    ("yf_prices_missing", lambda: yf_exceptions.YFPricesMissingError("SPY", URL), NOT_RETRIED),
    ("yf_data", lambda: yf_exceptions.YFDataException(URL), RETRIED),
    ("json", lambda: json.JSONDecodeError("Expecting value", URL, 0), RETRIED),
    ("timeout", lambda: TimeoutError(URL), RETRIED),
    ("curl_timeout", lambda: curl_exceptions.ConnectTimeout(URL), RETRIED),
    ("requests_timeout", lambda: requests_exceptions.ReadTimeout(URL), RETRIED),
    ("http_404", lambda: http_error(404), NOT_RETRIED),
    ("http_429", lambda: http_error(429, "7"), RATE_LIMITED),
    ("http_429_beyond_max_delay", lambda: http_error(429, "7200"), NOT_RETRIED),
    ("http_503", lambda: http_error(503), RETRIED),
    ("http_401", lambda: http_error(401), RETRIED),
    ("dns", lambda: curl_exceptions.DNSError(URL), RETRIED),
    ("ssl", lambda: ssl.SSLError(URL), RETRIED),
    ("reset", lambda: ConnectionResetError(URL), RETRIED),
    ("gaierror", lambda: socket.gaierror(URL), RETRIED),
    ("key_error", lambda: KeyError(URL), NOT_RETRIED),
    ("isin", lambda: ValueError("Invalid ISIN number: " + URL), NOT_RETRIED),
    ("invalid_period", lambda: yf_exceptions.YFInvalidPeriodError("SPY", "2mo", URL), NOT_RETRIED),
    ("network_access", lambda: NetworkAccessError(URL), NOT_RETRIED),
)


@pytest.fixture
def raising_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[BaseException], list[str]]]:
    """Replace ``yfinance.Ticker`` with a fake whose ``history`` raises the given exception."""
    monkeypatch.setattr(yfinance, "set_tz_cache_location", lambda location: None)
    logger = logging.getLogger("yfinance")
    saved = (
        yfinance.config.debug.hide_exceptions,
        yfinance.config.debug.logging,
        yfinance.config.network.retries,
        logger.level,
    )

    def install(error: BaseException) -> list[str]:
        calls: list[str] = []

        class Ticker:
            def __init__(self, symbol: str) -> None:
                calls.append(symbol)

            def history(self, **kwargs: object) -> pd.DataFrame:
                raise error

            def get_history_metadata(self) -> dict[str, object]:
                return {}

        monkeypatch.setattr(yfinance, "Ticker", Ticker)
        return calls

    try:
        yield install
    finally:
        yfinance.config.debug.hide_exceptions = saved[0]
        yfinance.config.debug.logging = saved[1]
        yfinance.config.network.retries = saved[2]
        logger.setLevel(saved[3])


def real_client_provider(tmp_path: Path, sleep: RecordingSleep) -> YFinanceProvider:
    from trading_bot.data.yahoo.client import YFinanceClient

    return YFinanceProvider(
        client=YFinanceClient(cache_dir=tmp_path), transport=make_transport(sleep), calendar=NYSE
    )


@pytest.mark.parametrize("method", ["fetch_candles", "validate_ticker"])
@pytest.mark.parametrize(
    ("factory", "sleeps"), [row[1:] for row in SURFACE], ids=[row[0] for row in SURFACE]
)
def test_every_mapped_exception_surfaces_as_an_allowed_error(
    tmp_path: Path,
    raising_ticker: Callable[[BaseException], list[str]],
    factory: Callable[[], BaseException],
    sleeps: list[float],
    method: str,
) -> None:
    error = factory()
    calls = raising_ticker(error)
    sleep = RecordingSleep()
    provider = real_client_provider(tmp_path, sleep)

    async def run() -> object:
        if method == "fetch_candles":
            return await provider.fetch_candles("SPY", D1, 2, now=utc("2025-11-28T17:59:59"))
        return await provider.validate_ticker("SPY")

    with pytest.raises(BaseException) as caught:
        asyncio.run(run())

    if isinstance(error, NetworkAccessError):
        assert caught.value is error
    else:
        allowed = FETCH_ERRORS if method == "fetch_candles" else VALIDATE_ERRORS
        assert isinstance(caught.value, allowed)
        assert isinstance(caught.value, MarketDataError)
        assert URL not in str(caught.value)
    assert len(calls) == (3 if sleeps else 1)
    assert sleep.delays == sleeps


# --- T11: Protocol, factory and aclose (AC27) ---------------------------------------------------


def test_the_provider_conforms_to_the_market_data_provider_protocol() -> None:
    provider = make_provider(metadata_client())

    assert yfinance_as_market_data_provider(provider) is provider


def test_the_constructor_rejects_arguments_of_the_wrong_type() -> None:
    client = metadata_client()
    with pytest.raises(TypeError):
        YFinanceProvider(client=client, transport="transport", calendar=NYSE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        YFinanceProvider(client=client, transport=make_transport(), calendar="NYSE")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        YFinanceProvider(client=client, transport=make_transport(), calendar=NYSE, logger="log")  # type: ignore[arg-type]


def test_aclose_closes_the_transport() -> None:
    client = metadata_client()
    provider = make_provider(client)

    async def scenario() -> None:
        await provider.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await provider.validate_ticker("SPY")

    asyncio.run(scenario())

    assert client.queries == ()


def test_the_factory_wires_a_working_provider_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured: list[Path] = []
    monkeypatch.setattr(client_module, "configure_yfinance", configured.append)
    calls: list[tuple[str, dict[str, object]]] = []
    spy_metadata = load_metadata()["SPY"]

    class Ticker:
        def __init__(self, symbol: str) -> None:
            calls.append(("Ticker", {"symbol": symbol}))

        def history(self, **kwargs: object) -> pd.DataFrame:
            calls.append(("history", kwargs))
            return load_recording("spy_1d_2025-11-24").frame

        def get_history_metadata(self) -> Mapping[str, object]:
            return spy_metadata

    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    slept = RecordingSleep()
    # The factory keeps the default sleeps; recording them proves that nothing slept.
    bucket_defaults = TokenBucket.__init__.__kwdefaults__
    transport_defaults = ProviderTransport.__init__.__kwdefaults__
    assert bucket_defaults is not None
    assert transport_defaults is not None
    monkeypatch.setitem(bucket_defaults, "sleep", slept)
    monkeypatch.setitem(transport_defaults, "sleep", slept)
    cache_dir = tmp_path / "yfinance"
    built: list[YFinanceProvider] = []

    async def scenario() -> TickerInfo:
        provider = build_yfinance_provider(calendar=NYSE, cache_dir=cache_dir)  # in the loop (D68)
        built.append(provider)
        return await provider.validate_ticker("SPY")

    info = asyncio.run(scenario())

    assert isinstance(built[0], YFinanceProvider)
    assert info.symbol == "SPY"
    assert info.exchange is Exchange.NYSE_ARCA
    assert configured == [cache_dir]
    assert [name for name, _ in calls] == ["Ticker", "history"]
    assert calls[1][1]["period"] == "1mo"
    assert slept.delays == []


def test_the_factory_uses_the_given_policy_and_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "configure_yfinance", lambda cache_dir: None)
    calls: list[str] = []

    class Ticker:
        def __init__(self, symbol: str) -> None:
            calls.append(symbol)

        def history(self, **kwargs: object) -> pd.DataFrame:
            raise TimeoutError(URL)

        def get_history_metadata(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(yfinance, "Ticker", Ticker)

    async def scenario() -> TickerInfo:
        provider = build_yfinance_provider(  # built inside the loop that uses it (D68)
            calendar=NYSE,
            cache_dir=tmp_path,
            policy=RetryPolicy(max_attempts=1),
            rate_limit=RateLimit(rate=2.0, burst=1),
        )
        return await provider.validate_ticker("SPY")

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(scenario())

    assert calls == ["SPY"]


def test_the_factory_rejects_arguments_of_the_wrong_type_before_creating_the_cache(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"

    with pytest.raises(TypeError):
        build_yfinance_provider(calendar="NYSE", cache_dir=cache_dir)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_yfinance_provider(calendar=NYSE, cache_dir=cache_dir, policy="retry")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_yfinance_provider(calendar=NYSE, cache_dir=cache_dir, rate_limit=5)  # type: ignore[arg-type]
    assert not cache_dir.exists()
