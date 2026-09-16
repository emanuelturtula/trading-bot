"""Tests of ``Signal``, ``SignalKey`` and their value types (spec 004, AC9-AC13)."""

from __future__ import annotations

import copy
import dataclasses
import pickle
import string
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

import numpy as np
import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.lookahead import values_equal
from trading_bot.domain.signals import (
    IndicatorValues,
    Side,
    Signal,
    SignalKey,
    normalize_ticker,
)
from trading_bot.domain.timeframe import Timeframe

CLOSE_TS = datetime(2024, 1, 3, 5, 0, tzinfo=UTC)
MINUS_FIVE = timezone(timedelta(hours=-5))


def make_signal(**overrides: object) -> Signal:
    fields: dict[str, object] = {
        "ticker": "AAPL",
        "timeframe": Timeframe.D1,
        "rule_id": "42",
        "side": Side.BUY,
        "candle_close_ts": CLOSE_TS,
        "close_price": 187.5,
        "indicator_values": {"rsi_14": 28.4, "sma_200": 180.1},
    }
    fields.update(overrides)
    return Signal(**fields)  # type: ignore[arg-type]


# --- AC9: Side -------------------------------------------------------------------------------


def test_side_has_exactly_buy_and_sell() -> None:
    assert issubclass(Side, StrEnum)
    assert [(member.name, member.value) for member in Side] == [("BUY", "BUY"), ("SELL", "SELL")]


# --- T7: normalize_ticker (AC10) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" aapl ", "AAPL"),
        ("brk-b", "BRK-B"),
        ("aapl\n", "AAPL"),
        ("\tmsft", "MSFT"),
        (" " + "a" * 32 + " ", "A" * 32),
    ],
)
def test_normalize_ticker_strips_and_upper_cases(raw: str, expected: str) -> None:
    result = normalize_ticker(raw)

    assert result == expected
    assert type(result) is str


@pytest.mark.parametrize(
    "symbol", ["AAPL", "BRK-B", "^GSPC", "EURUSD=X", "BTC-USD", "RELIANCE.NS", "M&M.NS", "ES=F"]
)
def test_normalize_ticker_keeps_yfinance_symbols_apart_from_case(symbol: str) -> None:
    assert normalize_ticker(symbol) == symbol
    assert normalize_ticker(symbol.lower()) == symbol


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("AA PL", id="inner-space"),
        pytest.param("AA\nPL", id="inner-newline"),
        pytest.param("AA\tPL", id="inner-tab"),
        pytest.param("AA\x00PL", id="nul"),
        pytest.param("AA\x7fPL", id="delete"),
        pytest.param("AA|PL", id="pipe"),
        pytest.param(chr(0xC4) + "APL", id="non-ascii"),
        pytest.param(chr(0x17F) + "py", id="non-ascii-that-upper-cases-to-ascii"),
        pytest.param("A" * 33, id="too-long"),
    ],
)
def test_normalize_ticker_rejects_invalid_symbols(raw: str) -> None:
    with pytest.raises(ValueError, match="ticker") as caught:
        normalize_ticker(raw)

    assert repr(raw[:32]) in str(caught.value)


def test_normalize_ticker_message_is_bounded() -> None:
    raw = "B" * 10_000

    with pytest.raises(ValueError, match="ticker") as caught:
        normalize_ticker(raw)

    assert repr("B" * 32) in str(caught.value)
    assert "B" * 33 not in str(caught.value)
    assert len(str(caught.value)) < 200


@pytest.mark.parametrize("value", [None, 1, b"AAPL"])
def test_normalize_ticker_rejects_non_strings(value: object) -> None:
    with pytest.raises(TypeError, match="ticker"):
        normalize_ticker(value)  # type: ignore[arg-type]


def test_signal_normalizes_its_ticker() -> None:
    signal = make_signal(ticker=" brk-b ")

    assert signal.ticker == "BRK-B"
    assert type(signal.ticker) is str


# --- T7: rule_id and enum fields (AC11) -------------------------------------------------------


