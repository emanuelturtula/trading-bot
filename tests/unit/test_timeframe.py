"""Tests of ``Timeframe`` (spec 004, AC2-AC4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum

import pandas as pd
import pytest

from trading_bot.domain.timeframe import Timeframe, UnknownTimeframeError

# --- AC2: members and durations ----------------------------------------------------------


def test_members_are_exactly_the_three_codes_in_order() -> None:
    assert issubclass(Timeframe, StrEnum)
    assert [(member.name, member.value) for member in Timeframe] == [
        ("H1", "1h"),
        ("H4", "4h"),
        ("D1", "1d"),
    ]


@pytest.mark.parametrize(
    ("member", "code", "duration"),
    [
        (Timeframe.H1, "1h", timedelta(hours=1)),
        (Timeframe.H4, "4h", timedelta(hours=4)),
        (Timeframe.D1, "1d", timedelta(days=1)),
    ],
)
def test_str_is_the_code_and_duration_matches(
    member: Timeframe, code: str, duration: timedelta
) -> None:
    assert str(member) == code
    assert f"{member}" == code
    assert member.duration == duration
    assert type(member.duration) is timedelta


# --- AC3: parse ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1h", Timeframe.H1),
        ("4h", Timeframe.H4),
        ("1d", Timeframe.D1),
        (" 1d ", Timeframe.D1),
        ("4h\n", Timeframe.H4),
        ("\t1h", Timeframe.H1),
    ],
)
def test_parse_accepts_exact_codes_with_surrounding_whitespace(
    text: str, expected: Timeframe
) -> None:
    assert Timeframe.parse(text) is expected


@pytest.mark.parametrize("member", list(Timeframe))
def test_parse_returns_members_unchanged(member: Timeframe) -> None:
    assert Timeframe.parse(member) is member


@pytest.mark.parametrize(
    "text",
    [
        "1H",
        "4H",
        "1D",
        "60m",
        "240m",
        "24h",
        "1hour",
        "daily",
        "D",
        "15m",
        "1w",
        "1wk",
        "1mo",
        "",
        "   ",
        "1 h",
    ],
)
def test_parse_rejects_other_spellings(text: str) -> None:
    with pytest.raises(UnknownTimeframeError) as caught:
        Timeframe.parse(text)

    assert isinstance(caught.value, ValueError)
    message = str(caught.value)
    assert repr(text[:32]) in message
    assert "1h, 4h, 1d" in message


def test_unknown_timeframe_error_is_a_value_error() -> None:
    assert issubclass(UnknownTimeframeError, ValueError)


def test_parse_message_is_bounded_for_long_inputs() -> None:
    text = "x" * 10_000

    with pytest.raises(UnknownTimeframeError) as caught:
        Timeframe.parse(text)

    message = str(caught.value)
    assert repr("x" * 32) in message
    assert "x" * 33 not in message
    assert len(message) < 200


@pytest.mark.parametrize("value", [None, 1, b"1h"])
def test_parse_rejects_non_strings(value: object) -> None:
    with pytest.raises(TypeError, match="str"):
        Timeframe.parse(value)  # type: ignore[arg-type]


def test_enum_constructor_stays_exact() -> None:
    assert Timeframe("1d") is Timeframe.D1
    with pytest.raises(ValueError):
        Timeframe(" 1d ")


# --- AC4: nominal_close --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("member", "open_time", "expected"),
    [
        (Timeframe.D1, "2024-01-02T05:00:00+00:00", "2024-01-03T05:00:00+00:00"),
        (Timeframe.H1, "2024-01-02T14:30:00+00:00", "2024-01-02T15:30:00+00:00"),
        (Timeframe.H4, "2024-01-02T18:30:00+00:00", "2024-01-02T22:30:00+00:00"),
    ],
)
def test_nominal_close_golden_cases(member: Timeframe, open_time: str, expected: str) -> None:
    result = member.nominal_close(datetime.fromisoformat(open_time))

    assert type(result) is datetime
    assert result.tzinfo is UTC
    assert result.isoformat() == expected


def test_daily_nominal_close_of_a_friday_is_on_saturday() -> None:
    friday = datetime(2024, 1, 5, 5, 0, tzinfo=UTC)
    assert friday.weekday() == 4

    result = Timeframe.D1.nominal_close(friday)

    assert result.weekday() == 5
    assert result == datetime(2024, 1, 6, 5, 0, tzinfo=UTC)


def test_hourly_nominal_close_ignores_daylight_saving_changes() -> None:
    open_time = datetime(2024, 3, 10, 6, 30, tzinfo=UTC)  # 01:30 EST, right before the US change

    result = Timeframe.H1.nominal_close(open_time)

    assert result - open_time == timedelta(hours=1)
    assert result.isoformat() == "2024-03-10T07:30:00+00:00"


@pytest.mark.parametrize("member", list(Timeframe))
@pytest.mark.parametrize(
    "open_time",
    [
        pytest.param(datetime(2024, 1, 2, 14, 30, tzinfo=UTC), id="stdlib-utc"),
        pytest.param(datetime(2024, 1, 2, 9, 30, tzinfo=timezone(timedelta(hours=-5))), id="fixed"),
        pytest.param(pd.Timestamp("2024-01-02T14:30:00", tz="UTC"), id="timestamp-utc"),
        pytest.param(pd.Timestamp("2024-01-02T09:30:00", tz="America/New_York"), id="timestamp-ny"),
    ],
)
def test_nominal_close_is_equal_for_equal_instants(member: Timeframe, open_time: datetime) -> None:
    result = member.nominal_close(open_time)

    assert type(result) is datetime
    assert result.tzinfo is UTC
    assert result == datetime(2024, 1, 2, 14, 30, tzinfo=UTC) + member.duration


@pytest.mark.parametrize("member", list(Timeframe))
def test_nominal_close_rejects_naive_open_times(member: Timeframe) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        member.nominal_close(datetime(2024, 1, 2, 14, 30))


ESCAPE = chr(0x1B)
NEWLINE = chr(0x0A)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(ESCAPE * 10_000, id="escape-characters"),
        pytest.param(chr(0xE0001) * 10_000, id="astral-non-printable"),
        pytest.param(f"{ESCAPE}[31m1h{ESCAPE}[0m{NEWLINE}" * 1_000, id="ansi-and-newlines"),
    ],
)
def test_parse_messages_stay_bounded_for_escape_heavy_inputs(text: str) -> None:
    with pytest.raises(UnknownTimeframeError) as caught:
        Timeframe.parse(text)

    message = str(caught.value)
    assert NEWLINE not in message
    assert ESCAPE not in message
    assert len(message) < 200
    assert any(repr(text[:n]) in message for n in range(1, 33))
