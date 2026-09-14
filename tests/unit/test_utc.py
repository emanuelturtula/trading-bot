"""Tests of the UTC helper (spec 004, AC1)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trading_bot.domain.utc import to_utc

NEW_YORK = ZoneInfo("America/New_York")
MINUS_FIVE = timezone(timedelta(hours=-5))

# The same instant, 2024-01-02T15:30:00.123456+00:00, in several aware representations.
SAME_INSTANT = [
    pytest.param(datetime(2024, 1, 2, 15, 30, 0, 123456, tzinfo=UTC), id="stdlib-utc"),
    pytest.param(datetime(2024, 1, 2, 10, 30, 0, 123456, tzinfo=MINUS_FIVE), id="fixed-offset"),
    pytest.param(datetime(2024, 1, 2, 10, 30, 0, 123456, tzinfo=NEW_YORK), id="zoneinfo"),
    pytest.param(pd.Timestamp("2024-01-02T15:30:00.123456", tz="UTC"), id="timestamp-utc"),
    pytest.param(
        pd.Timestamp("2024-01-02T10:30:00.123456", tz="America/New_York"), id="timestamp-new-york"
    ),
]
EXPECTED_INSTANT = datetime(2024, 1, 2, 15, 30, 0, 123456, tzinfo=UTC)


@pytest.mark.parametrize("value", SAME_INSTANT)
def test_aware_values_become_stdlib_utc_datetimes(value: datetime) -> None:
    result = to_utc(value)

    assert type(result) is datetime
    assert result.tzinfo is UTC
    assert result == value.astimezone(UTC)
    assert result == EXPECTED_INSTANT


@pytest.mark.parametrize("value", SAME_INSTANT)
def test_equal_instants_give_equal_results(value: datetime) -> None:
    assert to_utc(value) == to_utc(datetime(2024, 1, 2, 15, 30, 0, 123456, tzinfo=UTC))
    assert str(to_utc(value)) == str(EXPECTED_INSTANT)


@pytest.mark.parametrize(
    ("fold", "expected"),
    [
        pytest.param(0, datetime(2024, 11, 3, 5, 30, tzinfo=UTC), id="fold-0"),
        pytest.param(1, datetime(2024, 11, 3, 6, 30, tzinfo=UTC), id="fold-1"),
    ],
)
def test_wall_times_in_a_dst_fold_are_accepted(fold: int, expected: datetime) -> None:
    value = datetime(2024, 11, 3, 1, 30, tzinfo=NEW_YORK, fold=fold)

    result = to_utc(value)

    assert result == expected
    assert result.tzinfo is UTC
    assert result.isoformat() == expected.isoformat()


@pytest.mark.parametrize("value", SAME_INSTANT)
def test_is_idempotent(value: datetime) -> None:
    once = to_utc(value)
    twice = to_utc(once)

    assert twice == once
    assert type(twice) is datetime
    assert twice.tzinfo is UTC


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(datetime(2024, 1, 2, 15, 30), id="naive-datetime"),
        pytest.param(pd.Timestamp("2024-01-02T15:30:00"), id="naive-timestamp"),
        pytest.param(pd.NaT, id="nat"),
    ],
)
def test_naive_values_and_nat_are_rejected(value: datetime) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        to_utc(value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(pd.Timestamp("2024-01-02T15:30:00.000000001", tz="UTC"), id="utc"),
        pytest.param(
            pd.Timestamp("2024-01-02T10:30:00.123456789", tz="America/New_York"), id="new-york"
        ),
    ],
)
def test_sub_microsecond_timestamps_are_rejected(value: pd.Timestamp) -> None:
    with pytest.raises(ValueError, match="microsecond"):
        to_utc(value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("2024-01-02T15:30:00+00:00", id="str"),
        pytest.param(1_704_209_400, id="int"),
        pytest.param(date(2024, 1, 2), id="date"),
        pytest.param(None, id="none"),
    ],
)
def test_non_datetimes_are_rejected(value: object) -> None:
    with pytest.raises(TypeError, match="datetime"):
        to_utc(value)  # type: ignore[arg-type]