@pytest.mark.parametrize("rule_id", ["42", "rule-7", "RSI_Oversold", "a" * 64, "!~^=&"])
def test_rule_id_is_stored_verbatim(rule_id: str) -> None:
    signal = make_signal(rule_id=rule_id)

    assert signal.rule_id == rule_id
    assert type(signal.rule_id) is str


def test_rule_id_subclasses_are_stored_as_plain_str() -> None:
    class Code(StrEnum):
        RULE = "rule-7"

    signal = make_signal(rule_id=Code.RULE)

    assert signal.rule_id == "rule-7"
    assert type(signal.rule_id) is str


@pytest.mark.parametrize(
    "rule_id",
    [
        pytest.param(" 42", id="leading-space"),
        pytest.param("42 ", id="trailing-space"),
        pytest.param("", id="empty"),
        pytest.param("a b", id="inner-space"),
        pytest.param("a\nb", id="newline"),
        pytest.param("a|b", id="pipe"),
        pytest.param("r" + chr(0xE9) + "gle", id="non-ascii"),
        pytest.param("a" * 65, id="too-long"),
    ],
)
def test_invalid_rule_ids_are_rejected(rule_id: str) -> None:
    with pytest.raises(ValueError, match="rule_id") as caught:
        make_signal(rule_id=rule_id)

    assert repr(rule_id[:32]) in str(caught.value)
    assert len(str(caught.value)) < 200


@pytest.mark.parametrize("rule_id", [42, None, b"42"])
def test_non_string_rule_ids_are_rejected(rule_id: object) -> None:
    with pytest.raises(TypeError, match="rule_id"):
        make_signal(rule_id=rule_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("timeframe", "1d", id="timeframe-code"),
        pytest.param("timeframe", None, id="timeframe-none"),
        pytest.param("side", "BUY", id="side-string"),
        pytest.param("side", None, id="side-none"),
    ],
)
def test_enum_fields_require_members(field: str, value: object) -> None:
    with pytest.raises(TypeError, match=field):
        make_signal(**{field: value})


@pytest.mark.parametrize(
    ("value", "error"),
    [
        pytest.param("2024-01-03T05:00:00+00:00", TypeError, id="string"),
        pytest.param(datetime(2024, 1, 3, 5, 0), ValueError, id="naive"),
    ],
)
def test_candle_close_ts_must_be_an_aware_datetime(value: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        make_signal(candle_close_ts=value)


# --- T8: close_price and indicator_values (AC11) ----------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(187.5, 187.5, id="float"),
        pytest.param(3, 3.0, id="int"),
        pytest.param(np.float64(2.5), 2.5, id="numpy-float64"),
        pytest.param(np.int64(7), 7.0, id="numpy-int64"),
        pytest.param(Fraction(1, 4), 0.25, id="fraction"),
        pytest.param(5e-324, 5e-324, id="subnormal"),
    ],
)
def test_close_price_accepts_real_numbers_as_builtin_floats(value: object, expected: float) -> None:
    signal = make_signal(close_price=value)

    assert signal.close_price == expected
    assert type(signal.close_price) is float


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="bool"),
        pytest.param(np.bool_(True), id="numpy-bool"),
        pytest.param("187.5", id="string"),
        pytest.param(Decimal("187.5"), id="decimal"),
        pytest.param(None, id="none"),
        pytest.param(1 + 2j, id="complex"),
    ],
)
def test_close_price_rejects_non_real_types(value: object) -> None:
    with pytest.raises(TypeError, match="close_price"):
        make_signal(close_price=value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0.0, id="zero"),
        pytest.param(-0.0, id="negative-zero"),
        pytest.param(0, id="int-zero"),
        pytest.param(-1.0, id="negative"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("-inf"), id="minus-inf"),
        pytest.param(10**400, id="int-overflowing-float"),
    ],
)
def test_close_price_rejects_non_positive_and_non_finite_values(value: object) -> None:
    with pytest.raises(ValueError, match="close_price"):
        make_signal(close_price=value)


