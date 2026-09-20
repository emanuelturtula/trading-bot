"""The cooldown gate: counted by candle position, never by a duration (spec 015, T2, AC6, AC8).

Every expectation is written out by hand, including the golden table of spec 015 Design 5.2
around the 2024-07-04 holiday, whose point is that a duration-based count would give four
"bars" where three candles closed. No clock is read: every instant is a literal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pandas as pd
import pytest

from tests.fixtures.calendars import nyse_test_calendar, utc
from tests.fixtures.session_candles import session_candles
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine.cooldown import CooldownDecision, cooldown_decision, cooldown_decisions

ALLOWED = CooldownDecision.ALLOWED
BLOCKED = CooldownDecision.BLOCKED
SUPERSEDED = CooldownDecision.SUPERSEDED

# The daily candle the previous signal fired on, and the close that identifies it.
PREVIOUS_LABEL = utc("2024-07-01T04:00")
PREVIOUS_CLOSE = utc("2024-07-02T04:00")


def labels(*texts: str) -> pd.DatetimeIndex:
    """A canonical, tz-aware index of candle labels."""
    return pd.DatetimeIndex(
        [utc(text) for text in texts], dtype=pd.DatetimeTZDtype(unit="us", tz="UTC")
    )


def holiday_week() -> pd.DatetimeIndex:
    """The NYSE ``1d`` labels of spec 015 Design 5.2, from 2024-06-20 to 2024-07-09."""
    frame = session_candles(
        nyse_test_calendar(), Timeframe.D1, utc("2024-06-20T00:00"), utc("2024-07-10T00:00")
    )
    return pd.DatetimeIndex(frame.index)


def decide(
    index: pd.DatetimeIndex, cooldown_bars: int, previous_close: datetime | None = PREVIOUS_CLOSE
) -> dict[str, CooldownDecision]:
    """Every decision of ``index``, keyed by the ISO label, so failures name the candle."""
    decisions = cooldown_decisions(
        index,
        timeframe=Timeframe.D1,
        cooldown_bars=cooldown_bars,
        previous_close=previous_close,
    )
    return {label.isoformat(): decision for label, decision in zip(index, decisions, strict=True)}


# --- The decision table of Design 5.1 ------------------------------------------------------


def test_no_previous_signal_is_always_allowed() -> None:
    index = holiday_week()

    decisions = cooldown_decisions(
        index, timeframe=Timeframe.D1, cooldown_bars=500, previous_close=None
    )

    assert set(decisions) == {ALLOWED}
    assert len(decisions) == len(index)


def test_a_previous_label_after_the_candle_supersedes_it() -> None:
    index = labels("2024-06-26T04:00", "2024-06-27T04:00", "2024-06-28T04:00")

    assert decide(index, 0) == {
        "2024-06-26T04:00:00+00:00": SUPERSEDED,
        "2024-06-27T04:00:00+00:00": SUPERSEDED,
        "2024-06-28T04:00:00+00:00": SUPERSEDED,
    }


def test_the_same_candle_is_allowed_so_that_record_stays_the_arbiter() -> None:
    """Design 5.1: the equal label falls through to ``record``, the arbiter of rule 5."""
    index = labels("2024-06-28T04:00", "2024-07-01T04:00")

    for cooldown_bars in (0, 1, 2, 500):
        assert decide(index, cooldown_bars)["2024-07-01T04:00:00+00:00"] == ALLOWED


def test_a_cooldown_of_zero_never_blocks() -> None:
    index = holiday_week()

    assert BLOCKED not in set(decide(index, 0).values())


# --- The golden table of Design 5.2 (AC6, AC8) ---------------------------------------------


@pytest.mark.parametrize(
    ("cooldown_bars", "expected"),
    [
        (
            0,
            {
                "2024-06-28T04:00:00+00:00": SUPERSEDED,
                "2024-07-01T04:00:00+00:00": ALLOWED,
                "2024-07-02T04:00:00+00:00": ALLOWED,
                "2024-07-03T04:00:00+00:00": ALLOWED,
                "2024-07-05T04:00:00+00:00": ALLOWED,
            },
        ),
        (
            1,
            {
                "2024-06-28T04:00:00+00:00": SUPERSEDED,
                "2024-07-01T04:00:00+00:00": ALLOWED,
                "2024-07-02T04:00:00+00:00": BLOCKED,
                "2024-07-03T04:00:00+00:00": ALLOWED,
                "2024-07-05T04:00:00+00:00": ALLOWED,
            },
        ),
        (
            2,
            {
                "2024-06-28T04:00:00+00:00": SUPERSEDED,
                "2024-07-01T04:00:00+00:00": ALLOWED,
                "2024-07-02T04:00:00+00:00": BLOCKED,
                "2024-07-03T04:00:00+00:00": BLOCKED,
                "2024-07-05T04:00:00+00:00": ALLOWED,
            },
        ),
        (
            500,
            {
                "2024-06-28T04:00:00+00:00": SUPERSEDED,
                "2024-07-01T04:00:00+00:00": ALLOWED,
                "2024-07-02T04:00:00+00:00": BLOCKED,
                "2024-07-03T04:00:00+00:00": BLOCKED,
                "2024-07-05T04:00:00+00:00": BLOCKED,
            },
        ),
    ],
)
def test_the_golden_table_around_the_holiday(
    cooldown_bars: int, expected: dict[str, CooldownDecision]
) -> None:
    decisions = decide(holiday_week(), cooldown_bars)

    assert {label: decisions[label] for label in expected} == expected


def test_the_holiday_week_holds_the_labels_the_golden_table_names() -> None:
    """The gap is the point: 2024-07-04 is a holiday and 07-06/07 a weekend."""
    index = holiday_week()

    assert [label.isoformat() for label in index[-6:]] == [
        "2024-07-01T04:00:00+00:00",
        "2024-07-02T04:00:00+00:00",
        "2024-07-03T04:00:00+00:00",
        "2024-07-05T04:00:00+00:00",  # 2024-07-04 is a holiday
        "2024-07-08T04:00:00+00:00",  # 2024-07-06 and 07-07 are a weekend
        "2024-07-09T04:00:00+00:00",
    ]


def test_three_candles_closed_where_a_duration_would_count_four_days() -> None:
    """2024-07-05 is four calendar days after the previous label but three candles later."""
    index = holiday_week()
    evaluated = utc("2024-07-05T04:00")

    assert evaluated - PREVIOUS_LABEL == timedelta(days=4)
    assert decide(index, 3)["2024-07-05T04:00:00+00:00"] == BLOCKED
    assert decide(index, 2)["2024-07-05T04:00:00+00:00"] == ALLOWED


# --- Frame shapes ---------------------------------------------------------------------------


def test_a_previous_label_before_the_frame_start_counts_every_row() -> None:
    index = labels("2024-07-02T04:00", "2024-07-03T04:00", "2024-07-05T04:00")

    assert decide(index, 2) == {
        "2024-07-02T04:00:00+00:00": BLOCKED,  # one row in the window
        "2024-07-03T04:00:00+00:00": BLOCKED,  # two rows
        "2024-07-05T04:00:00+00:00": ALLOWED,  # three rows
    }


def test_a_history_shorter_than_the_cooldown_errs_towards_blocked() -> None:
    """Design 5.1: the conservative direction, fewer notifications, is deliberate."""
    index = labels("2024-07-03T04:00", "2024-07-05T04:00")

    assert decide(index, 5) == {
        "2024-07-03T04:00:00+00:00": BLOCKED,
        "2024-07-05T04:00:00+00:00": BLOCKED,
    }


def test_a_frame_of_one_candle() -> None:
    index = labels("2024-07-05T04:00")

    assert decide(index, 0) == {"2024-07-05T04:00:00+00:00": ALLOWED}
    assert decide(index, 1) == {"2024-07-05T04:00:00+00:00": BLOCKED}
    assert decide(index, 1, None) == {"2024-07-05T04:00:00+00:00": ALLOWED}


def test_gaps_in_the_frame_do_not_add_bars() -> None:
    index = labels("2024-07-01T04:00", "2024-07-05T04:00")

    assert decide(index, 1) == {
        "2024-07-01T04:00:00+00:00": ALLOWED,  # the same candle
        "2024-07-05T04:00:00+00:00": BLOCKED,  # one row in the window, not four days
    }


def test_labels_are_compared_as_instants_whatever_their_zone_or_unit() -> None:
    index = pd.DatetimeIndex([utc("2024-07-02T04:00"), utc("2024-07-03T04:00")]).tz_convert(
        "America/New_York"
    )
    elsewhere = PREVIOUS_CLOSE.astimezone(timezone(timedelta(hours=5, minutes=30)))

    assert cooldown_decisions(
        index, timeframe=Timeframe.D1, cooldown_bars=1, previous_close=elsewhere
    ) == (BLOCKED, ALLOWED)


# --- The two entry points agree -------------------------------------------------------------


def test_the_point_in_time_form_is_the_last_of_the_series_form() -> None:
    index = holiday_week()

    for cooldown_bars in (0, 1, 2, 7):
        for length in range(1, len(index) + 1):
            prefix = index[:length]
            assert (
                cooldown_decision(
                    prefix,
                    timeframe=Timeframe.D1,
                    cooldown_bars=cooldown_bars,
                    previous_close=PREVIOUS_CLOSE,
                )
                == cooldown_decisions(
                    prefix,
                    timeframe=Timeframe.D1,
                    cooldown_bars=cooldown_bars,
                    previous_close=PREVIOUS_CLOSE,
                )[-1]
            )


def test_a_prefix_gives_the_prefix_of_the_decisions() -> None:
    index = holiday_week()
    whole = cooldown_decisions(
        index, timeframe=Timeframe.D1, cooldown_bars=2, previous_close=PREVIOUS_CLOSE
    )

    for length in range(1, len(index) + 1):
        assert (
            cooldown_decisions(
                index[:length],
                timeframe=Timeframe.D1,
                cooldown_bars=2,
                previous_close=PREVIOUS_CLOSE,
            )
            == whole[:length]
        )


def test_the_timeframe_duration_places_the_previous_label() -> None:
    """``previous_label = previous_close - timeframe.duration``, subtracted exactly."""
    index = labels("2024-07-05T13:30", "2024-07-05T14:30", "2024-07-05T15:30")

    assert cooldown_decisions(
        index,
        timeframe=Timeframe.H1,
        cooldown_bars=1,
        previous_close=utc("2024-07-05T14:30"),  # the candle labelled 13:30
    ) == (ALLOWED, BLOCKED, ALLOWED)


# --- Argument rejections ---------------------------------------------------------------------


def test_a_non_index_is_a_type_error() -> None:
    for call in (cooldown_decision, cooldown_decisions):
        with pytest.raises(TypeError, match="DatetimeIndex"):
            call(
                [utc("2024-07-05T04:00")],  # type: ignore[arg-type]
                timeframe=Timeframe.D1,
                cooldown_bars=0,
                previous_close=None,
            )


def test_an_empty_index_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="at least one label"):
        cooldown_decisions(labels(), timeframe=Timeframe.D1, cooldown_bars=0, previous_close=None)


def test_a_naive_index_is_a_value_error() -> None:
    index = pd.DatetimeIndex([datetime(2024, 7, 5, 4, 0)])  # the rejected case: naive

    with pytest.raises(ValueError, match="timezone-aware"):
        cooldown_decisions(index, timeframe=Timeframe.D1, cooldown_bars=0, previous_close=None)


def test_an_unsorted_index_is_a_value_error() -> None:
    index = labels("2024-07-05T04:00", "2024-07-03T04:00")

    with pytest.raises(ValueError, match="increasing"):
        cooldown_decisions(index, timeframe=Timeframe.D1, cooldown_bars=0, previous_close=None)


def test_a_repeated_label_is_a_value_error() -> None:
    index = labels("2024-07-03T04:00", "2024-07-03T04:00")

    with pytest.raises(ValueError, match="increasing"):
        cooldown_decisions(index, timeframe=Timeframe.D1, cooldown_bars=0, previous_close=None)


def test_a_non_timeframe_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="Timeframe"):
        cooldown_decisions(
            labels("2024-07-05T04:00"),
            timeframe="1d",  # type: ignore[arg-type]
            cooldown_bars=0,
            previous_close=None,
        )


@pytest.mark.parametrize("cooldown_bars", [True, 1.0, "1"])
def test_a_non_integer_cooldown_is_a_type_error(cooldown_bars: object) -> None:
    with pytest.raises(TypeError, match="cooldown_bars"):
        cooldown_decisions(
            labels("2024-07-05T04:00"),
            timeframe=Timeframe.D1,
            cooldown_bars=cooldown_bars,  # type: ignore[arg-type]
            previous_close=None,
        )


def test_a_negative_cooldown_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="cooldown_bars"):
        cooldown_decisions(
            labels("2024-07-05T04:00"),
            timeframe=Timeframe.D1,
            cooldown_bars=-1,
            previous_close=None,
        )


def test_a_naive_previous_close_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="aware"):
        cooldown_decisions(
            labels("2024-07-05T04:00"),
            timeframe=Timeframe.D1,
            cooldown_bars=0,
            previous_close=datetime(2024, 7, 2, 4, 0),  # the rejected case: naive
        )


def test_a_previous_close_that_is_not_a_datetime_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="datetime"):
        cooldown_decisions(
            labels("2024-07-05T04:00"),
            timeframe=Timeframe.D1,
            cooldown_bars=0,
            previous_close="2024-07-02T04:00:00+00:00",  # type: ignore[arg-type]
        )


def test_the_decision_enumeration_holds_exactly_three_values() -> None:
    assert [member.value for member in CooldownDecision] == ["allowed", "blocked", "superseded"]
    assert CooldownDecision.ALLOWED == "allowed"
    assert str(CooldownDecision.BLOCKED) == "blocked"
    assert UTC is PREVIOUS_CLOSE.tzinfo
