"""Tests of the yfinance client (spec 011, T5-T6, AC12-AC15; decisions D48, D50 and D53).

T5: ``configure_yfinance`` (Design 8.1) and the ``Ticker.history`` call (Design 8.2), with a fake
``yfinance.Ticker`` that records every call. T6: the error mapping (Design 8.3) on exceptions
built at runtime, each carrying a crumb-like URL that must never reach ``str()``, ``repr()`` or a
log record, and the library canary (Design 8.4). Tests restore yfinance's configuration and
logger level; the cache location only ever points to a pytest temporary directory.
"""

from __future__ import annotations

import inspect
import json
import logging
import socket
import ssl
from collections.abc import Callable, Iterator, Mapping
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest
import requests
import requests.exceptions as requests_exceptions
import yfinance
import yfinance.cache
import yfinance.exceptions as yf_exceptions
import yfinance.utils
from curl_cffi import requests as curl_requests
from curl_cffi.requests import exceptions as curl_exceptions
from curl_cffi.requests.headers import Headers as CurlHeaders

import trading_bot.data.yahoo.client as client_module
from tests.fixtures.calendars import utc
from tests.fixtures.network_guard import NetworkAccessError
from trading_bot.data.errors import (
    InvalidTickerError,
    InvalidTickerReason,
    MarketDataError,
    NoDataError,
    ProviderDataError,
    ProviderFailure,
    ProviderUnavailableError,
)
from trading_bot.data.yahoo.client import (
    YAHOO_REQUEST_TIMEOUT,
    YFinanceClient,
    configure_yfinance,
    map_yahoo_error,
)
from trading_bot.data.yahoo.history import ISIN_PATTERN, ChartMetadata, HistoryQuery, YahooHistory

CLIENT_LOGGER = "trading_bot.data.yahoo.client"
URL = "https://" + "query.example.invalid" + "/v8/finance/chart/SPY?crumb=" + "c" * 11
LEAKS = ("example.invalid", "crumb", "https://")


@pytest.fixture
def cache_locations(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record ``set_tz_cache_location`` calls and restore yfinance's configuration afterwards."""
    locations: list[str] = []
    monkeypatch.setattr(yfinance, "set_tz_cache_location", locations.append)
    logger = logging.getLogger("yfinance")
    saved = (
        yfinance.config.debug.hide_exceptions,
        yfinance.config.debug.logging,
        yfinance.config.network.retries,
        logger.level,
    )
    try:
        yield locations
    finally:
        yfinance.config.debug.hide_exceptions = saved[0]
        yfinance.config.debug.logging = saved[1]
        yfinance.config.network.retries = saved[2]
        logger.setLevel(saved[3])


class FakeTicker:
    """Stands in for ``yfinance.Ticker``: records calls, serves a frame and metadata, or raises."""

    def __init__(
        self,
        symbol: str,
        *,
        frame: object,
        metadata: object,
        raise_in: str | None = None,
        error: BaseException | None = None,
        log: list[tuple[str, object]],
    ) -> None:
        self._frame = frame
        self._metadata = metadata
        self._raise_in = raise_in
        self._error = error
        self._log = log
        log.append(("Ticker", symbol))
        self._maybe_raise("constructor")

    def _maybe_raise(self, place: str) -> None:
        if self._raise_in == place and self._error is not None:
            raise self._error

    def history(self, *args: object, **kwargs: object) -> object:
        self._log.append(("history", (args, kwargs)))
        self._maybe_raise("history")
        return self._frame

    def get_history_metadata(self, *args: object, **kwargs: object) -> object:
        self._log.append(("get_history_metadata", (args, kwargs)))
        self._maybe_raise("metadata")
        return self._metadata

    @property
    def info(self) -> object:
        self._log.append(("info", None))
        return {}

    @property
    def fast_info(self) -> object:
        self._log.append(("fast_info", None))
        return {}


class LazyMetadata(Mapping[str, object]):
    """Like yfinance's HistoryMetadata: iteration and ``tradingPeriods`` would cost a request."""

    def __init__(self, values: dict[str, object], log: list[tuple[str, object]]) -> None:
        self._values = values
        self._log = log

    def __getitem__(self, key: str) -> object:
        if key == "tradingPeriods":
            self._log.append(("tradingPeriods", None))
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        self._log.append(("iterate", None))
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


SPY_METADATA: dict[str, object] = {
    "symbol": "SPY",
    "instrumentType": "ETF",
    "exchangeName": "PCX",
    "currency": "USD",
    "longName": "Synthetic S&P 500 Fund",
    "shortName": "Synthetic S&P",
    "tradingPeriods": [[{"start": 1}]],
}


def install_ticker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    frame: object = None,
    metadata: object = None,
    raise_in: str | None = None,
    error: BaseException | None = None,
) -> list[tuple[str, object]]:
    log: list[tuple[str, object]] = []
    served_frame = pd.DataFrame({"Close": [1.0]}) if frame is None else frame
    served_metadata = LazyMetadata(SPY_METADATA, log) if metadata is None else metadata

    def build(symbol: str) -> FakeTicker:
        return FakeTicker(
            symbol,
            frame=served_frame,
            metadata=served_metadata,
            raise_in=raise_in,
            error=error,
            log=log,
        )

    monkeypatch.setattr(yfinance, "Ticker", build)
    return log