def test_indicator_values_default_to_empty() -> None:
    signal = Signal(
        ticker="AAPL",
        timeframe=Timeframe.D1,
        rule_id="42",
        side=Side.SELL,
        candle_close_ts=CLOSE_TS,
        close_price=10.0,
    )

    assert isinstance(signal.indicator_values, IndicatorValues)
    assert len(signal.indicator_values) == 0
    assert signal.indicator_values == {}


def test_indicator_values_are_stored_as_an_ordered_float_copy() -> None:
    source: dict[str, float] = {
        "sma_200": 180,  # type: ignore[dict-item]
        "rsi_14": np.float64(28.4),
        "macd_hist": -0.5,
        "obv": 0.0,
        "tiny": np.float32(1.5),
    }

    signal = make_signal(indicator_values=source)

    values = signal.indicator_values
    assert isinstance(values, IndicatorValues)
    assert list(values) == ["sma_200", "rsi_14", "macd_hist", "obv", "tiny"]
    assert dict(values) == {
        "sma_200": 180.0,
        "rsi_14": 28.4,
        "macd_hist": -0.5,
        "obv": 0.0,
        "tiny": 1.5,
    }
    assert all(type(value) is float for value in values.values())
    assert all(type(key) is str for key in values)


def test_indicator_names_subclasses_are_stored_as_plain_str() -> None:
    class Name(StrEnum):
        RSI = "rsi_14"

    values = IndicatorValues({Name.RSI: 1.0})

    assert [type(key) for key in values] == [str]


@pytest.mark.parametrize(
    ("values", "error"),
    [
        pytest.param({"": 1.0}, ValueError, id="empty-name"),
        pytest.param({1: 1.0}, TypeError, id="int-name"),
        pytest.param({"rsi": True}, TypeError, id="bool-value"),
        pytest.param({"rsi": "1.0"}, TypeError, id="string-value"),
        pytest.param({"rsi": Decimal("1.0")}, TypeError, id="decimal-value"),
        pytest.param({"rsi": None}, TypeError, id="none-value"),
        pytest.param({"rsi": float("nan")}, ValueError, id="nan-value"),
        pytest.param({"rsi": float("inf")}, ValueError, id="inf-value"),
        pytest.param({"rsi": float("-inf")}, ValueError, id="minus-inf-value"),
        pytest.param([("rsi", 1.0)], TypeError, id="pairs-not-a-mapping"),
        pytest.param(None, TypeError, id="none"),
    ],
)
def test_invalid_indicator_values_are_rejected(values: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="indicator"):
        make_signal(indicator_values=values)


def test_indicator_value_messages_are_bounded() -> None:
    name = "n" * 10_000

    with pytest.raises(ValueError, match="indicator") as caught:
        IndicatorValues({name: float("nan")})

    assert len(str(caught.value)) < 200


def test_indicator_values_reject_non_mappings_directly() -> None:
    with pytest.raises(TypeError, match="indicator"):
        IndicatorValues([("rsi", 1.0)])  # type: ignore[arg-type]


# --- T9: immutability and value semantics (AC12) ----------------------------------------------

SIGNAL_FIELDS = [
    ("ticker", "MSFT"),
    ("timeframe", Timeframe.H1),
    ("rule_id", "7"),
    ("side", Side.SELL),
    ("candle_close_ts", datetime(2025, 1, 1, tzinfo=UTC)),
    ("close_price", 1.0),
    ("indicator_values", {}),
]
KEY_NAMES = ("ticker", "timeframe", "rule_id", "candle_close_ts")
KEY_FIELDS = [(name, value) for name, value in SIGNAL_FIELDS if name in KEY_NAMES]


@pytest.mark.parametrize(("name", "value"), SIGNAL_FIELDS)
def test_signal_fields_cannot_be_assigned(name: str, value: object) -> None:
    signal = make_signal()

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(signal, name, value)


@pytest.mark.parametrize(("name", "value"), KEY_FIELDS)
def test_signal_key_fields_cannot_be_assigned(name: str, value: object) -> None:
    key = make_signal().idempotency_key

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(key, name, value)


