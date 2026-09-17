"""Tests of the fake provider and the shared market data fixtures (spec 010, T12-T13).

AC13: the fake conforms to ``MarketDataProvider`` (checked by strict mypy through
``as_market_data_provider``). AC16: the fake runs the real ``prepare_candles`` on stored frames,
with scripted failures and recorded calls. AC17: ``session_candles``, ``provider_shaped`` and
``assert_closed_candles``. Coroutines run with ``asyncio.run`` (decision D46). Ticker metadata is
invented.
"""

from __future__ import annotations

import ast
import asyncio
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import tests.fixtures.provider_contract as provider_contract_module
from tests.fixtures.calendars import NEW_YORK, nyse_test_calendar, toy_calendar, utc
from tests.fixtures.candles import synthetic_candles
from tests.fixtures.fake_provider import FakeMarketDataProvider, FetchCall, as_market_data_provider
from tests.fixtures.provider_contract import assert_closed_candles
from tests.fixtures.session_candles import provider_shaped, session_candles
from trading_bot.data.errors import (
    CandleNotPublishedError,
    InvalidTickerError,
    InvalidTickerReason,
    MarketDataError,
    NoDataError,
    ProviderFailure,
    ProviderUnavailableError,
    UnpublishedReason,
)
from trading_bot.data.pipeline import prepare_candles
from trading_bot.data.provider import CandleRequest
from trading_bot.data.tickers import AssetType, Exchange, TickerInfo
from trading_bot.domain.candles import OHLCV_COLUMNS, validate_candles
from trading_bot.domain.timeframe import Timeframe

H1 = Timeframe.H1
H4 = Timeframe.H4
D1 = Timeframe.D1
NYSE = nyse_test_calendar()
NOW = utc("2024-07-05T20:00:30")
CANONICAL_INDEX = pd.DatetimeTZDtype(unit="us", tz="UTC")
EXAMPLE = TickerInfo(
    symbol="XMPL",
    name="Example Corp",
    asset_type=AssetType.EQUITY,
    exchange=Exchange.NASDAQ,
    currency="USD",
)


def daily_grid() -> pd.DataFrame:
    return session_candles(NYSE, D1, utc("2024-06-03T00:00"), utc("2024-07-10T00:00"))


def provider_with_candles() -> FakeMarketDataProvider:
    provider = FakeMarketDataProvider(calendar=NYSE)
    provider.set_candles("xmpl", D1, provider_shaped(daily_grid()))
    return provider


# --- T12: the fake provider (AC13, AC16) -------------------------------------------------------


def test_the_fake_conforms_to_the_protocol_through_a_typed_function() -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)

    assert as_market_data_provider(provider) is provider


def test_the_fake_rejects_a_calendar_that_is_not_a_market_calendar() -> None:
    with pytest.raises(TypeError):
        FakeMarketDataProvider(calendar="XNYS")  # type: ignore[arg-type]


def test_fetch_candles_equals_prepare_candles_on_the_stored_frame() -> None:
    provider = provider_with_candles()
    raw = provider_shaped(daily_grid())

    result = asyncio.run(provider.fetch_candles("XMPL", D1, 5, now=NOW))

    expected = prepare_candles(
        raw, CandleRequest(ticker="XMPL", timeframe=D1, lookback=5, now=NOW), calendar=NYSE
    )
    pd.testing.assert_frame_equal(result, expected, check_exact=True)
    assert_closed_candles(
        result, CandleRequest(ticker="XMPL", timeframe=D1, lookback=5, now=NOW), calendar=NYSE
    )
    assert [label.isoformat() for label in result.index] == [
        "2024-06-28T04:00:00+00:00",
        "2024-07-01T04:00:00+00:00",
        "2024-07-02T04:00:00+00:00",
        "2024-07-03T04:00:00+00:00",
        "2024-07-05T04:00:00+00:00",
    ]