START_QUERY = HistoryQuery(symbol="SPY", interval="1h", start=utc("2025-11-24T14:00"))
PERIOD_QUERY = HistoryQuery(symbol="SPY", interval="1d", period="1mo")
COMMON_KWARGS: dict[str, object] = {
    "prepost": False,
    "actions": True,
    "auto_adjust": False,
    "back_adjust": False,
    "repair": False,
    "keepna": False,
    "rounding": False,
}


# --- T5: configure_yfinance (Design 8.1) ------------------------------------------------------


def test_the_module_exports_exactly_the_spec_names() -> None:
    assert client_module.__all__ == [
        "YAHOO_REQUEST_TIMEOUT",
        "YFinanceClient",
        "configure_yfinance",
        "map_yahoo_error",
    ]
    assert YAHOO_REQUEST_TIMEOUT == 15.0


def test_configure_performs_the_spec_steps(
    tmp_path: Path, cache_locations: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mkdir_calls: list[tuple[Path, tuple[object, ...], dict[str, object]]] = []
    real_mkdir = Path.mkdir

    def recording_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        mkdir_calls.append((self, args, kwargs))
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", recording_mkdir)
    yfinance.config.debug.hide_exceptions = True
    yfinance.config.debug.logging = True
    yfinance.config.network.retries = 3
    logging.getLogger("yfinance").setLevel(logging.DEBUG)
    cache_dir = tmp_path / "yahoo" / "cache"

    configure_yfinance(cache_dir)

    assert mkdir_calls[0] == (cache_dir, (), {"mode": 0o700, "parents": True, "exist_ok": True})
    assert cache_dir.is_dir()
    assert list(cache_dir.iterdir()) == []  # no SQLite file is created
    assert cache_locations == [str(cache_dir)]
    assert yfinance.config.debug.hide_exceptions is False
    assert yfinance.config.debug.logging is False
    assert yfinance.config.network.retries == 0
    assert logging.getLogger("yfinance").level == logging.WARNING


def test_configure_twice_with_the_same_directory_gives_the_same_state(
    tmp_path: Path, cache_locations: list[str]
) -> None:
    configure_yfinance(tmp_path)
    first = (
        yfinance.config.debug.hide_exceptions,
        yfinance.config.debug.logging,
        yfinance.config.network.retries,
        logging.getLogger("yfinance").level,
    )

    configure_yfinance(tmp_path)

    assert cache_locations == [str(tmp_path), str(tmp_path)]
    assert (
        yfinance.config.debug.hide_exceptions,
        yfinance.config.debug.logging,
        yfinance.config.network.retries,
        logging.getLogger("yfinance").level,
    ) == first


def test_configure_rejects_a_non_path_and_a_relative_path(cache_locations: list[str]) -> None:
    with pytest.raises(TypeError):
        configure_yfinance("cache")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="absolute"):
        configure_yfinance(Path("relative") / "cache")

    assert cache_locations == []