def test_key_fields_are_the_four_identity_fields() -> None:
    assert tuple(field.name for field in dataclasses.fields(SignalKey)) == KEY_NAMES
    assert [field.name for field in dataclasses.fields(Signal)] == [
        name for name, _ in SIGNAL_FIELDS
    ]
    assert len(KEY_FIELDS) == 4


def test_indicator_values_are_read_only() -> None:
    values = make_signal().indicator_values

    with pytest.raises(TypeError):
        values["rsi_14"] = 1.0  # type: ignore[index]
    with pytest.raises(TypeError):
        del values["rsi_14"]  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        values.extra = 1.0  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        values._items = {}  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        del values._items  # type: ignore[attr-defined]
    assert dict(values) == {"rsi_14": 28.4, "sma_200": 180.1}


def test_mutating_the_source_mapping_does_not_change_the_signal() -> None:
    source = {"rsi_14": 28.4}
    signal = make_signal(indicator_values=source)

    source["rsi_14"] = 99.0
    source["extra"] = 1.0

    assert dict(signal.indicator_values) == {"rsi_14": 28.4}


def test_replace_validates_and_normalizes_again() -> None:
    signal = make_signal()
    new_york_midnight = pd.Timestamp("2024-01-03", tz="America/New_York")

    replaced = dataclasses.replace(signal, ticker="msft", candle_close_ts=new_york_midnight)

    assert replaced.ticker == "MSFT"
    assert type(replaced.candle_close_ts) is datetime
    assert replaced.candle_close_ts == datetime(2024, 1, 3, 5, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="ticker"):
        dataclasses.replace(signal, ticker="")
    with pytest.raises(ValueError, match="close_price"):
        dataclasses.replace(signal, close_price=0.0)


def round_trips(value: object) -> list[object]:
    pickled = pickle.loads(pickle.dumps(value))  # noqa: S301 - round-trips a value built here
    return [pickled, copy.deepcopy(value), copy.copy(value)]


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(make_signal, id="signal"),
        pytest.param(lambda: make_signal().idempotency_key, id="signal-key"),
        pytest.param(lambda: IndicatorValues({"b": 2.0, "a": -1.0}), id="indicator-values"),
        pytest.param(IndicatorValues, id="empty-indicator-values"),
    ],
)
def test_pickle_and_copies_round_trip(build: Callable[[], object]) -> None:
    value = build()

    for restored in round_trips(value):
        assert type(restored) is type(value)
        assert restored == value
        assert hash(restored) == hash(value)
        assert repr(restored) == repr(value)


def test_restored_indicator_values_stay_read_only_and_ordered() -> None:
    restored = round_trips(IndicatorValues({"b": 2.0, "a": -1.0}))[0]

    assert isinstance(restored, IndicatorValues)
    assert list(restored) == ["b", "a"]
    with pytest.raises(AttributeError):
        restored.extra = 1.0  # type: ignore[attr-defined]


def test_asdict_works_for_signal_and_key() -> None:
    signal = make_signal()

    as_dict = dataclasses.asdict(signal)
    key_dict = dataclasses.asdict(signal.idempotency_key)

    assert list(as_dict) == [name for name, _ in SIGNAL_FIELDS]
    assert as_dict["ticker"] == "AAPL"
    assert as_dict["indicator_values"] == IndicatorValues({"rsi_14": 28.4, "sma_200": 180.1})
    assert key_dict == {
        "ticker": "AAPL",
        "timeframe": Timeframe.D1,
        "rule_id": "42",
        "candle_close_ts": CLOSE_TS,
    }


def test_equal_signals_are_equal_and_hash_equal() -> None:
    first = make_signal(ticker="aapl", close_price=187, indicator_values={"a": 1, "b": 2.0})
    second = make_signal(ticker="AAPL", close_price=187.0, indicator_values={"b": 2.0, "a": 1.0})

    assert first == second
    assert hash(first) == hash(second)
    assert first != make_signal(close_price=187.25)
    assert first != make_signal(side=Side.SELL)