def test_fetch_calls_are_recorded_with_normalized_values() -> None:
    provider = provider_with_candles()

    asyncio.run(
        provider.fetch_candles(
            " xmpl ", D1, 3, now=pd.Timestamp("2024-07-05 16:00:30", tz=NEW_YORK)
        )
    )
    asyncio.run(provider.fetch_candles("XMPL", D1, 2, now=NOW))

    assert provider.fetch_calls == (
        FetchCall(ticker="XMPL", timeframe=D1, lookback=3, now=NOW),
        FetchCall(ticker="XMPL", timeframe=D1, lookback=2, now=NOW),
    )
    assert type(provider.fetch_calls[0].now) is datetime
    assert provider.validate_calls == ()


def test_fetch_call_is_a_frozen_keyword_only_value() -> None:
    call = FetchCall(ticker="XMPL", timeframe=D1, lookback=3, now=NOW)

    assert call == FetchCall(ticker="XMPL", timeframe=D1, lookback=3, now=NOW)
    assert hash(call) == hash(FetchCall(ticker="XMPL", timeframe=D1, lookback=3, now=NOW))
    with pytest.raises(TypeError):
        FetchCall("XMPL", D1, 3, NOW)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    [
        pytest.param(("XMPL", D1, 0, NOW), ValueError, id="lookback-zero"),
        pytest.param(("XMPL", "1d", 3, NOW), TypeError, id="timeframe-code"),
        pytest.param(("XMPL", D1, 3, datetime(2024, 7, 5)), ValueError, id="naive-now"),
        pytest.param(("A B", D1, 3, NOW), InvalidTickerError, id="malformed-ticker"),
    ],
)
def test_invalid_fetch_arguments_raise_before_recording(
    arguments: tuple[object, object, object, object], error_type: type[Exception]
) -> None:
    provider = provider_with_candles()
    ticker, timeframe, lookback, now = arguments

    with pytest.raises(error_type):
        asyncio.run(provider.fetch_candles(ticker, timeframe, lookback, now=now))  # type: ignore[arg-type]

    assert provider.fetch_calls == ()


def test_fetch_without_a_stored_frame_raises_no_data_error() -> None:
    provider = provider_with_candles()

    with pytest.raises(NoDataError) as caught:
        asyncio.run(provider.fetch_candles("XMPL", H1, 3, now=NOW))

    assert caught.value.ticker == "XMPL"
    assert caught.value.timeframe is H1
    assert "2024-07-05T20:00:30+00:00" in str(caught.value)
    assert len(provider.fetch_calls) == 1


def test_scripted_failures_are_raised_first_in_first_out_then_the_frame_is_served() -> None:
    provider = provider_with_candles()
    first = ProviderUnavailableError(ProviderFailure.TIMEOUT, ticker="XMPL")
    second = ProviderUnavailableError(
        ProviderFailure.RATE_LIMITED, retry_after=timedelta(seconds=5)
    )
    provider.fail_next(first, ticker="xmpl", times=2)
    provider.fail_next(second, ticker="XMPL")

    raised: list[MarketDataError] = []
    for _ in range(3):
        with pytest.raises(ProviderUnavailableError) as caught:
            asyncio.run(provider.fetch_candles("XMPL", D1, 2, now=NOW))
        raised.append(caught.value)
    result = asyncio.run(provider.fetch_candles("XMPL", D1, 2, now=NOW))

    assert raised[0] is first
    assert raised[1] is first
    assert raised[2] is second
    assert len(result) == 2
    assert len(provider.fetch_calls) == 4


def test_a_failure_raised_twice_does_not_accumulate_tracebacks() -> None:
    provider = provider_with_candles()
    error = ProviderUnavailableError(ProviderFailure.CONNECTION)
    provider.fail_next(error, ticker="XMPL", times=2)

    depths: list[int] = []
    for _ in range(2):
        with pytest.raises(ProviderUnavailableError) as caught:
            asyncio.run(provider.fetch_candles("XMPL", D1, 2, now=NOW))
        depths.append(len(traceback.extract_tb(caught.value.__traceback__)))

    assert depths[0] == depths[1]


