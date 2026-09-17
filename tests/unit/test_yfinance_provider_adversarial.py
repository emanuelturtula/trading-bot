"""Adversarial inputs and edges of the Yahoo provider stack (spec 011, T19).

Probes the surface T1-T11 do not cover with the inputs the Test plan calls out for the tester: a
``yf.download``-shaped ``MultiIndex`` frame, a naive index on the ``1h``/``1d`` path, an
after-hours hourly row and a duplicated hourly row on the ``4h`` path, an empty raw frame, a
frame whose rows are all after ``now``, metadata of the wrong type with a 10 000-character name
and bidirectional-override characters, unusual ``Retry-After`` headers, symbol-length and
ISIN-lookalike boundaries, a late ``BaseException`` from an abandoned worker, a provider closed
twice, one thousand scripted failures against a small ``max_attempts``, and the network guard
with an IPv6 loopback connection and ``urllib``.

Clients are replay fakes; sleeps and clocks are fake; every coroutine test builds its own
transport (and provider) inside the ``asyncio.run`` call that uses it (decision D68). No test
reaches the real network: the autouse guard (``tests/conftest.py``) is active throughout, and the
loopback probes exercise exactly what it allows.
"""

from __future__ import annotations

import asyncio
import http.server
import logging
import socket
import threading
import urllib.request
from collections.abc import Mapping
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
import requests
import requests.exceptions as requests_exceptions

import trading_bot.data.yahoo.client as client_module
from tests.fixtures.calendars import nyse_test_calendar, utc
from tests.fixtures.session_candles import provider_shaped, session_candles
from tests.fixtures.yahoo_recordings import RecordedYahooClient, hand_frame, load_recording
from trading_bot.data.errors import (
    InvalidTickerError,
    InvalidTickerReason,
    NoDataError,
    ProviderDataError,
    ProviderFailure,
    ProviderUnavailableError,
)
from trading_bot.data.tickers import TickerInfo
from trading_bot.data.transport import ProviderTransport, RateLimit, RetryPolicy, TokenBucket
from trading_bot.data.yahoo.history import HistoryQuery
from trading_bot.data.yahoo.instruments import check_yahoo_symbol
from trading_bot.data.yahoo.provider import YFinanceProvider
from trading_bot.domain.timeframe import Timeframe

NYSE = nyse_test_calendar()
H1 = Timeframe.H1
H4 = Timeframe.H4
D1 = Timeframe.D1
PIPELINE_LOGGER = "trading_bot.data.pipeline"
URL = "https://" + "query.example.invalid" + "/v8/finance/chart/SPY?crumb=" + "c" * 11


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
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


def fetch(
    provider: YFinanceProvider, ticker: str, timeframe: Timeframe, lookback: int, now: datetime
) -> pd.DataFrame:
    return asyncio.run(provider.fetch_candles(ticker, timeframe, lookback, now=now))


def validate(provider: YFinanceProvider, ticker: str) -> TickerInfo:
    return asyncio.run(provider.validate_ticker(ticker))


def daily_client() -> RecordedYahooClient:
    frame = load_recording("spy_1d_2025-11-24").frame
    return RecordedYahooClient(histories={("SPY", "1d"): frame})


def pipeline_records(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == PIPELINE_LOGGER
    ]


# --- A yf.download-shaped MultiIndex frame (AC9, AC21) ------------------------------------------


def multiindex_frame() -> pd.DataFrame:
    """Two rows shaped like ``yf.download``'s multi-ticker columns, never a single-ticker call."""
    index = pd.DatetimeIndex(["2025-11-24T14:30:00+00:00", "2025-11-25T14:30:00+00:00"])
    columns = pd.MultiIndex.from_product([["Close", "Open"], ["SPY"]])
    return pd.DataFrame([[100.0, 99.0], [101.0, 100.0]], index=index, columns=columns)