def test_indicator_values_equality_and_hash_ignore_insertion_order() -> None:
    first = IndicatorValues({"a": 1.0, "b": 2.0})
    second = IndicatorValues({"b": 2.0, "a": 1.0})

    assert first == second
    assert hash(first) == hash(second)
    assert first == {"a": 1.0, "b": 2.0}
    assert first != IndicatorValues({"a": 1.0, "b": 2.5})
    assert first != IndicatorValues({"a": 1.0})
    assert first != [("a", 1.0), ("b", 2.0)]
    assert IndicatorValues({"zero": 0.0}) == IndicatorValues({"zero": -0.0})
    assert hash(IndicatorValues({"zero": 0.0})) == hash(IndicatorValues({"zero": -0.0}))


def test_indicator_values_repr_and_mapping_api() -> None:
    values = IndicatorValues({"rsi_14": 28.4})

    assert repr(values) == "IndicatorValues({'rsi_14': 28.4})"
    assert values["rsi_14"] == 28.4
    assert values.get("missing") is None
    assert "rsi_14" in values
    assert list(values.items()) == [("rsi_14", 28.4)]
    with pytest.raises(KeyError):
        values["missing"]


def test_lookahead_values_equal_treats_equal_signals_as_equal() -> None:
    first = make_signal(ticker="aapl", indicator_values={"a": 1.0, "b": 2.0})
    second = make_signal(ticker="AAPL", indicator_values={"b": 2.0, "a": 1.0})

    assert values_equal(first, second)
    assert not values_equal(first, make_signal(indicator_values={"a": 1.0, "b": 2.5}))
    assert not values_equal(first, make_signal(side=Side.SELL))


# --- T10: idempotency key (AC13) --------------------------------------------------------------


def test_idempotency_key_is_built_from_the_normalized_fields() -> None:
    new_york_midnight = pd.Timestamp("2024-01-03", tz="America/New_York")
    signal = make_signal(ticker=" aapl ", candle_close_ts=new_york_midnight)

    key = signal.idempotency_key

    assert type(key) is SignalKey
    assert key == SignalKey(
        ticker="AAPL", timeframe=Timeframe.D1, rule_id="42", candle_close_ts=CLOSE_TS
    )
    assert (key.ticker, key.timeframe, key.rule_id, key.candle_close_ts) == (
        signal.ticker,
        signal.timeframe,
        signal.rule_id,
        signal.candle_close_ts,
    )


def test_idempotency_key_is_stable_across_calls() -> None:
    signal = make_signal()

    first = signal.idempotency_key
    second = signal.idempotency_key

    assert first == second
    assert hash(first) == hash(second)
    assert str(first) == str(second)


@pytest.mark.parametrize(
    "candle_close_ts",
    [
        pytest.param(datetime(2024, 1, 3, 5, 0, tzinfo=UTC), id="stdlib-utc"),
        pytest.param(datetime(2024, 1, 3, 0, 0, tzinfo=MINUS_FIVE), id="fixed-offset"),
        pytest.param(pd.Timestamp("2024-01-03T05:00:00", tz="UTC"), id="timestamp-utc"),
        pytest.param(
            pd.Timestamp("2024-01-03T00:00:00", tz="America/New_York"), id="timestamp-new-york"
        ),
    ],
)
@pytest.mark.parametrize("ticker", [" aapl ", "AAPL", "aApL\n"])
def test_equivalent_inputs_give_equal_keys(ticker: str, candle_close_ts: datetime) -> None:
    reference = make_signal().idempotency_key

    key = make_signal(ticker=ticker, candle_close_ts=candle_close_ts).idempotency_key

    assert key == reference
    assert hash(key) == hash(reference)
    assert str(key) == str(reference) == "AAPL|1d|42|2024-01-03T05:00:00+00:00"


TICKER_ALPHABET = string.ascii_letters + string.digits + "^=.-&"
RULE_ID_ALPHABET = "".join(chr(code) for code in range(0x21, 0x7F) if chr(code) != "|")
PADDING_ALPHABET = " \t\n\r"