def test_scripted_failures_are_scoped_to_the_ticker_and_the_method() -> None:
    provider = provider_with_candles()
    provider.set_candles("OTHR", D1, daily_grid())
    provider.add_ticker(EXAMPLE)
    provider.fail_next(NoDataError("scripted"), ticker="OTHR")
    provider.fail_next(
        ProviderUnavailableError(ProviderFailure.SERVER_ERROR),
        ticker="XMPL",
        method="validate_ticker",
    )

    assert len(asyncio.run(provider.fetch_candles("XMPL", D1, 2, now=NOW))) == 2
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.validate_ticker("XMPL"))
    with pytest.raises(NoDataError, match="scripted"):
        asyncio.run(provider.fetch_candles("OTHR", D1, 2, now=NOW))
    assert asyncio.run(provider.validate_ticker("XMPL")) is EXAMPLE


def test_scripted_failures_are_raised_before_the_stored_frame_is_looked_up() -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)
    provider.fail_next(ProviderUnavailableError(ProviderFailure.TIMEOUT), ticker="NONE")

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.fetch_candles("NONE", D1, 2, now=NOW))
    with pytest.raises(NoDataError):
        asyncio.run(provider.fetch_candles("NONE", D1, 2, now=NOW))


@pytest.mark.parametrize(
    ("kwargs", "error_type"),
    [
        pytest.param({"error": ValueError("not a market data error")}, TypeError, id="error-type"),
        pytest.param({"times": 0}, ValueError, id="times-zero"),
        pytest.param({"times": -1}, ValueError, id="times-negative"),
        pytest.param({"times": True}, TypeError, id="times-bool"),
        pytest.param({"times": 1.0}, TypeError, id="times-float"),
        pytest.param({"method": "fetch"}, ValueError, id="method-unknown"),
        pytest.param({"ticker": "A B"}, ValueError, id="ticker-malformed"),
        pytest.param({"ticker": None}, TypeError, id="ticker-none"),
    ],
)
def test_fail_next_validates_its_arguments(
    kwargs: dict[str, object], error_type: type[Exception]
) -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)
    arguments: dict[str, object] = {
        "error": NoDataError("scripted"),
        "ticker": "XMPL",
        "method": "fetch_candles",
        "times": 1,
    }
    arguments.update(kwargs)
    error = arguments.pop("error")

    with pytest.raises(error_type):
        provider.fail_next(error, **arguments)  # type: ignore[arg-type]


def test_replacing_the_stored_frame_publishes_a_new_candle() -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)
    grid = daily_grid()
    july_5 = pd.Timestamp("2024-07-05T04:00", tz="UTC")
    provider.set_candles("XMPL", D1, grid.drop(index=[july_5]))

    with pytest.raises(CandleNotPublishedError) as caught:
        asyncio.run(provider.fetch_candles("XMPL", D1, 2, now=NOW))
    provider.set_candles("XMPL", D1, grid)
    result = asyncio.run(provider.fetch_candles("XMPL", D1, 2, now=NOW))

    assert caught.value.reason is UnpublishedReason.MISSING
    assert caught.value.expected_label == utc("2024-07-05T04:00")
    assert result.index[-1] == july_5


def test_set_candles_stores_a_copy() -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)
    raw = provider_shaped(daily_grid())
    provider.set_candles("XMPL", D1, raw)

    raw.loc[raw.index[-3], "Close"] = np.nan

    result = asyncio.run(provider.fetch_candles("XMPL", D1, 2, now=NOW))
    assert len(result) == 2


@pytest.mark.parametrize(
    ("ticker", "timeframe", "raw", "error_type"),
    [
        pytest.param("XMPL", D1, [1.0], TypeError, id="raw-not-frame"),
        pytest.param("XMPL", "1d", None, TypeError, id="timeframe-code"),
        pytest.param("A B", D1, None, ValueError, id="ticker-malformed"),
    ],
)
def test_set_candles_validates_its_arguments(
    ticker: object, timeframe: object, raw: object, error_type: type[Exception]
) -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)
    frame = daily_grid() if raw is None else raw

    with pytest.raises(error_type):
        provider.set_candles(ticker, timeframe, frame)  # type: ignore[arg-type]


