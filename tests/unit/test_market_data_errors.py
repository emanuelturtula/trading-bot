"""Tests of the market data error hierarchy (spec 010, T8: AC10).

Errors are classified by class and ``retryable``; none is a ``ValueError``, so a caller's
``except ValueError`` for programming errors never swallows a provider failure. Messages are
single-line and built only from normalized tickers, timeframe codes, enum values and ISO
timestamps (decision D38).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

import trading_bot.data.errors as errors_module
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
from trading_bot.domain.timeframe import Timeframe

EXPECTED = datetime(2024, 7, 5, 4, 0, tzinfo=UTC)
LAST = datetime(2024, 7, 3, 4, 0, tzinfo=UTC)
ALL_ERRORS = (
    MarketDataError,
    InvalidTickerError,
    NoDataError,
    ProviderDataError,
    CandleNotPublishedError,
    ProviderUnavailableError,
)


def test_the_module_exports_exactly_the_spec_names() -> None:
    assert errors_module.__all__ == [
        "CandleNotPublishedError",
        "InvalidTickerError",
        "InvalidTickerReason",
        "MarketDataError",
        "NoDataError",
        "ProviderDataError",
        "ProviderFailure",
        "ProviderUnavailableError",
        "UnpublishedReason",
    ]


def test_the_module_imports_only_the_standard_library_and_three_domain_modules() -> None:
    tree = ast.parse(Path(errors_module.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }

    domain = {name for name in imported if name is not None and name.startswith("trading_bot")}
    assert domain <= {
        "trading_bot.domain.timeframe",
        "trading_bot.domain.signals",
        "trading_bot.domain.utc",
    }
    assert imported - domain <= {"__future__", "datetime", "enum", "re", "typing"}


# --- Hierarchy and retryable flags --------------------------------------------------------------


@pytest.mark.parametrize("error_type", ALL_ERRORS)
def test_every_error_is_a_market_data_error_and_none_is_a_value_error(
    error_type: type[MarketDataError],
) -> None:
    assert issubclass(error_type, MarketDataError)
    assert issubclass(error_type, Exception)
    assert not issubclass(error_type, ValueError)


@pytest.mark.parametrize(
    ("error_type", "retryable"),
    [
        (MarketDataError, False),
        (InvalidTickerError, False),
        (NoDataError, False),
        (ProviderDataError, False),
        (CandleNotPublishedError, True),
        (ProviderUnavailableError, True),
    ],
)
def test_retryable_is_a_class_level_flag(
    error_type: type[MarketDataError], retryable: bool
) -> None:
    assert error_type.retryable is retryable


def test_retryable_instances_follow_their_class_without_an_instance_attribute() -> None:
    unavailable = ProviderUnavailableError(ProviderFailure.TIMEOUT)
    no_data = NoDataError("no candles")

    assert unavailable.retryable is True
    assert no_data.retryable is False
    assert "retryable" not in vars(unavailable)
    assert "retryable" not in vars(no_data)


def test_enum_values_are_the_spec_codes() -> None:
    assert [reason.value for reason in InvalidTickerReason] == [
        "malformed",
        "not_found",
        "unsupported_asset_type",
        "unsupported_exchange",
        "unsupported_currency",
    ]
    assert [reason.value for reason in UnpublishedReason] == ["missing", "invalid"]
    assert [failure.value for failure in ProviderFailure] == [
        "timeout",
        "connection",
        "rate_limited",
        "server_error",
        "invalid_response",
    ]


# --- Attributes and normalization --------------------------------------------------------------


def test_the_base_error_normalizes_the_ticker_and_keeps_the_timeframe() -> None:
    error = MarketDataError("provider failure", ticker=" brk-b ", timeframe=Timeframe.H4)

    assert error.ticker == "BRK-B"
    assert error.timeframe is Timeframe.H4


def test_ticker_and_timeframe_default_to_none() -> None:
    error = NoDataError("no candles")

    assert error.ticker is None
    assert error.timeframe is None
    assert str(error) == "no candles"


@pytest.mark.parametrize(
    ("ticker", "error_type"),
    [("A B", ValueError), ("", ValueError), ("A|B", ValueError), (42, TypeError)],
)
def test_a_bad_ticker_is_a_programming_error(ticker: object, error_type: type[Exception]) -> None:
    with pytest.raises(error_type):
        NoDataError("no candles", ticker=ticker)  # type: ignore[arg-type]
    with pytest.raises(error_type):
        ProviderUnavailableError(ProviderFailure.TIMEOUT, ticker=ticker)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeframe", ["1h", "1d", 1])
def test_a_timeframe_that_is_not_a_member_raises_type_error(timeframe: object) -> None:
    with pytest.raises(TypeError):
        MarketDataError("failure", timeframe=timeframe)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProviderDataError("index_type", "failure", timeframe=timeframe)  # type: ignore[arg-type]


def test_invalid_ticker_error_keeps_its_reason() -> None:
    error = InvalidTickerError(
        InvalidTickerReason.NOT_FOUND, "unknown to the provider", ticker="aapl"
    )

    assert error.reason is InvalidTickerReason.NOT_FOUND
    assert error.ticker == "AAPL"
    assert error.timeframe is None


def test_invalid_ticker_error_rejects_a_reason_that_is_not_a_member() -> None:
    with pytest.raises(TypeError):
        InvalidTickerError("malformed", "bad ticker")  # type: ignore[arg-type]


def test_provider_data_error_keeps_its_kind_as_a_plain_str() -> None:
    class Text(str):
        pass

    error = ProviderDataError(Text("index_type"), "unusable", ticker="AAPL", timeframe=Timeframe.D1)

    assert error.kind == "index_type"
    assert type(error.kind) is str


@pytest.mark.parametrize("kind", ["a", "_", "index_type", "a" * 32, "missing_column"])
def test_provider_data_error_accepts_kinds_matching_the_pattern(kind: str) -> None:
    assert ProviderDataError(kind, "unusable").kind == kind


@pytest.mark.parametrize(
    "kind",
    ["", "a" * 33, "Index_type", "index-type", "index type", "index1", "\u00edndex", "kind\n"],
)
def test_provider_data_error_rejects_kinds_outside_the_pattern(kind: str) -> None:
    with pytest.raises(ValueError, match="kind"):
        ProviderDataError(kind, "unusable")


def test_provider_data_error_rejects_a_non_str_kind() -> None:
    with pytest.raises(TypeError):
        ProviderDataError(None, "unusable")  # type: ignore[arg-type]


def test_candle_not_published_error_converts_labels_to_stdlib_utc() -> None:
    error = CandleNotPublishedError(
        UnpublishedReason.INVALID,
        ticker=" aapl",
        timeframe=Timeframe.D1,
        expected_label=pd.Timestamp("2024-07-05 00:00", tz="America/New_York"),
        last_label=pd.Timestamp("2024-07-03 04:00", tz="UTC"),
    )

    assert error.reason is UnpublishedReason.INVALID
    assert error.ticker == "AAPL"
    assert error.timeframe is Timeframe.D1
    assert error.expected_label == EXPECTED
    assert type(error.expected_label) is datetime
    assert error.last_label == LAST
    assert type(error.last_label) is datetime


def test_candle_not_published_error_accepts_no_last_label() -> None:
    error = CandleNotPublishedError(
        UnpublishedReason.MISSING,
        ticker="AAPL",
        timeframe=Timeframe.D1,
        expected_label=EXPECTED,
        last_label=None,
    )

    assert error.last_label is None


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        ({"reason": "missing"}, TypeError),
        ({"ticker": None}, TypeError),
        ({"ticker": "A B"}, ValueError),
        ({"timeframe": None}, TypeError),
        ({"timeframe": "1d"}, TypeError),
        ({"expected_label": "2024-07-05T04:00Z"}, TypeError),
        ({"expected_label": datetime(2024, 7, 5, 4, 0)}, ValueError),
        ({"last_label": datetime(2024, 7, 3, 4, 0)}, ValueError),
        ({"last_label": pd.Timestamp("2024-07-03 04:00:00.000000001", tz="UTC")}, ValueError),
    ],
)
def test_candle_not_published_error_validates_its_fields(
    overrides: dict[str, object], error_type: type[Exception]
) -> None:
    arguments: dict[str, object] = {
        "reason": UnpublishedReason.MISSING,
        "ticker": "AAPL",
        "timeframe": Timeframe.D1,
        "expected_label": EXPECTED,
        "last_label": LAST,
    }
    arguments.update(overrides)
    reason = arguments.pop("reason")

    with pytest.raises(error_type):
        CandleNotPublishedError(reason, **arguments)  # type: ignore[arg-type]


def test_provider_unavailable_error_keeps_failure_and_retry_after() -> None:
    error = ProviderUnavailableError(
        ProviderFailure.RATE_LIMITED,
        ticker="spy",
        timeframe=Timeframe.H1,
        retry_after=timedelta(seconds=30),
    )

    assert error.failure is ProviderFailure.RATE_LIMITED
    assert error.retry_after == timedelta(seconds=30)
    assert error.ticker == "SPY"
    assert error.timeframe is Timeframe.H1


def test_provider_unavailable_error_defaults() -> None:
    error = ProviderUnavailableError(ProviderFailure.CONNECTION)

    assert error.retry_after is None
    assert error.ticker is None
    assert error.timeframe is None


def test_retry_after_may_be_zero() -> None:
    assert ProviderUnavailableError(
        ProviderFailure.RATE_LIMITED, retry_after=timedelta(0)
    ).retry_after == timedelta(0)


@pytest.mark.parametrize(
    ("retry_after", "error_type"),
    [(timedelta(microseconds=-1), ValueError), (30, TypeError), (30.0, TypeError)],
)
def test_retry_after_must_be_a_non_negative_timedelta(
    retry_after: object, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type):
        ProviderUnavailableError(ProviderFailure.RATE_LIMITED, retry_after=retry_after)  # type: ignore[arg-type]


def test_provider_unavailable_error_rejects_a_failure_that_is_not_a_member() -> None:
    with pytest.raises(TypeError):
        ProviderUnavailableError("timeout")  # type: ignore[arg-type]


# --- Messages -----------------------------------------------------------------------------------

MESSAGE_BUILDERS = [
    pytest.param(lambda message: MarketDataError(message), id="market-data-error"),
    pytest.param(lambda message: NoDataError(message, ticker="AAPL"), id="no-data"),
    pytest.param(
        lambda message: InvalidTickerError(InvalidTickerReason.NOT_FOUND, message),
        id="invalid-ticker",
    ),
    pytest.param(lambda message: ProviderDataError("index_type", message), id="provider-data"),
]


@pytest.mark.parametrize("build", MESSAGE_BUILDERS)
@pytest.mark.parametrize(
    "message",
    ["line one\nline two", "carriage\rreturn", "trailing newline\n", "x" * 301],
)
def test_a_multi_line_or_overlong_message_is_rejected(build: object, message: str) -> None:
    with pytest.raises(ValueError, match="message") as caught:
        build(message)  # type: ignore[operator]

    assert message not in str(caught.value)


@pytest.mark.parametrize("build", MESSAGE_BUILDERS)
def test_a_message_of_exactly_300_characters_is_accepted(build: object) -> None:
    error = build("x" * 300)  # type: ignore[operator]

    assert "x" * 300 in str(error)


@pytest.mark.parametrize("build", MESSAGE_BUILDERS)
def test_a_non_str_message_raises_type_error(build: object) -> None:
    with pytest.raises(TypeError):
        build(b"bytes")  # type: ignore[operator]


def test_invalid_ticker_error_message_names_the_reason_and_the_ticker() -> None:
    error = InvalidTickerError(
        InvalidTickerReason.UNSUPPORTED_ASSET_TYPE,
        "asset type index is not supported",
        ticker="^gspc",
    )

    assert str(error) == "unsupported_asset_type: asset type index is not supported [ticker=^GSPC]"


def test_invalid_ticker_error_message_without_a_ticker() -> None:
    error = InvalidTickerError(InvalidTickerReason.MALFORMED, "invalid ticker 'A B'")

    assert str(error) == "malformed: invalid ticker 'A B'"


def test_no_data_error_message_names_ticker_timeframe_and_the_callers_iso_now() -> None:
    error = NoDataError(
        "no closed candles at 2024-07-05T20:00:30+00:00", ticker="aapl", timeframe=Timeframe.D1
    )

    assert (
        str(error) == "no closed candles at 2024-07-05T20:00:30+00:00 [ticker=AAPL, timeframe=1d]"
    )


def test_provider_data_error_message_names_kind_ticker_and_timeframe() -> None:
    error = ProviderDataError(
        "index_type",
        "the provider response cannot be normalized",
        ticker="AAPL",
        timeframe=Timeframe.D1,
    )

    assert str(error) == (
        "index_type: the provider response cannot be normalized [ticker=AAPL, timeframe=1d]"
    )


def test_candle_not_published_error_message_for_a_missing_candle() -> None:
    error = CandleNotPublishedError(
        UnpublishedReason.MISSING,
        ticker="AAPL",
        timeframe=Timeframe.D1,
        expected_label=EXPECTED,
        last_label=LAST,
    )

    assert str(error) == (
        "missing: the provider has no row for the last closed candle [ticker=AAPL, "
        "timeframe=1d, expected_label=2024-07-05T04:00:00+00:00, "
        "last_label=2024-07-03T04:00:00+00:00]"
    )


def test_candle_not_published_error_message_for_an_invalid_candle_without_a_last_label() -> None:
    error = CandleNotPublishedError(
        UnpublishedReason.INVALID,
        ticker="AAPL",
        timeframe=Timeframe.H1,
        expected_label=EXPECTED,
        last_label=None,
    )

    assert str(error) == (
        "invalid: the provider row for the last closed candle is invalid [ticker=AAPL, "
        "timeframe=1h, expected_label=2024-07-05T04:00:00+00:00, last_label=none]"
    )


def test_provider_unavailable_error_message_with_every_field() -> None:
    error = ProviderUnavailableError(
        ProviderFailure.RATE_LIMITED,
        ticker="AAPL",
        timeframe=Timeframe.H1,
        retry_after=timedelta(seconds=30),
    )

    assert str(error) == (
        "rate_limited: the market data provider is unavailable "
        "[ticker=AAPL, timeframe=1h, retry_after=30.0s]"
    )


def test_provider_unavailable_error_message_without_optional_fields() -> None:
    assert str(ProviderUnavailableError(ProviderFailure.TIMEOUT)) == (
        "timeout: the market data provider is unavailable"
    )


def test_messages_built_from_the_longest_fields_stay_single_line_and_bounded() -> None:
    ticker = "A" * 32
    errors: list[MarketDataError] = [
        NoDataError("x" * 300, ticker=ticker, timeframe=Timeframe.D1),
        InvalidTickerError(InvalidTickerReason.UNSUPPORTED_CURRENCY, "x" * 300, ticker=ticker),
        ProviderDataError("k" * 32, "x" * 300, ticker=ticker, timeframe=Timeframe.D1),
        CandleNotPublishedError(
            UnpublishedReason.INVALID,
            ticker=ticker,
            timeframe=Timeframe.D1,
            expected_label=EXPECTED,
            last_label=LAST,
        ),
        ProviderUnavailableError(
            ProviderFailure.INVALID_RESPONSE,
            ticker=ticker,
            timeframe=Timeframe.D1,
            retry_after=timedelta(days=999_999_999),
        ),
    ]

    for error in errors:
        text = str(error)
        assert "\n" not in text
        assert "\r" not in text
        assert len(text) <= 400


def test_errors_keep_the_message_in_args_for_default_rendering() -> None:
    error = NoDataError("no candles", ticker="AAPL")

    assert error.args == ("no candles [ticker=AAPL]",)
    assert repr(error) == "NoDataError('no candles [ticker=AAPL]')"