@st.composite
def equivalent_signal_pairs(draw: st.DrawFn) -> tuple[Signal, Signal, str]:
    """A canonical signal, an equivalent one in another representation, and the key string."""
    symbol = draw(st.text(TICKER_ALPHABET, min_size=1, max_size=32)).upper()
    flips = draw(st.lists(st.booleans(), min_size=len(symbol), max_size=len(symbol)))
    cased = "".join(
        char.lower() if flip else char for char, flip in zip(symbol, flips, strict=True)
    )
    padding = st.text(PADDING_ALPHABET, max_size=3)
    ticker = draw(padding) + cased + draw(padding)
    rule_id = draw(st.text(RULE_ID_ALPHABET, min_size=1, max_size=64))
    timeframe = draw(st.sampled_from(list(Timeframe)))
    instant = draw(
        st.datetimes(
            min_value=datetime(1970, 1, 1),
            max_value=datetime(2100, 1, 1),
            timezones=st.just(UTC),
        )
    )
    minutes = draw(st.integers(min_value=-14 * 60, max_value=14 * 60))
    canonical = make_signal(
        ticker=symbol, rule_id=rule_id, timeframe=timeframe, candle_close_ts=instant
    )
    variant = make_signal(
        ticker=ticker,
        rule_id=rule_id,
        timeframe=timeframe,
        candle_close_ts=instant.astimezone(timezone(timedelta(minutes=minutes))),
        side=draw(st.sampled_from(list(Side))),
        close_price=draw(st.floats(min_value=1e-6, max_value=1e9)),
    )
    return canonical, variant, f"{symbol}|{timeframe.value}|{rule_id}|{instant.isoformat()}"


@given(pair=equivalent_signal_pairs())
def test_keys_are_stable_across_equivalent_representations(
    pair: tuple[Signal, Signal, str],
) -> None:
    canonical, variant, expected = pair

    assert variant.idempotency_key == canonical.idempotency_key
    assert hash(variant.idempotency_key) == hash(canonical.idempotency_key)
    assert str(variant.idempotency_key) == str(canonical.idempotency_key) == expected


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"side": Side.SELL}, id="side"),
        pytest.param({"close_price": 1.25}, id="close-price"),
        pytest.param({"indicator_values": {"other": 1.0}}, id="indicator-values"),
    ],
)
def test_key_ignores_non_identity_fields(changes: dict[str, object]) -> None:
    signal = make_signal()

    changed = make_signal(**changes)

    assert changed != signal
    assert changed.idempotency_key == signal.idempotency_key
    assert str(changed.idempotency_key) == str(signal.idempotency_key)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"ticker": "AAPM"}, id="ticker"),
        pytest.param({"timeframe": Timeframe.H4}, id="timeframe"),
        pytest.param({"rule_id": "rule-2"}, id="rule-id"),
        pytest.param({"rule_id": "RULE"}, id="rule-id-case"),
        pytest.param(
            {"candle_close_ts": CLOSE_TS + timedelta(microseconds=1)}, id="one-microsecond"
        ),
    ],
)
def test_key_changes_with_any_identity_field(changes: dict[str, object]) -> None:
    reference = make_signal(rule_id="rule").idempotency_key

    key = make_signal(**{"rule_id": "rule", **changes}).idempotency_key

    assert key != reference
    assert str(key) != str(reference)


def test_golden_string_forms() -> None:
    first = make_signal(
        ticker="aapl",
        timeframe=Timeframe.D1,
        rule_id="42",
        candle_close_ts=datetime(2024, 1, 3, 5, 0, tzinfo=UTC),
    )
    second = make_signal(
        ticker="brk-b",
        timeframe=Timeframe.H4,
        rule_id="rule-7",
        candle_close_ts=Timeframe.H4.nominal_close(datetime(2024, 1, 2, 18, 30, tzinfo=UTC)),
    )

    assert str(first.idempotency_key) == "AAPL|1d|42|2024-01-03T05:00:00+00:00"
    assert str(second.idempotency_key) == "BRK-B|4h|rule-7|2024-01-02T22:30:00+00:00"