def test_validate_ticker_returns_a_supported_instrument_and_records_the_text() -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)
    provider.add_ticker(EXAMPLE)

    assert asyncio.run(provider.validate_ticker(" xmpl ")) is EXAMPLE
    assert provider.validate_calls == (" xmpl ",)
    assert provider.fetch_calls == ()


def test_validate_ticker_rejects_malformed_text_after_recording_it() -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)

    with pytest.raises(InvalidTickerError) as caught:
        asyncio.run(provider.validate_ticker("A B"))

    assert caught.value.reason is InvalidTickerReason.MALFORMED
    assert provider.validate_calls == ("A B",)


def test_validate_ticker_reports_an_unknown_symbol_as_not_found() -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)
    provider.add_ticker(EXAMPLE)

    with pytest.raises(InvalidTickerError) as caught:
        asyncio.run(provider.validate_ticker("nope"))

    assert caught.value.reason is InvalidTickerReason.NOT_FOUND
    assert caught.value.ticker == "NOPE"


def test_validate_ticker_applies_the_d27_policy() -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)
    provider.add_ticker(
        TickerInfo(
            symbol="^XMPL",
            name="Example Index",
            asset_type=AssetType.INDEX,
            exchange=Exchange.OTHER,
            currency="USD",
        )
    )

    with pytest.raises(InvalidTickerError) as caught:
        asyncio.run(provider.validate_ticker("^xmpl"))

    assert caught.value.reason is InvalidTickerReason.UNSUPPORTED_ASSET_TYPE
    assert caught.value.ticker == "^XMPL"


def test_add_ticker_replaces_an_entry_with_the_same_symbol() -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)
    provider.add_ticker(EXAMPLE)
    replacement = TickerInfo(
        symbol="xmpl",
        name="Example Corp",
        asset_type=AssetType.EQUITY,
        exchange=Exchange.OTHER,
        currency="USD",
    )
    provider.add_ticker(replacement)

    with pytest.raises(InvalidTickerError) as caught:
        asyncio.run(provider.validate_ticker("XMPL"))

    assert caught.value.reason is InvalidTickerReason.UNSUPPORTED_EXCHANGE


def test_add_ticker_rejects_a_non_ticker_info() -> None:
    with pytest.raises(TypeError):
        FakeMarketDataProvider(calendar=NYSE).add_ticker({"symbol": "XMPL"})  # type: ignore[arg-type]


@pytest.mark.parametrize("text", [None, 42, b"XMPL"])
def test_validate_ticker_rejects_a_non_str_before_recording(text: object) -> None:
    provider = FakeMarketDataProvider(calendar=NYSE)

    with pytest.raises(TypeError):
        asyncio.run(provider.validate_ticker(text))  # type: ignore[arg-type]

    assert provider.validate_calls == ()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda provider: provider.fetch_candles("XMPL", D1, 2, now=NOW), id="fetch_candles"
        ),
        pytest.param(lambda provider: provider.validate_ticker("XMPL"), id="validate_ticker"),
    ],
)
def test_each_call_yields_to_the_event_loop_exactly_once(
    call: Callable[[FakeMarketDataProvider], Awaitable[object]],
) -> None:
    provider = provider_with_candles()
    provider.add_ticker(EXAMPLE)
    events: list[str] = []

    async def other_task() -> None:
        events.append("other step 1")
        await asyncio.sleep(0)
        events.append("other step 2")

    async def scenario() -> None:
        task = asyncio.create_task(other_task())
        await call(provider)
        events.append("call returned")
        await task

    asyncio.run(scenario())

    assert events == ["other step 1", "call returned", "other step 2"]