@pytest.mark.parametrize(("timeframe", "interval"), [(D1, "1d"), (H4, "1h")])
def test_a_multiindex_columns_frame_is_a_provider_data_error(
    timeframe: Timeframe, interval: str
) -> None:
    client = RecordedYahooClient(histories={("SPY", interval): multiindex_frame()})

    with pytest.raises(ProviderDataError) as caught:
        fetch(make_provider(client), "SPY", timeframe, 1, utc("2025-11-28T17:59:59"))

    assert caught.value.kind == "multiindex_columns"
    assert caught.value.ticker == "SPY"
    assert caught.value.timeframe is timeframe
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


# --- A naive index on the 1h/1d path (AC9, AC21; the 4h path is T10's) ---------------------------


def test_a_naive_index_on_the_daily_path_is_a_provider_data_error() -> None:
    frame = load_recording("spy_1d_2025-11-24").frame.copy(deep=True)
    frame.index = pd.DatetimeIndex(frame.index).tz_localize(None)
    client = RecordedYahooClient(histories={("SPY", "1d"): frame})

    with pytest.raises(ProviderDataError) as caught:
        fetch(make_provider(client), "SPY", D1, 2, utc("2025-11-28T18:00:30"))

    assert caught.value.kind == "naive_index"
    assert caught.value.ticker == "SPY"
    assert caught.value.timeframe is D1


# --- After-hours and duplicated hourly rows on the 4h path (AC21, AC23) --------------------------


def with_extra_row(frame: pd.DataFrame, label: pd.Timestamp) -> pd.DataFrame:
    extra = frame.iloc[[0]].copy()
    extra.index = pd.DatetimeIndex([label]).as_unit(frame.index.unit).rename(frame.index.name)
    return pd.concat([frame, extra]).sort_index()