def test_configure_lets_an_os_error_propagate(tmp_path: Path, cache_locations: list[str]) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError):
        configure_yfinance(blocker / "cache")

    assert cache_locations == []


@pytest.mark.parametrize("timeout", ["15", True, None])
def test_the_client_rejects_a_non_number_timeout_before_configuring(
    tmp_path: Path, cache_locations: list[str], timeout: object
) -> None:
    with pytest.raises(TypeError):
        YFinanceClient(cache_dir=tmp_path, request_timeout=timeout)  # type: ignore[arg-type]

    assert cache_locations == []


@pytest.mark.parametrize("timeout", [0, -1.0, float("nan"), float("inf")])
def test_the_client_rejects_a_bad_timeout_before_configuring(
    tmp_path: Path, cache_locations: list[str], timeout: float
) -> None:
    with pytest.raises(ValueError):
        YFinanceClient(cache_dir=tmp_path, request_timeout=timeout)

    assert cache_locations == []


def test_the_client_configures_yfinance(tmp_path: Path, cache_locations: list[str]) -> None:
    YFinanceClient(cache_dir=tmp_path)

    assert cache_locations == [str(tmp_path)]
    assert yfinance.config.debug.hide_exceptions is False


# --- T5: the call (Design 8.2) ----------------------------------------------------------------


