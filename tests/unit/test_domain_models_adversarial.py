"""Adversarial tests for the candle contract and the signal value types (spec 004, T12-T14).

These tests go beyond the developer's self-tests (``test_candle_validation.py``,
``test_timeframe.py``, ``test_signals.py``): validator edge cases that a naive implementation
could mishandle (subnormal prices, ``-0.0`` volume, boolean columns, huge frames), ticker/rule id
corner cases (full-width and zero-width Unicode, DST folds, huge indicator maps), and message
safety for user-controlled text across the three entry points that echo it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.candles import synthetic_candles
from trading_bot.domain.candles import (
    CandleColumnsError,
    CandleErrorKind,
    CandleValuesError,
    validate_candles,
)
from trading_bot.domain.signals import IndicatorValues, SignalKey, normalize_ticker
from trading_bot.domain.timeframe import Timeframe, UnknownTimeframeError

NEW_YORK = ZoneInfo("America/New_York")
UTC_NOW = datetime(2024, 1, 3, 5, 0, tzinfo=UTC)

# --- T12: adversarial candles (AC5-AC7) -----------------------------------------------------


def test_negative_zero_volume_is_accepted() -> None:
    frame = synthetic_candles(5, seed=1)
    frame = frame.copy()
    frame.iloc[2, frame.columns.get_loc("volume")] = -0.0

    result = validate_candles(frame)

    assert result is frame


def test_subnormal_positive_prices_are_accepted() -> None:
    tiny = 5e-324  # the smallest positive subnormal double: still > 0
    frame = pd.DataFrame(
        {"open": tiny, "high": tiny, "low": tiny, "close": tiny, "volume": 0.0},
        index=pd.date_range("2024-01-01", periods=1, freq="D", tz="UTC"),
        dtype=np.float64,
    )

    result = validate_candles(frame)

    assert result is frame
    assert (result["open"] > 0.0).all()


def test_datetime_index_with_freq_set_is_accepted_and_preserved() -> None:
    index = pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC")
    frame = pd.DataFrame(
        {"open": 1.0, "high": 1.5, "low": 0.5, "close": 1.0, "volume": 10.0},
        index=index,
        dtype=np.float64,
    )
    assert frame.index.freq is not None

    result = validate_candles(frame)

    assert result is frame
    assert result.index.freq == index.freq


def test_frame_with_attrs_set_is_returned_by_identity() -> None:
    frame = synthetic_candles(5, seed=1).copy()
    frame.attrs["source"] = "test-fixture"

    result = validate_candles(frame)

    assert result is frame
    assert result.attrs == {"source": "test-fixture"}


def test_boolean_column_is_rejected_as_wrong_dtype() -> None:
    frame = synthetic_candles(5, seed=1).astype({"close": bool})

    with pytest.raises(CandleColumnsError) as caught:
        validate_candles(frame)

    assert caught.value.kind is CandleErrorKind.DTYPE
    assert caught.value.column == "close"
    assert "bool" in str(caught.value)


def test_nan_and_infinite_in_the_same_row_reports_missing_value_first() -> None:
    frame = synthetic_candles(10, seed=1).copy()
    frame.iloc[4, frame.columns.get_loc("open")] = np.nan
    frame.iloc[4, frame.columns.get_loc("high")] = np.inf

    with pytest.raises(CandleValuesError) as caught:
        validate_candles(frame)

    assert caught.value.kind is CandleErrorKind.MISSING_VALUE
    assert caught.value.position == 4


def test_huge_frame_with_only_the_last_row_broken_reports_that_row() -> None:
    """A 100,000-row frame whose only defect is in the last row (AC6, AC19 performance)."""
    n = 100_000
    index = pd.date_range("2000-01-01", periods=n, freq="min", tz="UTC")
    frame = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1_000.0},
        index=index,
        dtype=np.float64,
    )
    frame.iloc[n - 1, frame.columns.get_loc("close")] = np.nan

    with pytest.raises(CandleValuesError) as caught:
        validate_candles(frame)

    assert caught.value.kind is CandleErrorKind.MISSING_VALUE
    assert caught.value.position == n - 1
    assert caught.value.count == 1


# --- T13: adversarial signals (AC1, AC10-AC13) ----------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(chr(0xFF21) + "APL", id="fullwidth-latin-a"),  # U+FF21 fullwidth "A"
        pytest.param("AA" + chr(0x200B) + "PL", id="zero-width-space"),
        pytest.param("AAPL" + chr(0x200D), id="zero-width-joiner-suffix"),
    ],
)
def test_normalize_ticker_rejects_fullwidth_and_zero_width_characters(raw: str) -> None:
    with pytest.raises(ValueError, match="ticker"):
        normalize_ticker(raw)


@pytest.mark.parametrize(("length", "accepted"), [(32, True), (33, False)])
def test_normalize_ticker_boundary_lengths(length: int, accepted: bool) -> None:
    symbol = "a" * length

    if accepted:
        assert normalize_ticker(symbol) == "A" * length
    else:
        with pytest.raises(ValueError, match="ticker"):
            normalize_ticker(symbol)


@pytest.mark.parametrize(("length", "accepted"), [(64, True), (65, False)])
def test_rule_id_boundary_lengths(length: int, accepted: bool) -> None:
    rule_id = "r" * length
    fields: dict[str, object] = {
        "ticker": "AAPL",
        "timeframe": Timeframe.D1,
        "rule_id": rule_id,
        "candle_close_ts": datetime(2024, 1, 3, 5, 0, tzinfo=UTC),
    }

    if accepted:
        assert SignalKey(**fields).rule_id == rule_id  # type: ignore[arg-type]
    else:
        with pytest.raises(ValueError, match="rule_id"):
            SignalKey(**fields)  # type: ignore[arg-type]


def test_indicator_values_with_float32_and_ten_thousand_entries() -> None:
    names = [f"indicator_{i}" for i in range(10_000)]
    source = {name: np.float32(i) * 0.5 for i, name in enumerate(names)}

    values = IndicatorValues(source)

    assert len(values) == 10_000
    assert all(type(value) is float for value in values.values())
    assert all(type(key) is str for key in values)
    assert values["indicator_9999"] == pytest.approx(4999.5)


def test_candle_close_ts_accepts_a_datetime_subclass_other_than_timestamp() -> None:
    class CustomDatetime(datetime):
        """A datetime subclass that is neither the stdlib type nor a pandas Timestamp."""

    value = CustomDatetime(2024, 1, 3, 5, 0, tzinfo=UTC)
    assert type(value) is not datetime

    key = SignalKey(ticker="AAPL", timeframe=Timeframe.D1, rule_id="42", candle_close_ts=value)

    assert type(key.candle_close_ts) is datetime
    assert key.candle_close_ts == datetime(2024, 1, 3, 5, 0, tzinfo=UTC)


def test_ambiguous_dst_fold_gives_two_different_instants_and_keys() -> None:
    """The wall time 2024-11-03T01:30 in America/New_York is ambiguous: fold picks the instant."""
    fold_0 = datetime(2024, 11, 3, 1, 30, tzinfo=NEW_YORK, fold=0)
    fold_1 = datetime(2024, 11, 3, 1, 30, tzinfo=NEW_YORK, fold=1)

    key_0 = SignalKey(ticker="AAPL", timeframe=Timeframe.H1, rule_id="r", candle_close_ts=fold_0)
    key_1 = SignalKey(ticker="AAPL", timeframe=Timeframe.H1, rule_id="r", candle_close_ts=fold_1)

    assert key_0.candle_close_ts == datetime(2024, 11, 3, 5, 30, tzinfo=UTC)
    assert key_1.candle_close_ts == datetime(2024, 11, 3, 6, 30, tzinfo=UTC)
    assert key_0 != key_1
    assert str(key_0) != str(key_1)
    assert hash(key_0) != hash(key_1)


# --- Idempotency key collision probing (beyond the mandated plan) --------------------------


def test_key_string_form_is_unambiguous_because_the_charset_excludes_the_separator() -> None:
    """Every field charset excludes ``|``, so ``str(key).split("|", 3)`` always recovers the
    original four fields verbatim: no combination of valid fields can collide through the
    separator (ticker and rule_id charsets forbid "|"; the timeframe code and the isoformat
    timestamp never contain it either)."""
    rule_id = "".join(chr(code) for code in range(0x21, 0x7F) if chr(code) != "|")[:64]
    key = SignalKey(
        ticker="BRK-B",
        timeframe=Timeframe.H4,
        rule_id=rule_id,
        candle_close_ts=datetime(2024, 1, 2, 22, 30, tzinfo=UTC),
    )

    ticker, timeframe, parsed_rule_id, timestamp = str(key).split("|", 3)

    assert (ticker, timeframe, parsed_rule_id, timestamp) == (
        "BRK-B",
        "4h",
        rule_id,
        "2024-01-02T22:30:00+00:00",
    )


def test_microsecond_zero_and_nonzero_candle_close_ts_format_differently_and_do_not_collide() -> (
    None
):
    exact = SignalKey(
        ticker="AAPL",
        timeframe=Timeframe.H1,
        rule_id="r",
        candle_close_ts=datetime(2024, 1, 2, 15, 30, 0, tzinfo=UTC),
    )
    with_micros = SignalKey(
        ticker="AAPL",
        timeframe=Timeframe.H1,
        rule_id="r",
        candle_close_ts=datetime(2024, 1, 2, 15, 30, 0, 500_000, tzinfo=UTC),
    )

    assert str(exact) == "AAPL|1h|r|2024-01-02T15:30:00+00:00"
    assert str(with_micros) == "AAPL|1h|r|2024-01-02T15:30:00.500000+00:00"
    assert exact != with_micros


def test_one_microsecond_apart_fixed_offset_instants_still_give_distinct_keys() -> None:
    """A very small change in a fixed-offset representation must not be lost in normalization."""
    minus_five = timezone(timedelta(hours=-5))
    base_utc = datetime(2024, 1, 2, 15, 30, 0, tzinfo=UTC)
    same_instant_fixed_offset = datetime(2024, 1, 2, 10, 30, 0, tzinfo=minus_five)  # equal instant
    one_microsecond_later_fixed_offset = datetime(2024, 1, 2, 10, 30, 0, 1, tzinfo=minus_five)

    key_base = SignalKey(
        ticker="AAPL", timeframe=Timeframe.H1, rule_id="r", candle_close_ts=base_utc
    )
    key_same = SignalKey(
        ticker="AAPL",
        timeframe=Timeframe.H1,
        rule_id="r",
        candle_close_ts=same_instant_fixed_offset,
    )
    key_plus = SignalKey(
        ticker="AAPL",
        timeframe=Timeframe.H1,
        rule_id="r",
        candle_close_ts=one_microsecond_later_fixed_offset,
    )

    # Sanity: the fixed-offset representation of the same instant gives the same key.
    assert key_base == key_same
    assert str(key_base) == str(key_same)
    # The one-microsecond difference, expressed only through the fixed-offset wall time, survives.
    assert key_base != key_plus
    assert str(key_base) != str(key_plus)


# --- T14: message safety across the three echoing entry points (AC3, AC10, AC11) ------------

# Escape-heavy inputs distinct from the developer's own coverage: bidirectional overrides,
# combining marks, backspace/vertical-tab/form-feed controls and carriage returns.
RIGHT_TO_LEFT_OVERRIDE = chr(0x202E)
ZERO_WIDTH_SPACE = chr(0x200B)
COMBINING_MARK = chr(0x0301)  # combining acute accent, stacks indefinitely on repetition
BACKSPACE = chr(0x08)
VERTICAL_TAB = chr(0x0B)
FORM_FEED = chr(0x0C)
CARRIAGE_RETURN = chr(0x0D)
NEWLINE = chr(0x0A)

ADVERSARIAL_MESSAGE_INPUTS = [
    pytest.param(RIGHT_TO_LEFT_OVERRIDE * 5_000, id="bidi-override"),
    pytest.param("a" + COMBINING_MARK * 5_000, id="combining-marks"),
    pytest.param(BACKSPACE + VERTICAL_TAB + FORM_FEED + CARRIAGE_RETURN + NEWLINE, id="controls"),
    pytest.param(ZERO_WIDTH_SPACE * 10_000, id="zero-width-space"),
]


def assert_message_is_safe(message: str) -> None:
    assert NEWLINE not in message
    assert CARRIAGE_RETURN not in message
    assert len(message) < 200


@pytest.mark.parametrize("raw", ADVERSARIAL_MESSAGE_INPUTS)
def test_timeframe_parse_messages_stay_bounded_and_single_line(raw: str) -> None:
    with pytest.raises(UnknownTimeframeError) as caught:
        Timeframe.parse(raw)

    assert_message_is_safe(str(caught.value))


@pytest.mark.parametrize("raw", ADVERSARIAL_MESSAGE_INPUTS)
def test_normalize_ticker_messages_stay_bounded_and_single_line(raw: str) -> None:
    with pytest.raises(ValueError, match="ticker") as caught:
        normalize_ticker(raw)

    assert_message_is_safe(str(caught.value))


@pytest.mark.parametrize("raw", ADVERSARIAL_MESSAGE_INPUTS)
def test_rule_id_messages_stay_bounded_and_single_line(raw: str) -> None:
    with pytest.raises(ValueError, match="rule_id") as caught:
        SignalKey(ticker="AAPL", timeframe=Timeframe.D1, rule_id=raw, candle_close_ts=UTC_NOW)

    assert_message_is_safe(str(caught.value))


def test_ten_thousand_character_input_is_bounded_for_every_echoing_entry_point() -> None:
    huge = "x" * 10_000

    with pytest.raises(UnknownTimeframeError) as timeframe_error:
        Timeframe.parse(huge)
    with pytest.raises(ValueError, match="ticker") as ticker_error:
        normalize_ticker(huge)
    with pytest.raises(ValueError, match="rule_id") as rule_id_error:
        SignalKey(ticker="AAPL", timeframe=Timeframe.D1, rule_id=huge, candle_close_ts=UTC_NOW)

    for caught in (timeframe_error.value, ticker_error.value, rule_id_error.value):
        message = str(caught)
        assert_message_is_safe(message)
        assert huge[:33] not in message
        # The bounded echo keeps repr() of some prefix of length 1..32 (spec 004, deviation 2).
        assert any(repr(huge[:n]) in message for n in range(1, 33))
