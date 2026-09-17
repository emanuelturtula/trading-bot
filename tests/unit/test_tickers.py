"""Tests of ticker metadata and the v1 instrument policy (spec 010, T9: AC11).

``TickerInfo`` validates provider metadata, ``parse_ticker`` turns malformed text into a typed
error, and ``ensure_supported`` is the single implementation of decision D27: US-listed stocks
and ETFs quoted in USD. Ticker names and metadata here are invented examples.
"""

from __future__ import annotations

import dataclasses

import pytest

import trading_bot.data.tickers as tickers_module
from trading_bot.data.errors import InvalidTickerError, InvalidTickerReason
from trading_bot.data.tickers import (
    SUPPORTED_ASSET_TYPES,
    SUPPORTED_CURRENCY,
    AssetType,
    Exchange,
    TickerInfo,
    ensure_supported,
    parse_ticker,
)


def info(**overrides: object) -> TickerInfo:
    fields: dict[str, object] = {
        "symbol": "AAPL",
        "name": "Example Corp",
        "asset_type": AssetType.EQUITY,
        "exchange": Exchange.NASDAQ,
        "currency": "USD",
    }
    fields.update(overrides)
    return TickerInfo(**fields)  # type: ignore[arg-type]


def test_the_module_exports_exactly_the_spec_names() -> None:
    assert tickers_module.__all__ == [
        "SUPPORTED_ASSET_TYPES",
        "SUPPORTED_CURRENCY",
        "AssetType",
        "Exchange",
        "TickerInfo",
        "ensure_supported",
        "parse_ticker",
    ]


def test_enum_values_and_the_supported_sets_are_the_spec_ones() -> None:
    assert [member.value for member in AssetType] == [
        "equity",
        "etf",
        "index",
        "mutual_fund",
        "cryptocurrency",
        "currency",
        "future",
        "option",
        "other",
    ]
    assert {member.name: member.value for member in Exchange} == {
        "NYSE": "XNYS",
        "NASDAQ": "XNAS",
        "NYSE_ARCA": "ARCX",
        "NYSE_AMERICAN": "XASE",
        "CBOE_BZX": "BATS",
        "OTHER": "OTHER",
    }
    assert frozenset({AssetType.EQUITY, AssetType.ETF}) == SUPPORTED_ASSET_TYPES
    assert isinstance(SUPPORTED_ASSET_TYPES, frozenset)
    assert SUPPORTED_CURRENCY == "USD"


# --- TickerInfo (Design 6.1) ---------------------------------------------------------------------


def test_ticker_info_normalizes_symbol_and_strips_name() -> None:
    result = info(symbol=" brk-b ", name="  Example Holdings Inc.  ")

    assert result.symbol == "BRK-B"
    assert result.name == "Example Holdings Inc."
    assert type(result.name) is str


def test_ticker_info_keeps_non_ascii_letters_in_the_name_and_the_currency_case() -> None:
    result = info(name="Nestl\u00e9 Example S.A.", currency="GBp")

    assert result.name == "Nestl\u00e9 Example S.A."
    assert result.currency == "GBp"
    assert result != info(name="Nestl\u00e9 Example S.A.", currency="GBP")


def test_ticker_info_is_frozen_slotted_keyword_only_equal_by_value_and_hashable() -> None:
    first = info()
    second = info(symbol="aapl")

    assert "__slots__" in TickerInfo.__dict__
    assert all(field.kw_only for field in dataclasses.fields(TickerInfo))
    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second, info(symbol="MSFT")}) == 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.name = "Other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        ({"symbol": 42}, TypeError),
        ({"symbol": "A B"}, ValueError),
        ({"symbol": ""}, ValueError),
        ({"name": None}, TypeError),
        ({"name": b"Example"}, TypeError),
        ({"asset_type": "equity"}, TypeError),
        ({"asset_type": None}, TypeError),
        ({"exchange": "XNAS"}, TypeError),
        ({"exchange": None}, TypeError),
        ({"currency": None}, TypeError),
        ({"currency": 840}, TypeError),
        ({"currency": ""}, ValueError),
        ({"currency": "USDOLLARS"}, ValueError),
        ({"currency": "US1"}, ValueError),
        ({"currency": "U D"}, ValueError),
        ({"currency": " USD"}, ValueError),
        ({"currency": "\u00dcSD"}, ValueError),
    ],
)
def test_ticker_info_rejects_bad_fields(
    overrides: dict[str, object], error_type: type[Exception]
) -> None:
    with pytest.raises(error_type):
        info(**overrides)