def test_a_start_query_calls_yfinance_once_with_exactly_the_spec_arguments(
    tmp_path: Path, cache_locations: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = pd.DataFrame({"Close": [1.0, 2.0]})
    log = install_ticker(monkeypatch, frame=frame)

    result = YFinanceClient(cache_dir=tmp_path).history(START_QUERY)

    assert log == [
        ("Ticker", "SPY"),
        (
            "history",
            (
                (),
                {
                    "start": utc("2025-11-24T14:00"),
                    "interval": "1h",
                    **COMMON_KWARGS,
                    "timeout": 15.0,
                },
            ),
        ),
        ("get_history_metadata", ((), {})),
    ]
    assert isinstance(result, YahooHistory)
    assert result.frame is frame
    assert result.metadata == ChartMetadata(
        symbol="SPY",
        instrument_type="ETF",
        exchange_name="PCX",
        currency="USD",
        long_name="Synthetic S&P 500 Fund",
        short_name="Synthetic S&P",
    )


def test_a_period_query_passes_one_month_and_no_start(
    tmp_path: Path, cache_locations: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    log = install_ticker(monkeypatch)

    YFinanceClient(cache_dir=tmp_path, request_timeout=7).history(PERIOD_QUERY)

    assert log[1] == (
        "history",
        ((), {"period": "1mo", "interval": "1d", **COMMON_KWARGS, "timeout": 7}),
    )
    assert len(log) == 3


def test_the_call_never_passes_end_and_never_reads_info_or_trading_periods(
    tmp_path: Path, cache_locations: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    log = install_ticker(monkeypatch)
    client = YFinanceClient(cache_dir=tmp_path)

    client.history(START_QUERY)
    client.history(PERIOD_QUERY)

    history_kwargs = [entry[1] for entry in log if entry[0] == "history"]
    assert len(history_kwargs) == 2
    for args_and_kwargs in history_kwargs:
        assert isinstance(args_and_kwargs, tuple)
        assert "end" not in args_and_kwargs[1]
        assert "raise_errors" not in args_and_kwargs[1]
    assert {entry[0] for entry in log} == {"Ticker", "history", "get_history_metadata"}


@pytest.mark.parametrize("frame", [[1.0], "frame", pd.Series([1.0])])
def test_a_frame_that_is_not_a_dataframe_is_an_invalid_response(
    tmp_path: Path, cache_locations: list[str], monkeypatch: pytest.MonkeyPatch, frame: object
) -> None:
    install_ticker(monkeypatch, frame=frame)

    with pytest.raises(ProviderDataError) as caught:
        YFinanceClient(cache_dir=tmp_path).history(START_QUERY)

    assert caught.value.kind == "invalid_response"
    assert caught.value.ticker == "SPY"


@pytest.mark.parametrize("metadata", [[("symbol", "SPY")], "SPY", 42])
def test_metadata_that_is_not_a_mapping_is_an_invalid_response(
    tmp_path: Path, cache_locations: list[str], monkeypatch: pytest.MonkeyPatch, metadata: object
) -> None:
    install_ticker(monkeypatch, metadata=metadata)

    with pytest.raises(ProviderDataError) as caught:
        YFinanceClient(cache_dir=tmp_path).history(START_QUERY)

    assert caught.value.kind == "invalid_response"


def test_history_rejects_a_query_of_the_wrong_type(
    tmp_path: Path, cache_locations: list[str]
) -> None:
    with pytest.raises(TypeError):
        YFinanceClient(cache_dir=tmp_path).history("SPY")  # type: ignore[arg-type]


# --- T6: the error mapping (Design 8.3) --------------------------------------------------------


def with_url(error: BaseException) -> BaseException:
    """Put the crumb-like URL into the exception's arguments, as HTTP libraries do."""
    error.args = (*error.args, URL)
    return error


def http_error_requests(status: int, retry_after: str | None = None) -> Exception:
    response = requests.Response()
    response.status_code = status
    response.url = URL
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return requests_exceptions.HTTPError(f"{status} Client Error for url: {URL}", response=response)


def http_error_curl(status: int, retry_after: str | None = None) -> Exception:
    response = curl_requests.Response()
    response.status_code = status
    response.url = URL
    if retry_after is not None:
        response.headers = CurlHeaders({"Retry-After": retry_after})
    return curl_exceptions.HTTPError(f"HTTP Error {status}: {URL}", 0, response)


type Expected = tuple[type[MarketDataError], str, float | None]

# (id, exception factory, (class, reason/failure/kind, retry_after seconds))
MAPPING_TABLE: tuple[tuple[str, Callable[[], Exception], Expected], ...] = (
    (
        "yf_rate_limit",
        lambda: with_url(yf_exceptions.YFRateLimitError()),  # type: ignore[return-value]
        (ProviderUnavailableError, "rate_limited", None),
    ),
    (
        "yf_tz_missing",
        lambda: with_url(yf_exceptions.YFTzMissingError("SPY")),  # type: ignore[return-value]
        (InvalidTickerError, "not_found", None),
    ),
    (
        "yf_prices_missing_delisted",
        lambda: with_url(  # type: ignore[return-value]
            yf_exceptions.YFPricesMissingError(
                "SPY", "", yahoo_reason="No data found, symbol may be delisted"
            )
        ),
        (InvalidTickerError, "not_found", None),
    ),
    (
        "yf_prices_missing_delisted_upper_case",
        lambda: yf_exceptions.YFPricesMissingError(
            "SPY", URL, yahoo_reason="NO DATA FOUND, SYMBOL MAY BE DELISTED"
        ),
        (InvalidTickerError, "not_found", None),
    ),
    (
        "yf_prices_missing_empty",
        lambda: yf_exceptions.YFPricesMissingError(
            "SPY", " (1h 2026-09-12 12:00:00+00:00 -> 2026-09-13 08:00:00+00:00) " + URL
        ),
        (NoDataError, "", None),
    ),
    (
        "yf_prices_missing_range",
        lambda: with_url(  # type: ignore[return-value]
            yf_exceptions.YFPricesMissingError(
                "SPY",
                "",
                yahoo_reason="1h data not available for startTime=1 and endTime=2. "
                "The requested range must be within the last 730 days.",
            )
        ),
        (NoDataError, "", None),
    ),
    (
        "yf_data_exception",
        lambda: yf_exceptions.YFDataException("*** YAHOO! FINANCE IS CURRENTLY DOWN! *** " + URL),
        (ProviderUnavailableError, "invalid_response", None),
    ),
    (
        "json_decode_error",
        lambda: json.JSONDecodeError("Expecting value", "<html>" + URL, 0),
        (ProviderUnavailableError, "invalid_response", None),
    ),
    (
        "requests_json_decode_error",
        lambda: requests_exceptions.JSONDecodeError("Expecting value", "<html>" + URL, 0),
        (ProviderUnavailableError, "invalid_response", None),
    ),
    (
        "curl_json_decode_error",
        lambda: curl_exceptions.JSONDecodeError("Expecting value " + URL, "<html>", 0),
        (ProviderUnavailableError, "invalid_response", None),
    ),
    ("builtin_timeout", lambda: TimeoutError(URL), (ProviderUnavailableError, "timeout", None)),
    (
        "curl_timeout",
        lambda: curl_exceptions.Timeout(URL),
        (ProviderUnavailableError, "timeout", None),
    ),
    (
        "curl_connect_timeout",
        lambda: curl_exceptions.ConnectTimeout(URL),
        (ProviderUnavailableError, "timeout", None),
    ),
    (
        "requests_read_timeout",
        lambda: requests_exceptions.ReadTimeout(URL),
        (ProviderUnavailableError, "timeout", None),
    ),
    ("requests_404", lambda: http_error_requests(404), (InvalidTickerError, "not_found", None)),
    ("curl_404", lambda: http_error_curl(404), (InvalidTickerError, "not_found", None)),
    (
        "requests_429_seconds",
        lambda: http_error_requests(429, "7"),
        (ProviderUnavailableError, "rate_limited", 7.0),
    ),
    (
        "curl_429_seconds",
        lambda: http_error_curl(429, "7"),
        (ProviderUnavailableError, "rate_limited", 7.0),
    ),
    (
        "requests_429_capped",
        lambda: http_error_requests(429, "7200"),
        (ProviderUnavailableError, "rate_limited", 3600.0),
    ),
    (
        "curl_429_capped",
        lambda: http_error_curl(429, "7200"),
        (ProviderUnavailableError, "rate_limited", 3600.0),
    ),
    (
        "requests_429_date",
        lambda: http_error_requests(429, "Wed, 21 Oct 2026 07:28:00 GMT"),
        (ProviderUnavailableError, "rate_limited", None),
    ),
    (
        "curl_429_date",
        lambda: http_error_curl(429, "Wed, 21 Oct 2026 07:28:00 GMT"),
        (ProviderUnavailableError, "rate_limited", None),
    ),
    (
        "requests_429_no_header",
        lambda: http_error_requests(429),
        (ProviderUnavailableError, "rate_limited", None),
    ),
    (
        "curl_429_no_header",
        lambda: http_error_curl(429),
        (ProviderUnavailableError, "rate_limited", None),
    ),
    (
        "requests_500",
        lambda: http_error_requests(500),
        (ProviderUnavailableError, "server_error", None),
    ),
    ("curl_503", lambda: http_error_curl(503), (ProviderUnavailableError, "server_error", None)),
    (
        "requests_503",
        lambda: http_error_requests(503),
        (ProviderUnavailableError, "server_error", None),
    ),
    ("curl_500", lambda: http_error_curl(500), (ProviderUnavailableError, "server_error", None)),
    (
        "requests_401",
        lambda: http_error_requests(401),
        (ProviderUnavailableError, "invalid_response", None),
    ),
    (
        "curl_401",
        lambda: http_error_curl(401),
        (ProviderUnavailableError, "invalid_response", None),
    ),
    (
        "curl_dns_error",
        lambda: curl_exceptions.DNSError(URL),
        (ProviderUnavailableError, "connection", None),
    ),
    (
        "requests_connection_error",
        lambda: requests_exceptions.ConnectionError(URL),
        (ProviderUnavailableError, "connection", None),
    ),
    ("ssl_error", lambda: ssl.SSLError(URL), (ProviderUnavailableError, "connection", None)),
    (
        "connection_reset",
        lambda: ConnectionResetError(URL),
        (ProviderUnavailableError, "connection", None),
    ),
    ("gaierror", lambda: socket.gaierror(URL), (ProviderUnavailableError, "connection", None)),
)

UNEXPECTED_TABLE: tuple[tuple[str, Callable[[], Exception], str], ...] = (
    ("key_error", lambda: KeyError("chart " + URL), "builtins.KeyError"),
    ("invalid_isin", lambda: ValueError("Invalid ISIN number: X " + URL), "builtins.ValueError"),
    (
        "yf_invalid_period",
        lambda: yf_exceptions.YFInvalidPeriodError("SPY", "2mo", "1d, 5d " + URL),
        "yfinance.exceptions.YFInvalidPeriodError",
    ),
)


def assert_no_leak(error: BaseException, caplog: pytest.LogCaptureFixture) -> None:
    rendered = [str(error), repr(error), *caplog.messages, caplog.text]
    for text in rendered:
        for leak in LEAKS:
            assert leak not in text, f"{leak!r} leaked into {text!r}"


def assert_mapped(result: MarketDataError, expected: Expected) -> None:
    error_type, code, retry_after = expected
    assert type(result) is error_type
    assert result.ticker == "SPY"
    if isinstance(result, ProviderUnavailableError):
        assert result.failure is ProviderFailure(code)
        expected_retry = None if retry_after is None else timedelta(seconds=retry_after)
        assert result.retry_after == expected_retry
    elif isinstance(result, InvalidTickerError):
        assert result.reason is InvalidTickerReason(code)


@pytest.mark.parametrize(
    ("factory", "expected"),
    [row[1:] for row in MAPPING_TABLE],
    ids=[row[0] for row in MAPPING_TABLE],
)
def test_map_yahoo_error_gives_the_spec_table_without_leaks_or_logs(
    factory: Callable[[], Exception], expected: Expected, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    error = factory()

    result = map_yahoo_error(error, symbol="SPY")

    assert_mapped(result, expected)
    assert_no_leak(result, caplog)
    assert [record for record in caplog.records if record.name == CLIENT_LOGGER] == []


def test_a_market_data_error_maps_to_the_same_object(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    error = NoDataError("no candles in the window", ticker="SPY")

    assert map_yahoo_error(error, symbol="SPY") is error
    assert caplog.records == []


@pytest.mark.parametrize(
    ("factory", "qualified_name"),
    [row[1:] for row in UNEXPECTED_TABLE],
    ids=[row[0] for row in UNEXPECTED_TABLE],
)
def test_unexpected_exceptions_log_exactly_one_error_naming_the_class_and_symbol(
    factory: Callable[[], Exception], qualified_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    result = map_yahoo_error(factory(), symbol="SPY")

    assert type(result) is ProviderDataError
    assert result.kind == "unexpected_provider_error"
    assert result.ticker == "SPY"
    records = [record for record in caplog.records if record.name == CLIENT_LOGGER]
    assert [(record.levelname, record.getMessage()) for record in records] == [
        ("ERROR", f"unexpected {qualified_name} from yfinance for SPY")
    ]
    assert records[0].exc_info is None
    assert_no_leak(result, caplog)


def test_a_status_code_that_is_a_bool_is_not_a_status(caplog: pytest.LogCaptureFixture) -> None:
    class Response:
        status_code = True

    class FailureError(Exception):
        response = Response()

    result = map_yahoo_error(FailureError(URL), symbol="SPY")

    assert isinstance(result, ProviderDataError)
    assert result.kind == "unexpected_provider_error"


def test_a_response_that_cannot_be_read_is_not_a_status(caplog: pytest.LogCaptureFixture) -> None:
    class FailureError(OSError):
        @property
        def response(self) -> object:
            raise RuntimeError(URL)

    result = map_yahoo_error(FailureError(URL), symbol="SPY")

    assert isinstance(result, ProviderUnavailableError)
    assert result.failure is ProviderFailure.CONNECTION


@pytest.mark.parametrize(
    ("header", "seconds"),
    [("0", 0.0), ("0007", 7.0), ("3600", 3600.0), ("3601", 3600.0), ("9" * 5000, 3600.0)],
)
def test_retry_after_seconds_are_parsed_and_capped(header: str, seconds: float) -> None:
    result = map_yahoo_error(http_error_requests(429, header), symbol="SPY")

    assert isinstance(result, ProviderUnavailableError)
    assert result.retry_after == timedelta(seconds=seconds)


@pytest.mark.parametrize("header", [" 7", "7 ", "+7", "-7", "7.5", "", chr(0x0667)])
def test_retry_after_that_is_not_only_ascii_digits_is_ignored(header: str) -> None:
    result = map_yahoo_error(http_error_requests(429, header), symbol="SPY")

    assert isinstance(result, ProviderUnavailableError)
    assert result.retry_after is None


def test_headers_that_are_not_a_mapping_are_ignored() -> None:
    class Response:
        status_code = 429
        headers = (("Retry-After", "7"),)

    class FailureError(Exception):
        response = Response()

    result = map_yahoo_error(FailureError(URL), symbol="SPY")

    assert isinstance(result, ProviderUnavailableError)
    assert result.retry_after is None


def test_headers_that_cannot_be_read_are_ignored() -> None:
    class Headers(Mapping[str, str]):
        def __getitem__(self, key: str) -> str:
            raise RuntimeError(URL)

        def __iter__(self) -> Iterator[str]:
            return iter(())

        def __len__(self) -> int:
            return 0

        def get(self, key: str, default: object = None) -> str:  # type: ignore[override]
            raise RuntimeError(URL)

    class Response:
        status_code = 429
        headers = Headers()

    class FailureError(Exception):
        response = Response()

    result = map_yahoo_error(FailureError(URL), symbol="SPY")

    assert isinstance(result, ProviderUnavailableError)
    assert result.failure is ProviderFailure.RATE_LIMITED
    assert result.retry_after is None


def test_a_yahoo_reason_that_is_not_a_str_is_no_data() -> None:
    error = yf_exceptions.YFPricesMissingError("SPY", "")
    error.yahoo_reason = 42

    assert type(map_yahoo_error(error, symbol="SPY")) is NoDataError


def test_map_yahoo_error_rejects_arguments_of_the_wrong_type() -> None:
    with pytest.raises(TypeError):
        map_yahoo_error("boom", symbol="SPY")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        map_yahoo_error(KeyError("x"), symbol=42)  # type: ignore[arg-type]


# --- T6: through the client, raised from None -------------------------------------------------


@pytest.mark.parametrize("place", ["constructor", "history", "metadata"])
@pytest.mark.parametrize(
    ("factory", "expected"),
    [row[1:] for row in MAPPING_TABLE],
    ids=[row[0] for row in MAPPING_TABLE],
)
def test_the_client_raises_the_mapped_error_from_none(
    tmp_path: Path,
    cache_locations: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    factory: Callable[[], Exception],
    expected: Expected,
    place: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    install_ticker(monkeypatch, raise_in=place, error=factory())

    with pytest.raises(MarketDataError) as caught:
        YFinanceClient(cache_dir=tmp_path).history(START_QUERY)

    assert_mapped(caught.value, expected)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert_no_leak(caught.value, caplog)


def test_the_client_raises_a_market_data_error_as_the_same_object_from_none(
    tmp_path: Path, cache_locations: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    error = NoDataError("no candles in the window", ticker="SPY")
    install_ticker(monkeypatch, raise_in="history", error=error)

    with pytest.raises(NoDataError) as caught:
        YFinanceClient(cache_dir=tmp_path).history(START_QUERY)

    assert caught.value is error
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize(
    ("factory", "qualified_name"),
    [row[1:] for row in UNEXPECTED_TABLE],
    ids=[row[0] for row in UNEXPECTED_TABLE],
)
def test_the_client_logs_one_error_for_unexpected_exceptions(
    tmp_path: Path,
    cache_locations: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    factory: Callable[[], Exception],
    qualified_name: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    install_ticker(monkeypatch, raise_in="constructor", error=factory())

    with pytest.raises(ProviderDataError) as caught:
        YFinanceClient(cache_dir=tmp_path).history(START_QUERY)

    assert caught.value.kind == "unexpected_provider_error"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == CLIENT_LOGGER
    ] == [("ERROR", f"unexpected {qualified_name} from yfinance for SPY")]
    assert_no_leak(caught.value, caplog)


@pytest.mark.parametrize(
    "factory", [KeyboardInterrupt, lambda: NetworkAccessError(URL)], ids=["keyboard", "network"]
)
def test_base_exceptions_that_are_not_exceptions_propagate_unchanged(
    tmp_path: Path,
    cache_locations: list[str],
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[], BaseException],
) -> None:
    error = factory()
    install_ticker(monkeypatch, raise_in="history", error=error)

    with pytest.raises(BaseException) as caught:
        YFinanceClient(cache_dir=tmp_path).history(START_QUERY)

    assert caught.value is error


# --- T6: the library canary (Design 8.4) ------------------------------------------------------


def test_the_installed_yfinance_history_accepts_every_parameter_the_client_uses() -> None:
    from yfinance.scrapers.history import PriceHistory

    parameters = inspect.signature(PriceHistory.history).parameters

    assert {
        "period",
        "interval",
        "start",
        "end",
        "prepost",
        "actions",
        "auto_adjust",
        "back_adjust",
        "repair",
        "keepna",
        "rounding",
        "timeout",
    } <= set(parameters)


def test_the_installed_yfinance_ticker_has_history_and_metadata() -> None:
    assert callable(getattr(yfinance.Ticker, "history", None))
    assert callable(getattr(yfinance.Ticker, "get_history_metadata", None))


def test_the_installed_yfinance_defines_the_mapped_exceptions() -> None:
    assert issubclass(yf_exceptions.YFDataException, yf_exceptions.YFException)
    for name in ("YFException", "YFDataException", "YFRateLimitError", "YFTzMissingError"):
        assert isinstance(getattr(yf_exceptions, name), type)
    assert isinstance(yf_exceptions.YFPricesMissingError, type)
    assert yf_exceptions.YFPricesMissingError("SPY", "", yahoo_reason="x").yahoo_reason == "x"


def test_the_installed_yfinance_configuration_can_be_read_and_assigned(
    cache_locations: list[str],
) -> None:
    assert callable(yfinance.set_tz_cache_location)
    assert callable(yfinance.cache.set_tz_cache_location)
    for section, option, value in (
        ("debug", "hide_exceptions", False),
        ("debug", "logging", False),
        ("network", "retries", 0),
    ):
        holder = getattr(yfinance.config, section)
        getattr(holder, option)
        setattr(holder, option, value)
        assert getattr(getattr(yfinance.config, section), option) == value


@pytest.mark.parametrize("text", ["US0378331005", "US037833100A", "SPY"])
def test_yfinance_isin_detection_agrees_with_the_isin_pattern(text: str) -> None:
    assert yfinance.utils.is_isin(text) is (ISIN_PATTERN.fullmatch(text) is not None)