def test_signal_key_built_from_raw_values_normalizes_like_signal() -> None:
    raw_close = pd.Timestamp("2024-01-03T00:00:00", tz="America/New_York")

    key = SignalKey(ticker="aapl", timeframe=Timeframe.D1, rule_id="42", candle_close_ts=raw_close)

    assert key.ticker == "AAPL"
    assert type(key.candle_close_ts) is datetime
    assert key.candle_close_ts.tzinfo is UTC
    assert key == make_signal(ticker="aapl", candle_close_ts=raw_close).idempotency_key
    assert str(key) == "AAPL|1d|42|2024-01-03T05:00:00+00:00"


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        pytest.param({"ticker": "AA PL"}, ValueError, id="ticker"),
        pytest.param({"ticker": 1}, TypeError, id="ticker-type"),
        pytest.param({"timeframe": "1d"}, TypeError, id="timeframe"),
        pytest.param({"rule_id": " 42"}, ValueError, id="rule-id"),
        pytest.param({"rule_id": 42}, TypeError, id="rule-id-type"),
        pytest.param({"candle_close_ts": datetime(2024, 1, 3)}, ValueError, id="naive"),
        pytest.param({"candle_close_ts": "2024-01-03"}, TypeError, id="close-type"),
    ],
)
@pytest.mark.parametrize("build", [SignalKey, make_signal], ids=["signal-key", "signal"])
def test_signal_key_and_signal_reject_the_same_inputs(
    build: Callable[..., object], changes: dict[str, object], error: type[Exception]
) -> None:
    fields: dict[str, object] = {
        "ticker": "AAPL",
        "timeframe": Timeframe.D1,
        "rule_id": "42",
        "candle_close_ts": CLOSE_TS,
        **changes,
    }

    with pytest.raises(error):
        build(**fields)


def test_signal_key_is_not_a_tuple_and_works_in_dicts_and_sets() -> None:
    key = make_signal().idempotency_key
    as_tuple = ("AAPL", Timeframe.D1, "42", CLOSE_TS)
    same = make_signal(ticker=" aapl ", side=Side.SELL).idempotency_key

    assert key != as_tuple
    assert as_tuple != key
    assert not isinstance(key, tuple)
    assert {key: "sent"}[same] == "sent"
    assert same in {key}
    assert len({key, same, make_signal(rule_id="43").idempotency_key}) == 2


# --- Bounded echoes of rejected input -----------------------------------------------------------

ESCAPE = chr(0x1B)
NEWLINE = chr(0x0A)
ESCAPE_HEAVY = [
    pytest.param(ESCAPE * 10_000, id="escape-characters"),
    pytest.param(chr(0xE0001) * 10_000, id="astral-non-printable"),
    pytest.param(f"{ESCAPE}[31mAAPL{ESCAPE}[0m{NEWLINE}" * 1_000, id="ansi-and-newlines"),
]


def assert_bounded_echo(message: str, raw: str) -> None:
    assert NEWLINE not in message
    assert ESCAPE not in message
    assert len(message) < 200
    assert any(repr(raw[:n]) in message for n in range(1, 33))


@pytest.mark.parametrize("raw", ESCAPE_HEAVY)
def test_ticker_messages_stay_bounded_for_escape_heavy_inputs(raw: str) -> None:
    with pytest.raises(ValueError, match="ticker") as caught:
        normalize_ticker(raw)

    assert_bounded_echo(str(caught.value), raw)


@pytest.mark.parametrize("raw", ESCAPE_HEAVY)
def test_rule_id_messages_stay_bounded_for_escape_heavy_inputs(raw: str) -> None:
    with pytest.raises(ValueError, match="rule_id") as caught:
        make_signal(rule_id=raw)

    assert_bounded_echo(str(caught.value), raw)


@pytest.mark.parametrize("raw", ESCAPE_HEAVY)
def test_indicator_value_messages_stay_bounded_for_escape_heavy_names(raw: str) -> None:
    with pytest.raises(ValueError, match="indicator") as caught:
        IndicatorValues({raw: float("inf")})

    assert_bounded_echo(str(caught.value), raw)