def test_concurrent_calls_from_one_event_loop_are_all_served() -> None:
    provider = provider_with_candles()

    async def scenario() -> list[pd.DataFrame]:
        return list(
            await asyncio.gather(
                *(provider.fetch_candles("XMPL", D1, n, now=NOW) for n in (1, 2, 3))
            )
        )

    results = asyncio.run(scenario())

    assert [len(result) for result in results] == [1, 2, 3]
    assert len(provider.fetch_calls) == 3


# --- T13: shared fixtures (AC17) ---------------------------------------------------------------


@pytest.mark.parametrize("timeframe", list(Timeframe))
@pytest.mark.parametrize(
    ("calendar_name", "start", "end", "count"),
    [
        ("nyse", "2024-07-01T00:00", "2024-07-10T00:00", {H1: 39, H4: 11, D1: 6}),
        ("toy", "2024-03-06T05:00", "2024-03-15T04:00", {H1: 23, H4: 7, D1: 4}),
    ],
)
def test_session_candles_are_canonical_and_on_the_calendar_grid(
    timeframe: Timeframe, calendar_name: str, start: str, end: str, count: dict[Timeframe, int]
) -> None:
    calendar = NYSE if calendar_name == "nyse" else toy_calendar()

    frame = session_candles(calendar, timeframe, utc(start), utc(end))

    assert validate_candles(frame) is frame
    assert frame.index.dtype == CANONICAL_INDEX
    assert frame.index.name is None
    assert list(frame.columns) == list(OHLCV_COLUMNS)
    assert len(frame) == count[timeframe]
    assert list(frame.index) == [
        slot.label for slot in calendar.candle_slots(timeframe, utc(start), utc(end))
    ]


def test_session_candles_take_the_values_of_synthetic_candles() -> None:
    frame = session_candles(NYSE, H1, utc("2024-07-01T00:00"), utc("2024-07-10T00:00"), seed=7)

    expected = synthetic_candles(len(frame), seed=7, timeframe=H1)
    np.testing.assert_array_equal(frame.to_numpy(), expected.to_numpy())
    assert not np.array_equal(
        frame.to_numpy(),
        session_candles(NYSE, H1, utc("2024-07-01T00:00"), utc("2024-07-10T00:00")).to_numpy(),
    )


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_session_candles_without_slots_give_an_empty_canonical_frame(timeframe: Timeframe) -> None:
    frame = session_candles(NYSE, timeframe, utc("2024-07-06T00:00"), utc("2024-07-08T00:00"))

    assert len(frame) == 0
    assert validate_candles(frame) is frame
    assert frame.index.dtype == CANONICAL_INDEX
    assert list(frame.columns) == list(OHLCV_COLUMNS)
    assert all(dtype == np.dtype(np.float64) for dtype in frame.dtypes)


def test_provider_shaped_has_the_yfinance_history_shape() -> None:
    candles = session_candles(NYSE, H1, utc("2024-07-02T00:00"), utc("2024-07-03T00:00"))

    shaped = provider_shaped(candles)

    assert str(shaped.index.dtype) == "datetime64[s, America/New_York]"
    assert shaped.index.name == "Datetime"
    assert list(shaped.columns) == [
        "Open",
        "High",
        "Low",
        "Close",
        "Adj Close",
        "Volume",
        "Dividends",
        "Stock Splits",
    ]
    assert shaped["Volume"].dtype == np.dtype(np.int64)
    assert shaped["Volume"].tolist() == np.rint(candles["volume"]).astype(np.int64).tolist()
    assert shaped["Adj Close"].tolist() == candles["close"].tolist()
    assert shaped["Close"].tolist() == candles["close"].tolist()
    assert shaped["Dividends"].tolist() == [0.0] * len(candles)
    assert shaped["Stock Splits"].tolist() == [0.0] * len(candles)
    assert shaped.index[0] == pd.Timestamp("2024-07-02 09:30", tz=NEW_YORK)


def test_provider_shaped_options_set_zone_unit_and_name() -> None:
    candles = session_candles(NYSE, D1, utc("2024-07-02T00:00"), utc("2024-07-06T00:00"))

    shaped = provider_shaped(candles, timezone=UTC, unit="ns", index_name="Date")

    assert str(shaped.index.dtype) == "datetime64[ns, UTC]"
    assert shaped.index.name == "Date"