def test_an_after_hours_hourly_row_is_dropped_and_logged_as_1h(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    raw = with_extra_row(
        provider_shaped(hand_frame()), pd.Timestamp("2024-11-27 17:00", tz="America/New_York")
    )
    client = RecordedYahooClient(histories={("SPY", "1h"): raw})

    fetch(make_provider(client), "SPY", H4, 10, utc("2024-11-29T18:00:30"))

    assert pipeline_records(caplog) == [
        (
            "WARNING",
            "dropped 1 outside_session candle rows for SPY 1h (first 2024-11-27T22:00:00+00:00, "
            "last 2024-11-27T22:00:00+00:00)",
        )
    ]


def test_a_duplicated_hourly_row_is_deduplicated_and_logged_as_1h(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    shaped = provider_shaped(hand_frame())
    raw = with_extra_row(shaped, pd.Timestamp(shaped.index[0]))  # an exact duplicate of row 0
    client = RecordedYahooClient(histories={("SPY", "1h"): raw})

    frame = fetch(make_provider(client), "SPY", H4, 10, utc("2024-11-27T18:30:30"))

    # Design 5.3: the first 4h row (2024-11-27T14:30Z), unaffected by the extra duplicate.
    assert len(frame) == 1
    assert frame.iloc[0][["open", "high", "low", "close", "volume"]].tolist() == [
        100.0,
        105.0,
        98.0,
        99.0,
        4500.0,
    ]
    assert pipeline_records(caplog) == [
        (
            "DEBUG",
            "dropped 1 duplicate candle rows for SPY 1h (first 2024-11-27T14:30:00+00:00, "
            "last 2024-11-27T14:30:00+00:00)",
        )
    ]


# --- An empty raw frame and a frame with only rows after now (AC21) ------------------------------


@pytest.mark.parametrize(
    ("timeframe", "interval", "name"),
    [(H1, "1h", "spy_1h_2025-11-24"), (D1, "1d", "spy_1d_2025-11-24")],
    ids=["1h", "1d"],
)
def test_an_empty_raw_frame_gives_no_data(timeframe: Timeframe, interval: str, name: str) -> None:
    empty = load_recording(name).frame.iloc[:0]
    client = RecordedYahooClient(histories={("SPY", interval): empty})

    with pytest.raises(NoDataError):
        fetch(make_provider(client), "SPY", timeframe, 2, utc("2025-11-28T18:00:30"))


def test_a_frame_with_only_rows_after_now_gives_no_data() -> None:
    future = session_candles(NYSE, D1, utc("2026-01-01T00:00"), utc("2026-02-01T00:00"))
    client = RecordedYahooClient(histories={("SPY", "1d"): provider_shaped(future)})

    with pytest.raises(NoDataError):
        fetch(make_provider(client), "SPY", D1, 2, utc("2025-11-28T18:00:30"))


# --- Metadata edges: wrong types, a huge name and bidirectional overrides (AC9, AC11, AC25) ------


def metadata_client(values: Mapping[str, object]) -> RecordedYahooClient:
    return RecordedYahooClient(metadata={"AAPL": values})


def test_metadata_values_of_the_wrong_type_end_as_unsupported_asset_type() -> None:
    values: dict[str, object] = {
        "symbol": "AAPL",
        "currency": "USD",
        "instrumentType": ["EQUITY"],  # wrong type: filtered out by ChartMetadata.from_mapping
        "exchangeName": 42,  # wrong type: filtered out too
        "longName": "Apple Inc.",
    }

    with pytest.raises(InvalidTickerError) as caught:
        validate(make_provider(metadata_client(values)), "AAPL")

    assert caught.value.reason is InvalidTickerReason.UNSUPPORTED_ASSET_TYPE


def test_a_ten_thousand_character_name_falls_back_to_the_short_name() -> None:
    values = {
        "symbol": "AAPL",
        "currency": "USD",
        "instrumentType": "EQUITY",
        "exchangeName": "NMS",
        "longName": "A" * 10_000,
        "shortName": "Apple",
    }

    info = validate(make_provider(metadata_client(values)), "AAPL")

    assert info.name == "Apple"


def test_bidirectional_override_characters_never_survive_into_the_name() -> None:
    override = "‮"  # RIGHT-TO-LEFT OVERRIDE: a Unicode format character, non-printable
    values = {
        "symbol": "AAPL",
        "currency": "USD",
        "instrumentType": "EQUITY",
        "exchangeName": "NMS",
        "longName": override + "Apple Inc.",
        "shortName": override + "AAPL",
    }

    info = validate(make_provider(metadata_client(values)), "AAPL")

    assert info.name == "AAPL"  # both names are non-printable: the symbol is the last fallback
    assert override not in info.name


# --- Retry-After edges beyond T6's table (AC14) ---------------------------------------------------


def http_429(retry_after: str) -> Exception:
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = retry_after
    return requests_exceptions.HTTPError(URL, response=response)


@pytest.mark.parametrize("header", ["\t7", "7\t", "36 00", "007\n", chr(0xFF17)])
def test_retry_after_with_unusual_whitespace_or_digits_is_ignored(header: str) -> None:
    result = client_module.map_yahoo_error(http_429(header), symbol="SPY")

    assert isinstance(result, ProviderUnavailableError)
    assert result.failure is ProviderFailure.RATE_LIMITED
    assert result.retry_after is None


# --- Symbol-length and ISIN-lookalike boundaries (AC9, AC10) --------------------------------------


def test_the_24_and_25_character_symbol_boundary_holds_for_the_query_too() -> None:
    twenty_four = "A" * 24
    twenty_five = "A" * 25

    assert check_yahoo_symbol(twenty_four) == twenty_four
    query = HistoryQuery(symbol=twenty_four, interval="1h", start=utc("2025-01-01T00:00"))
    assert query.symbol == twenty_four
    with pytest.raises(InvalidTickerError):
        check_yahoo_symbol(twenty_five)
    with pytest.raises(ValueError, match="symbol"):
        HistoryQuery(symbol=twenty_five, interval="1h", start=utc("2025-01-01T00:00"))


def test_near_isin_lengths_that_do_not_fully_match_are_accepted_as_symbols() -> None:
    eleven = "AB" + "1" * 8 + "9"  # two letters, 8 alnum, a digit: 11 characters, not 12
    thirteen = "AB" + "1" * 10 + "9"  # 13 characters, not 12
    assert len(eleven) == 11
    assert len(thirteen) == 13

    assert check_yahoo_symbol(eleven) == eleven
    assert check_yahoo_symbol(thirteen) == thirteen


def test_a_lowercase_twelve_character_isin_shape_is_malformed_not_not_found() -> None:
    lowercase_isin = "us0378331005"  # shaped like an ISIN, but lowercase never matches ISIN_PATTERN

    with pytest.raises(InvalidTickerError) as caught:
        check_yahoo_symbol(lowercase_isin)

    assert caught.value.reason is InvalidTickerReason.MALFORMED


# --- A late BaseException from an abandoned worker (AC18) ----------------------------------------


def test_a_late_base_exception_from_the_worker_is_discarded_without_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()

    class Boom(BaseException):
        pass

    def operation() -> int:
        gate.wait(30.0)
        raise Boom("a base exception arriving after the timeout")

    async def scenario() -> None:
        clock = FakeClock()
        transport = ProviderTransport(
            policy=RetryPolicy(max_attempts=1, attempt_timeout=0.001),
            limiter=TokenBucket(RateLimit(), clock=clock, sleep=clock.sleep),
        )
        with pytest.raises(ProviderUnavailableError):
            await transport.call(operation, ticker="SPY", timeframe=None)
        gate.set()
        await transport.aclose()  # waits for the worker; its late BaseException is discarded

    asyncio.run(scenario())

    assert [record for record in caplog.records if record.levelno >= logging.INFO] == []


# --- A provider closed twice (AC27) ---------------------------------------------------------------


def test_provider_aclose_is_idempotent() -> None:
    client = daily_client()
    provider = make_provider(client)

    async def scenario() -> None:
        await provider.aclose()
        await provider.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await provider.validate_ticker("SPY")

    asyncio.run(scenario())

    assert client.queries == ()


# --- 1 000 scripted failures against a small max_attempts (AC17) ---------------------------------


def test_a_thousand_scripted_failures_still_cost_only_max_attempts_calls() -> None:
    client = daily_client()
    error = ProviderUnavailableError(ProviderFailure.CONNECTION, ticker="SPY")
    client.fail_next(error, times=1_000)
    clock = FakeClock()
    sleep = RecordingSleep()
    transport = ProviderTransport(
        policy=RetryPolicy(max_attempts=10),
        limiter=TokenBucket(RateLimit(), clock=clock, sleep=clock.sleep),
        sleep=sleep,
        jitter=lambda: 0.0,
    )
    provider = YFinanceProvider(client=client, transport=transport, calendar=NYSE)

    with pytest.raises(ProviderUnavailableError) as caught:
        asyncio.run(provider.fetch_candles("SPY", D1, 2, now=utc("2025-11-28T17:59:59")))

    assert caught.value is error
    assert len(client.queries) == 10
    assert len(sleep.delays) == 9


# --- The network guard: IPv6 loopback and urllib (AC32) -------------------------------------------


def test_the_guard_allows_an_ipv6_loopback_connection() -> None:
    try:
        server = socket.create_server(("::1", 0), family=socket.AF_INET6)
    except OSError:
        pytest.skip("IPv6 loopback is not available in this environment")
    try:
        server.listen(1)
        port = server.getsockname()[1]
        client = socket.create_connection(("::1", port), timeout=5)
        try:
            accepted, _ = server.accept()
            try:
                client.sendall(b"x")
                assert accepted.recv(1) == b"x"
            finally:
                accepted.close()
        finally:
            client.close()
    finally:
        server.close()


class _LoopbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # a fixed stdlib method name
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, log_format: str, *args: object) -> None:
        pass  # silence the stdlib server's own request logging


def test_the_guard_allows_urllib_to_a_loopback_http_server() -> None:
    server = http.server.HTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            assert response.read() == b"ok"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


# --- Purity of the frame builders used above ------------------------------------------------------


def test_the_multiindex_frame_has_no_open_high_low_close_volume_columns() -> None:
    frame = multiindex_frame()

    assert isinstance(frame.columns, pd.MultiIndex)
    assert np.asarray(frame.to_numpy()).shape == (2, 2)