def test_a_currency_of_eight_letters_is_accepted() -> None:
    assert info(currency="ABCDEFGH").currency == "ABCDEFGH"


def test_a_name_of_120_characters_is_accepted_after_stripping() -> None:
    assert info(name=" " + "n" * 120 + "\t").name == "n" * 120


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("n" * 121, id="121-characters"),
        pytest.param("Example\nCorp", id="line-break"),
        pytest.param("Example\tCorp", id="tab"),
        pytest.param("Example\x07Corp", id="control"),
        pytest.param("Example\u200bCorp", id="zero-width-space"),
        pytest.param("Example\u202eCorp", id="bidi-override"),
        pytest.param("Example\u00a0Corp", id="no-break-space"),
    ],
)
def test_ticker_info_rejects_names_that_are_not_1_to_120_printable_characters(name: str) -> None:
    with pytest.raises(ValueError, match="name") as caught:
        info(name=name)

    message = str(caught.value)
    assert "Example" not in message
    assert "nnnn" not in message
    assert "\n" not in message


# --- parse_ticker ---------------------------------------------------------------------------------


def test_parse_ticker_returns_the_normalized_symbol() -> None:
    assert parse_ticker("  spy ") == "SPY"


@pytest.mark.parametrize("text", ["A B", "", "   ", "A|B", "T" * 33, "caf\u00e9", "X\nY"])
def test_parse_ticker_turns_malformed_text_into_invalid_ticker_error_from_none(text: str) -> None:
    with pytest.raises(InvalidTickerError) as caught:
        parse_ticker(text)

    error = caught.value
    assert error.reason is InvalidTickerReason.MALFORMED
    assert error.ticker is None
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    message = str(error)
    assert message.startswith("malformed: invalid ticker ")
    assert "\n" not in message
    assert len(message) < 300


def test_parse_ticker_echoes_the_bounded_repr_of_normalize_tickers_message() -> None:
    with pytest.raises(InvalidTickerError) as caught:
        parse_ticker("A B")

    assert str(caught.value) == (
        "malformed: invalid ticker 'A B': expected 1-32 printable ASCII characters without "
        "whitespace or '|'"
    )


def test_parse_ticker_bounds_the_echo_of_long_text() -> None:
    with pytest.raises(InvalidTickerError) as caught:
        parse_ticker("\x00" * 10_000)

    assert len(str(caught.value)) < 300


@pytest.mark.parametrize("text", [None, 42, b"AAPL"])
def test_parse_ticker_rejects_a_non_str_with_type_error(text: object) -> None:
    with pytest.raises(TypeError):
        parse_ticker(text)  # type: ignore[arg-type]


# --- ensure_supported (Design 6.2) ---------------------------------------------------------------


@pytest.mark.parametrize(
    "supported",
    [
        pytest.param(
            info(symbol="AAPL", asset_type=AssetType.EQUITY, exchange=Exchange.NASDAQ), id="AAPL"
        ),
        pytest.param(
            info(symbol="SPY", asset_type=AssetType.ETF, exchange=Exchange.NYSE_ARCA), id="SPY"
        ),
        pytest.param(
            info(symbol="BRK-B", asset_type=AssetType.EQUITY, exchange=Exchange.NYSE), id="BRK-B"
        ),
        pytest.param(info(symbol="XMPL", exchange=Exchange.NYSE_AMERICAN), id="nyse-american"),
        pytest.param(
            info(symbol="XMPL", asset_type=AssetType.ETF, exchange=Exchange.CBOE_BZX), id="cboe"
        ),
    ],
)
def test_ensure_supported_returns_the_same_object_for_supported_instruments(
    supported: TickerInfo,
) -> None:
    assert ensure_supported(supported) is supported


