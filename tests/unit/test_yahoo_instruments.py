"""Tests of the Yahoo types, symbols and metadata mapping (spec 011, T4, AC9-AC11).

AC9: ``HistoryQuery``, ``ChartMetadata``, ``YahooHistory`` and the ``YahooClient`` port
(Design 6), including the validation order and a metadata mapping whose iteration and lazy
``tradingPeriods`` lookup raise. AC10: ``check_yahoo_symbol`` (Design 7.1). AC11: the mapping
tables and ``ticker_info`` (Design 7.2 and 7.3), on the recorded metadata and on synthetic rows.
Expectations are literals from the spec tables or values read from the recording file.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from types import MappingProxyType

import pandas as pd
import pytest

import trading_bot.data.yahoo.history as history_module
import trading_bot.data.yahoo.instruments as instruments_module
from tests.fixtures.calendars import NEW_YORK, utc
from tests.fixtures.yahoo_recordings import load_metadata
from trading_bot.data.errors import InvalidTickerError, InvalidTickerReason, ProviderDataError
from trading_bot.data.tickers import AssetType, Exchange, TickerInfo
from trading_bot.data.yahoo.history import (
    ISIN_PATTERN,
    YAHOO_SYMBOL_PATTERN,
    ChartMetadata,
    HistoryQuery,
    YahooHistory,
)
from trading_bot.data.yahoo.instruments import (
    ASSET_TYPES,
    EXCHANGES,
    check_yahoo_symbol,
    ticker_info,
)

# --- AC9: history types ----------------------------------------------------------------------


def test_the_history_module_exports_exactly_the_spec_names() -> None:
    assert history_module.__all__ == [
        "ISIN_PATTERN",
        "YAHOO_SYMBOL_PATTERN",
        "ChartMetadata",
        "HistoryQuery",
        "YahooClient",
        "YahooHistory",
        "YahooInterval",
    ]


def test_the_symbol_patterns_are_the_spec_expressions() -> None:
    assert YAHOO_SYMBOL_PATTERN.pattern == r"[A-Z0-9^][A-Z0-9.^=-]{0,23}"
    assert ISIN_PATTERN.pattern == r"[A-Z]{2}[A-Z0-9]{9}[0-9]"


def test_a_start_query_stores_a_stdlib_utc_start() -> None:
    query = HistoryQuery(
        symbol="SPY", interval="1h", start=pd.Timestamp("2025-11-24T09:00", tz=NEW_YORK)
    )

    assert query.start == utc("2025-11-24T14:00")
    assert type(query.start) is datetime
    assert query.start.tzinfo is UTC
    assert query.period is None


def test_a_period_query_has_no_start() -> None:
    query = HistoryQuery(symbol="SPY", interval="1d", period="1mo")

    assert query.start is None
    assert query.period == "1mo"


def test_history_query_is_frozen_keyword_only_equal_by_value_and_hashable() -> None:
    first = HistoryQuery(symbol="SPY", interval="1d", period="1mo")
    second = HistoryQuery(symbol="SPY", interval="1d", period="1mo")

    assert first == second
    assert hash(first) == hash(second)
    assert first != HistoryQuery(symbol="SPY", interval="1h", period="1mo")
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.symbol = "QQQ"  # type: ignore[misc]
    with pytest.raises(TypeError):
        HistoryQuery("SPY", "1d", None, "1mo")  # type: ignore[misc]


def test_a_non_str_symbol_raises_type_error_first() -> None:
    with pytest.raises(TypeError, match="symbol"):
        HistoryQuery(symbol=42, interval="5m", start=None, period=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("symbol", ["spy", "SPY/1", ".SPY", "", "A" * 25, "US0378331005"])
def test_an_invalid_symbol_raises_value_error_before_the_interval(symbol: str) -> None:
    with pytest.raises(ValueError, match="symbol"):
        HistoryQuery(symbol=symbol, interval="5m", start=None, period=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("interval", ["5m", "4h", "1H", 1])
def test_an_invalid_interval_raises_value_error_before_start_and_period(interval: object) -> None:
    with pytest.raises(ValueError, match="interval"):
        HistoryQuery(symbol="SPY", interval=interval, start=None, period=None)  # type: ignore[arg-type]


def test_exactly_one_of_start_and_period_is_required_before_start_is_converted() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        HistoryQuery(symbol="SPY", interval="1d")
    with pytest.raises(ValueError, match="exactly one"):
        HistoryQuery(
            symbol="SPY",
            interval="1d",
            start=datetime(2025, 11, 24),  # naive, but the count is checked first
            period="1mo",
        )


def test_start_goes_through_to_utc() -> None:
    with pytest.raises(ValueError, match="naive"):
        HistoryQuery(symbol="SPY", interval="1d", start=datetime(2025, 11, 24))
    with pytest.raises(TypeError, match="datetime"):
        HistoryQuery(symbol="SPY", interval="1d", start="2025-11-24")  # type: ignore[arg-type]


@pytest.mark.parametrize("period", ["2mo", "max", 1])
def test_a_period_other_than_one_month_raises_value_error(period: object) -> None:
    with pytest.raises(ValueError, match="period"):
        HistoryQuery(symbol="SPY", interval="1d", period=period)  # type: ignore[arg-type]


class _HostileMetadata(Mapping[str, object]):
    """A mapping whose iteration, keys, items and ``tradingPeriods`` lookup raise."""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values
        self.gets: list[str] = []

    def __getitem__(self, key: str) -> object:
        if key == "tradingPeriods":
            raise AssertionError("tradingPeriods must never be read")
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("the metadata must never be iterated")

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> Iterator[str]:  # type: ignore[override]
        raise AssertionError("keys must never be called")

    def items(self) -> Iterator[tuple[str, object]]:  # type: ignore[override]
        raise AssertionError("items must never be called")

    def get(self, key: str, default: object = None) -> object:
        self.gets.append(key)
        return self._values.get(key, default)


def test_from_mapping_reads_only_its_six_keys_through_get() -> None:
    metadata = _HostileMetadata(
        {
            "symbol": "SPY",
            "instrumentType": "ETF",
            "exchangeName": "PCX",
            "currency": "USD",
            "longName": "A long name",
            "shortName": "A short name",
            "regularMarketPrice": 1.0,
        }
    )

    result = ChartMetadata.from_mapping(metadata)

    assert result == ChartMetadata(
        symbol="SPY",
        instrument_type="ETF",
        exchange_name="PCX",
        currency="USD",
        long_name="A long name",
        short_name="A short name",
    )
    assert metadata.gets == [
        "symbol",
        "instrumentType",
        "exchangeName",
        "currency",
        "longName",
        "shortName",
    ]


def test_from_mapping_keeps_only_str_values() -> None:
    result = ChartMetadata.from_mapping(
        {"symbol": 42, "instrumentType": None, "exchangeName": ["NMS"], "currency": b"USD"}
    )

    assert result == ChartMetadata(
        symbol=None,
        instrument_type=None,
        exchange_name=None,
        currency=None,
        long_name=None,
        short_name=None,
    )


def test_from_mapping_rejects_a_non_mapping() -> None:
    with pytest.raises(TypeError, match="Mapping"):
        ChartMetadata.from_mapping([("symbol", "SPY")])  # type: ignore[arg-type]


def test_yahoo_history_holds_the_frame_as_given_and_compares_by_identity() -> None:
    frame = pd.DataFrame({"Close": [1.0]})
    metadata = ChartMetadata.from_mapping({"symbol": "SPY"})
    first = YahooHistory(frame=frame, metadata=metadata)

    assert first.frame is frame
    assert first.metadata is metadata
    assert first != YahooHistory(frame=frame, metadata=metadata)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.frame = frame  # type: ignore[misc]


# --- AC10: check_yahoo_symbol (Design 7.1) ----------------------------------------------------


def test_the_instruments_module_exports_exactly_the_spec_names() -> None:
    assert instruments_module.__all__ == [
        "ASSET_TYPES",
        "EXCHANGES",
        "check_yahoo_symbol",
        "ticker_info",
    ]


@pytest.mark.parametrize(
    "symbol",
    [
        "SPY",
        "BRK-B",
        "^GSPC",
        "EURUSD=X",
        "ES=F",
        "RELIANCE.NS",
        "BTC-USD",
        "A",
        "ABCDEFGHIJKLMNOPQRSTUVWX",
        "US037833100A",
    ],
)
def test_valid_symbols_are_returned_unchanged(symbol: str) -> None:
    assert check_yahoo_symbol(symbol) is symbol


@pytest.mark.parametrize(
    ("symbol", "ticker"),
    [
        ("ABCDEFGHIJKLMNOPQRSTUVWXY", "ABCDEFGHIJKLMNOPQRSTUVWXY"),
        (".SPY", ".SPY"),
        ("-SPY", "-SPY"),
        ("=X", "=X"),
        ("M&M.NS", "M&M.NS"),
        ("SPY/1", "SPY/1"),
        ("SPY?X", "SPY?X"),
        ("SPY#1", "SPY#1"),
        ("SPY%20", "SPY%20"),
        ("spy", None),
    ],
)
def test_malformed_symbols_raise_malformed(symbol: str, ticker: str | None) -> None:
    with pytest.raises(InvalidTickerError) as caught:
        check_yahoo_symbol(symbol)

    assert caught.value.reason is InvalidTickerReason.MALFORMED
    assert caught.value.ticker == ticker
    assert str(caught.value).startswith("malformed: ")
    if ticker is not None:
        assert str(caught.value).endswith(f"[ticker={ticker}]")


def test_an_isin_shaped_symbol_is_not_found_without_io() -> None:
    with pytest.raises(InvalidTickerError) as caught:
        check_yahoo_symbol("US0378331005")

    assert caught.value.reason is InvalidTickerReason.NOT_FOUND
    assert caught.value.ticker == "US0378331005"
    assert str(caught.value).startswith("not_found: ")
    assert str(caught.value).endswith("[ticker=US0378331005]")


def test_an_unnormalizable_symbol_has_no_ticker_and_is_not_echoed() -> None:
    with pytest.raises(InvalidTickerError) as caught:
        check_yahoo_symbol("bad symbol|\n" + "x" * 40)

    assert caught.value.ticker is None
    assert "bad symbol" not in str(caught.value)
    assert "\n" not in str(caught.value)


def test_a_non_str_symbol_raises_type_error() -> None:
    with pytest.raises(TypeError):
        check_yahoo_symbol(42)  # type: ignore[arg-type]


# --- AC11: mapping tables (Design 7.2) ------------------------------------------------------


def test_asset_types_is_exactly_the_read_only_spec_table() -> None:
    assert isinstance(ASSET_TYPES, MappingProxyType)
    assert dict(ASSET_TYPES) == {
        "EQUITY": AssetType.EQUITY,
        "ETF": AssetType.ETF,
        "INDEX": AssetType.INDEX,
        "MUTUALFUND": AssetType.MUTUAL_FUND,
        "CRYPTOCURRENCY": AssetType.CRYPTOCURRENCY,
        "CURRENCY": AssetType.CURRENCY,
        "FUTURE": AssetType.FUTURE,
        "OPTION": AssetType.OPTION,
    }
    with pytest.raises(TypeError):
        ASSET_TYPES["ECNQUOTE"] = AssetType.OTHER  # type: ignore[index]


def test_exchanges_is_exactly_the_read_only_spec_table() -> None:
    assert isinstance(EXCHANGES, MappingProxyType)
    assert dict(EXCHANGES) == {
        "NYQ": Exchange.NYSE,
        "NMS": Exchange.NASDAQ,
        "NGM": Exchange.NASDAQ,
        "NCM": Exchange.NASDAQ,
        "PCX": Exchange.NYSE_ARCA,
        "ASE": Exchange.NYSE_AMERICAN,
        "BTS": Exchange.CBOE_BZX,
    }
    with pytest.raises(TypeError):
        EXCHANGES["PNK"] = Exchange.OTHER  # type: ignore[index]


# --- AC11: ticker_info on the recorded metadata (Design 7.3) ------------------------------------

RECORDED_TABLE: tuple[tuple[str, AssetType, Exchange, str, str], ...] = (
    ("AAPL", AssetType.EQUITY, Exchange.NASDAQ, "USD", "longName"),
    ("QQQ", AssetType.ETF, Exchange.NASDAQ, "USD", "longName"),
    ("BRK-B", AssetType.EQUITY, Exchange.NYSE, "USD", "longName"),
    ("SPY", AssetType.ETF, Exchange.NYSE_ARCA, "USD", "longName"),
    ("UEC", AssetType.EQUITY, Exchange.NYSE_AMERICAN, "USD", "longName"),
    ("ARKB", AssetType.ETF, Exchange.CBOE_BZX, "USD", "longName"),
    ("^GSPC", AssetType.INDEX, Exchange.OTHER, "USD", "longName"),
    ("VFIAX", AssetType.MUTUAL_FUND, Exchange.OTHER, "USD", "longName"),
    ("BTC-USD", AssetType.CRYPTOCURRENCY, Exchange.OTHER, "USD", "longName"),
    ("EURUSD=X", AssetType.CURRENCY, Exchange.OTHER, "USD", "longName"),
    ("ES=F", AssetType.FUTURE, Exchange.OTHER, "USD", "shortName"),
    ("RELIANCE.NS", AssetType.EQUITY, Exchange.OTHER, "INR", "longName"),
    ("TCEHY", AssetType.EQUITY, Exchange.OTHER, "USD", "longName"),
    ("NSRGY", AssetType.EQUITY, Exchange.OTHER, "USD", "longName"),
    ("SHOP.TO", AssetType.EQUITY, Exchange.OTHER, "CAD", "longName"),
)


@pytest.mark.parametrize(
    ("symbol", "asset_type", "exchange", "currency", "name_key"),
    RECORDED_TABLE,
    ids=[row[0] for row in RECORDED_TABLE],
)
def test_ticker_info_maps_the_recorded_metadata(
    symbol: str, asset_type: AssetType, exchange: Exchange, currency: str, name_key: str
) -> None:
    recorded = load_metadata()[symbol]

    info = ticker_info(symbol, ChartMetadata.from_mapping(recorded))

    name = recorded[name_key]
    assert isinstance(name, str)
    assert info == TickerInfo(
        symbol=symbol,
        name=name.strip(),
        asset_type=asset_type,
        exchange=exchange,
        currency=currency,
    )


def test_the_recorded_futures_metadata_has_no_long_name() -> None:
    assert load_metadata()["ES=F"]["longName"] is None


def test_the_recorded_nsrgy_name_is_non_ascii_and_kept() -> None:
    recorded = load_metadata()["NSRGY"]
    info = ticker_info("NSRGY", ChartMetadata.from_mapping(recorded))

    assert not info.name.isascii()
    assert info.name == recorded["longName"]


# --- AC11: ticker_info on synthetic rows (Design 7.3) --------------------------------------------


def aapl_metadata(**changes: object) -> ChartMetadata:
    """The recorded AAPL metadata with changes; a value of ``...`` removes the key."""
    values = dict(load_metadata()["AAPL"])
    for key, value in changes.items():
        if value is ...:
            values.pop(key, None)
        else:
            values[key] = value
    return ChartMetadata.from_mapping(values)


def aapl_long_name() -> str:
    name = load_metadata()["AAPL"]["longName"]
    assert isinstance(name, str)
    return name


def aapl_short_name() -> str:
    name = load_metadata()["AAPL"]["shortName"]
    assert isinstance(name, str)
    return name


def test_exchange_ncm_maps_to_nasdaq() -> None:
    assert ticker_info("AAPL", aapl_metadata(exchangeName="NCM")).exchange is Exchange.NASDAQ


@pytest.mark.parametrize("value", [" NMS", ..., 42, "nms"])
def test_unknown_or_missing_exchange_codes_map_to_other(value: object) -> None:
    assert ticker_info("AAPL", aapl_metadata(exchangeName=value)).exchange is Exchange.OTHER


@pytest.mark.parametrize("value", ["ECNQUOTE", "equity", ...])
def test_unknown_or_missing_instrument_types_map_to_other(value: object) -> None:
    assert ticker_info("AAPL", aapl_metadata(instrumentType=value)).asset_type is AssetType.OTHER


@pytest.mark.parametrize("value", [..., "AAPL|X"])
def test_a_missing_or_invalid_metadata_symbol_is_invalid_metadata(value: object) -> None:
    with pytest.raises(ProviderDataError) as caught:
        ticker_info("AAPL", aapl_metadata(symbol=value))

    assert caught.value.kind == "invalid_metadata"
    assert caught.value.ticker == "AAPL"
    assert "AAPL|X" not in str(caught.value)


def test_another_metadata_symbol_is_a_symbol_mismatch() -> None:
    with pytest.raises(ProviderDataError) as caught:
        ticker_info("AAPL", aapl_metadata(symbol="MSFT"))

    assert caught.value.kind == "symbol_mismatch"
    assert caught.value.ticker == "AAPL"
    assert "MSFT" not in str(caught.value)


def test_a_lowercase_metadata_symbol_is_accepted() -> None:
    assert ticker_info("AAPL", aapl_metadata(symbol="aapl")).symbol == "AAPL"


@pytest.mark.parametrize("value", [..., "", "US$", "USDOLLARS", 42])
def test_a_missing_or_invalid_currency_is_invalid_metadata(value: object) -> None:
    with pytest.raises(ProviderDataError) as caught:
        ticker_info("AAPL", aapl_metadata(currency=value))

    assert caught.value.kind == "invalid_metadata"
    assert caught.value.ticker == "AAPL"


def test_a_minor_unit_currency_is_kept_as_given() -> None:
    assert ticker_info("AAPL", aapl_metadata(currency="GBp")).currency == "GBp"


@pytest.mark.parametrize("long_name", ["Apple\nInc.", "x" * 121, "   "])
def test_an_invalid_long_name_falls_back_to_the_short_name(long_name: str) -> None:
    info = ticker_info("AAPL", aapl_metadata(longName=long_name))

    assert info.name == aapl_short_name().strip()


@pytest.mark.parametrize(
    ("long_name", "short_name"),
    [(..., ...), ("   ", "\t"), (42, "x" * 121), ("a\x00b", ...)],
)
def test_without_a_valid_name_the_symbol_is_the_name(long_name: object, short_name: object) -> None:
    info = ticker_info("AAPL", aapl_metadata(longName=long_name, shortName=short_name))

    assert info.name == "AAPL"


def test_the_long_name_is_preferred_and_stripped() -> None:
    info = ticker_info("AAPL", aapl_metadata(longName="  " + aapl_long_name() + "  "))

    assert info.name == aapl_long_name().strip()


def test_ticker_info_rejects_arguments_of_the_wrong_type() -> None:
    with pytest.raises(TypeError):
        ticker_info(42, aapl_metadata())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ticker_info("AAPL", {"symbol": "AAPL"})  # type: ignore[arg-type]