def test_provider_shaped_round_trips_through_normalization() -> None:
    from trading_bot.domain.candle_normalization import normalize_candles

    candles = session_candles(NYSE, H4, utc("2024-03-01T00:00"), utc("2024-03-20T00:00"))

    result = normalize_candles(provider_shaped(candles), H4, calendar=NYSE)

    assert result.dropped == ()
    pd.testing.assert_frame_equal(
        result.candles, candles.assign(volume=np.rint(candles["volume"])), check_exact=True
    )


def contract_request(lookback: int = 5) -> CandleRequest:
    return CandleRequest(ticker="XMPL", timeframe=D1, lookback=lookback, now=NOW)


def contract_frame() -> pd.DataFrame:
    """The last five daily candles closed at NOW: a frame that meets the contract."""
    return daily_grid().loc[: pd.Timestamp("2024-07-05T04:00", tz="UTC")].iloc[-5:]


def test_assert_closed_candles_passes_on_a_frame_that_meets_the_contract() -> None:
    assert_closed_candles(contract_frame(), contract_request(), calendar=NYSE)
    assert_closed_candles(contract_frame().iloc[-1:], contract_request(), calendar=NYSE)


def _with_nan() -> pd.DataFrame:
    frame = contract_frame()
    frame.iloc[0, 3] = np.nan
    return frame


def _ns_index() -> pd.DataFrame:
    frame = contract_frame()
    frame.index = pd.DatetimeIndex(frame.index).as_unit("ns")
    return frame


def _with_label(label: str) -> pd.DataFrame:
    frame = contract_frame()
    extra = frame.iloc[[-1]].set_axis(
        pd.DatetimeIndex([pd.Timestamp(label, tz="UTC")]).as_unit("us"), axis=0
    )
    return pd.concat([frame.iloc[:-1], extra]).sort_index()


@pytest.mark.parametrize(
    ("build", "lookback", "match"),
    [
        pytest.param(lambda: contract_frame()["close"], 5, "valid candle frame", id="not-a-frame"),
        pytest.param(_with_nan, 5, "valid candle frame", id="invalid-values"),
        pytest.param(_ns_index, 5, "datetime64", id="index-unit"),
        pytest.param(lambda: contract_frame().iloc[:0], 5, "empty", id="empty"),
        pytest.param(contract_frame, 4, "lookback", id="longer-than-lookback"),
        pytest.param(lambda: _with_label("2024-07-05T00:00"), 5, "slot", id="off-grid-label"),
        pytest.param(
            lambda: _with_label("2020-07-06T04:00"), 5, "slot", id="label-outside-calendar"
        ),
        pytest.param(lambda: _with_label("2024-07-08T04:00"), 5, "closes", id="label-not-closed"),
        pytest.param(lambda: contract_frame().iloc[:-1], 5, "last closed", id="stale-last-label"),
    ],
)
def test_assert_closed_candles_raises_for_each_violation(
    build: Callable[[], pd.DataFrame], lookback: int, match: str
) -> None:
    with pytest.raises(AssertionError, match=match):
        assert_closed_candles(build(), contract_request(lookback), calendar=NYSE)


def test_assert_closed_candles_raises_explicitly_instead_of_using_assert_statements() -> None:
    tree = ast.parse(Path(provider_contract_module.__file__).read_text(encoding="utf-8"))

    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]


def test_the_fixture_modules_export_their_public_names() -> None:
    import tests.fixtures.fake_provider as fake_provider_module
    import tests.fixtures.session_candles as session_candles_module

    assert fake_provider_module.__all__ == [
        "FakeMarketDataProvider",
        "FetchCall",
        "as_market_data_provider",
    ]
    assert provider_contract_module.__all__ == ["assert_closed_candles"]
    assert session_candles_module.__all__ == ["provider_shaped", "session_candles"]