@pytest.mark.parametrize(
    ("instrument", "reason", "message"),
    [
        pytest.param(
            info(symbol="^GSPC", asset_type=AssetType.INDEX, exchange=Exchange.OTHER),
            InvalidTickerReason.UNSUPPORTED_ASSET_TYPE,
            "unsupported_asset_type: asset type index is not supported; supported asset types are "
            "equity and etf [ticker=^GSPC]",
            id="index",
        ),
        pytest.param(
            info(symbol="VFIAX", asset_type=AssetType.MUTUAL_FUND, exchange=Exchange.NASDAQ),
            InvalidTickerReason.UNSUPPORTED_ASSET_TYPE,
            "unsupported_asset_type: asset type mutual_fund is not supported; supported asset "
            "types are equity and etf [ticker=VFIAX]",
            id="mutual-fund",
        ),
        pytest.param(
            info(symbol="BTC-USD", asset_type=AssetType.CRYPTOCURRENCY, exchange=Exchange.OTHER),
            InvalidTickerReason.UNSUPPORTED_ASSET_TYPE,
            "unsupported_asset_type: asset type cryptocurrency is not supported; supported asset "
            "types are equity and etf [ticker=BTC-USD]",
            id="cryptocurrency",
        ),
        pytest.param(
            info(symbol="EURUSD=X", asset_type=AssetType.CURRENCY, exchange=Exchange.OTHER),
            InvalidTickerReason.UNSUPPORTED_ASSET_TYPE,
            "unsupported_asset_type: asset type currency is not supported; supported asset types "
            "are equity and etf [ticker=EURUSD=X]",
            id="currency",
        ),
        pytest.param(
            info(symbol="ES=F", asset_type=AssetType.FUTURE, exchange=Exchange.OTHER),
            InvalidTickerReason.UNSUPPORTED_ASSET_TYPE,
            "unsupported_asset_type: asset type future is not supported; supported asset types "
            "are equity and etf [ticker=ES=F]",
            id="future",
        ),
        pytest.param(
            info(symbol="XMPLF", asset_type=AssetType.EQUITY, exchange=Exchange.OTHER),
            InvalidTickerReason.UNSUPPORTED_EXCHANGE,
            "unsupported_exchange: exchange OTHER is not supported; supported exchanges are XNYS, "
            "XNAS, ARCX, XASE and BATS [ticker=XMPLF]",
            id="otc-equity-in-usd",
        ),
        pytest.param(
            info(
                symbol="RELIANCE.NS",
                asset_type=AssetType.EQUITY,
                exchange=Exchange.OTHER,
                currency="INR",
            ),
            InvalidTickerReason.UNSUPPORTED_EXCHANGE,
            "unsupported_exchange: exchange OTHER is not supported; supported exchanges are XNYS, "
            "XNAS, ARCX, XASE and BATS [ticker=RELIANCE.NS]",
            id="exchange-wins-over-currency",
        ),
        pytest.param(
            info(
                symbol="XMPL", asset_type=AssetType.ETF, exchange=Exchange.NYSE_ARCA, currency="EUR"
            ),
            InvalidTickerReason.UNSUPPORTED_CURRENCY,
            "unsupported_currency: currency EUR is not supported; the supported currency is USD "
            "[ticker=XMPL]",
            id="etf-in-eur",
        ),
        pytest.param(
            info(symbol="XMPL", currency="usd"),
            InvalidTickerReason.UNSUPPORTED_CURRENCY,
            "unsupported_currency: currency usd is not supported; the supported currency is USD "
            "[ticker=XMPL]",
            id="currency-case-matters",
        ),
    ],
)
def test_ensure_supported_rejects_with_the_first_failing_check(
    instrument: TickerInfo, reason: InvalidTickerReason, message: str
) -> None:
    with pytest.raises(InvalidTickerError) as caught:
        ensure_supported(instrument)

    assert caught.value.reason is reason
    assert caught.value.ticker == instrument.symbol
    assert str(caught.value) == message


@pytest.mark.parametrize(
    "asset_type",
    [member for member in AssetType if member not in (AssetType.EQUITY, AssetType.ETF)],
)
def test_every_other_asset_type_is_unsupported_even_on_a_supported_exchange(
    asset_type: AssetType,
) -> None:
    with pytest.raises(InvalidTickerError) as caught:
        ensure_supported(info(asset_type=asset_type, exchange=Exchange.NYSE))

    assert caught.value.reason is InvalidTickerReason.UNSUPPORTED_ASSET_TYPE


def test_ensure_supported_rejects_a_non_ticker_info_with_type_error() -> None:
    with pytest.raises(TypeError):
        ensure_supported({"symbol": "AAPL"})  # type: ignore[arg-type]


def test_ensure_supported_messages_never_echo_the_name() -> None:
    with pytest.raises(InvalidTickerError) as caught:
        ensure_supported(info(name="Secret Name Example", asset_type=AssetType.INDEX))

    assert "Secret Name Example" not in str(caught.value)
