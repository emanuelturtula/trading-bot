"""Tests of the weekly timeframe (spec 027, AC1-AC4, AC6-AC7).

The calendar grid of ``1w`` on the NYSE calendar, the daily-to-weekly resampling, its anti
look-ahead property, the Yahoo provider path and the signal message. Expectations are literals
read from the NYSE calendar, or recomputed with pandas ``groupby``, never with the code under test.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.calendars import nyse_test_calendar, utc
from tests.fixtures.session_candles import session_candles
from tests.fixtures.yahoo_recordings import RecordedYahooClient, load_recording
from trading_bot.data.errors import CandleNotPublishedError, UnpublishedReason
from trading_bot.data.provider import CandleRequest
from trading_bot.data.transport import ProviderTransport, RateLimit, RetryPolicy, TokenBucket
from trading_bot.data.yahoo.history import HistoryQuery
from trading_bot.data.yahoo.provider import YFinanceProvider, plan_history, publishable_1w
from trading_bot.domain.candle_normalization import normalize_candles
from trading_bot.domain.candle_resampling import resample_daily_to_1w
from trading_bot.domain.market_calendar.sessions import (
    CalendarRangeError,
    CandleLabelError,
    CandleLabelErrorKind,
)
from trading_bot.domain.timeframe import Timeframe

NYSE = nyse_test_calendar()
D1 = Timeframe.D1
W1 = Timeframe.W1


def minutes(value: datetime) -> str:
    return value.replace(tzinfo=None).isoformat(timespec="minutes")


# --- AC1: the timeframe ------------------------------------------------------------------------


def test_1w_parses_and_lasts_seven_days() -> None:
    assert Timeframe.parse(" 1w ") is W1
    assert W1.duration == timedelta(days=7)
    assert W1.nominal_close(utc("2024-07-01T04:00")) == utc("2024-07-08T04:00")


# --- AC2/AC3: the calendar grid ----------------------------------------------------------------


def test_a_regular_week_runs_from_the_monday_open_to_the_friday_close() -> None:
    (week,) = NYSE.candle_slots(W1, utc("2024-06-10T00:00"), utc("2024-06-11T00:00"))

    assert week.timeframe is W1
    assert week.session_day.isoformat() == "2024-06-10"
    assert minutes(week.label) == "2024-06-10T04:00"  # 00:00 New York, the 1d label
    assert minutes(week.open_time) == "2024-06-10T13:30"
    assert minutes(week.close_time) == "2024-06-14T20:00"
    assert week.label == NYSE.candle_slot(D1, week.label).label


def test_a_holiday_week_starts_with_its_first_session_and_ends_with_its_last() -> None:
    # 2024-01-15 is Martin Luther King Jr. Day; 2024-03-29 is Good Friday.
    (mlk,) = NYSE.candle_slots(W1, utc("2024-01-15T00:00"), utc("2024-01-17T00:00"))
    (easter,) = NYSE.candle_slots(W1, utc("2024-03-25T00:00"), utc("2024-03-26T00:00"))

    assert (minutes(mlk.label), minutes(mlk.close_time)) == ("2024-01-16T05:00", "2024-01-19T21:00")
    assert (minutes(easter.label), minutes(easter.close_time)) == (
        "2024-03-25T04:00",
        "2024-03-28T20:00",
    )


def test_a_thanksgiving_week_closes_with_the_half_day() -> None:
    (week,) = NYSE.candle_slots(W1, utc("2025-11-24T00:00"), utc("2025-11-25T00:00"))

    assert minutes(week.close_time) == "2025-11-28T18:00"


def test_weeks_are_contiguous_and_cover_every_session_once() -> None:
    start, end = utc("2024-01-01T00:00"), utc("2025-01-01T00:00")
    weeks = NYSE.candle_slots(W1, start, end)
    days = NYSE.candle_slots(D1, start, end)

    assert len(weeks) == 53  # 52 ISO weeks of 2024 and the one starting on 2024-12-30
    covered = [
        day for day in days for week in weeks if week.open_time <= day.open_time < week.close_time
    ]
    assert covered == list(days)
    assert all(NYSE.candle_slot(W1, week.label) == week for week in weeks)
    year_week = {day.session_day.isocalendar()[:2] for day in days}
    assert len(year_week) == len(weeks)


def test_closed_candles_and_next_close_follow_the_last_session() -> None:
    thursday = utc("2024-06-13T15:00")
    friday_close = utc("2024-06-14T20:00")

    assert NYSE.next_candle_close(W1, thursday) == friday_close
    assert [minutes(w.label) for w in NYSE.closed_candles(W1, thursday, 2)] == [
        "2024-05-28T04:00",  # Memorial Day week starts on Tuesday
        "2024-06-03T04:00",
    ]
    assert minutes(NYSE.closed_candles(W1, friday_close, 1)[-1].label) == "2024-06-10T04:00"
    assert NYSE.next_candle_close(W1, friday_close) == utc("2024-06-21T20:00")


def test_too_few_closed_weeks_raise_calendar_range_error() -> None:
    with pytest.raises(CalendarRangeError, match="fewer than requested"):
        NYSE.closed_candles(W1, utc("2021-01-20T00:00"), 5)


def test_no_week_closing_after_the_end_raises_calendar_range_error() -> None:
    with pytest.raises(CalendarRangeError, match="closes after"):
        NYSE.next_candle_close(W1, NYSE.coverage_end - timedelta(hours=1))


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("2024-06-11T04:00", CandleLabelErrorKind.OFF_GRID),  # Tuesday: a session, not the first
        ("2024-06-10T13:30", CandleLabelErrorKind.OFF_GRID),  # the open, not the label
        ("2024-06-15T04:00", CandleLabelErrorKind.NOT_A_SESSION),  # Saturday
    ],
)
def test_other_labels_are_rejected_with_their_kind(label: str, kind: CandleLabelErrorKind) -> None:
    with pytest.raises(CandleLabelError) as raised:
        NYSE.candle_slot(W1, utc(label))
    assert raised.value.kind is kind
    assert raised.value.timeframe is W1


# --- AC4: resampling ---------------------------------------------------------------------------


def expected_weeks(daily: pd.DataFrame) -> pd.DataFrame:
    """Weeks recomputed with pandas: group the daily rows by ISO week in New York time."""
    local = daily.index.tz_convert("America/New_York")
    keys = [f"{label.isocalendar()[0]}-{label.isocalendar()[1]:02d}" for label in local]
    grouped = daily.groupby(keys, sort=True)
    frame = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
        }
    )
    frame.index = pd.DatetimeIndex(grouped.apply(lambda rows: rows.index[0]).to_list())
    return frame


def test_weeks_aggregate_their_daily_rows() -> None:
    daily = session_candles(NYSE, D1, utc("2024-01-01T00:00"), utc("2024-04-01T00:00"), seed=3)
    now = utc("2024-03-29T00:00")  # after Thursday's close of the Good Friday week

    result = resample_daily_to_1w(daily, now, calendar=NYSE)

    expected = expected_weeks(daily.loc[daily.index < pd.Timestamp(now)])
    np.testing.assert_array_equal(result.candles.to_numpy(), expected.to_numpy())
    assert list(result.candles.index) == list(expected.index)
    assert all(entry.bars >= 3 and not entry.missing for entry in result.slots)


def test_a_missing_day_is_reported_and_a_missing_last_day_withholds_the_week() -> None:
    daily = session_candles(NYSE, D1, utc("2024-06-03T00:00"), utc("2024-06-15T00:00"))
    daily = daily.drop(index=[pd.Timestamp("2024-06-05T04:00Z"), pd.Timestamp("2024-06-14T04:00Z")])
    now = utc("2024-06-14T20:02")
    last = NYSE.closed_candles(W1, now, 1)[-1]

    result = resample_daily_to_1w(daily, now, calendar=NYSE)

    assert [
        (e.bars, [minutes(m) for m in e.missing], e.closing_bar_missing) for e in result.slots
    ] == [
        (4, ["2024-06-05T04:00"], False),
        (4, ["2024-06-14T04:00"], True),
    ]
    assert len(publishable_1w(result, last=last)) == 1


def test_a_week_with_its_last_day_keeps_every_row() -> None:
    daily = session_candles(NYSE, D1, utc("2024-06-03T00:00"), utc("2024-06-15T00:00"))
    now = utc("2024-06-14T20:02")
    result = resample_daily_to_1w(daily, now, calendar=NYSE)

    assert publishable_1w(result, last=NYSE.closed_candles(W1, now, 1)[-1]) is result.candles


def test_publishable_1w_rejects_arguments_of_the_wrong_type() -> None:
    daily = session_candles(NYSE, D1, utc("2024-06-03T00:00"), utc("2024-06-15T00:00"))
    now = utc("2024-06-14T20:02")
    result = resample_daily_to_1w(daily, now, calendar=NYSE)
    with pytest.raises(TypeError, match="resampled"):
        publishable_1w(daily, last=NYSE.closed_candles(W1, now, 1)[-1])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="last"):
        publishable_1w(result, last=now)  # type: ignore[arg-type]


def test_a_daily_row_that_is_not_a_session_label_is_rejected() -> None:
    daily = session_candles(NYSE, D1, utc("2024-06-03T00:00"), utc("2024-06-15T00:00"))
    moved = daily.rename(index={daily.index[1]: daily.index[1] + pd.Timedelta(hours=1)})
    with pytest.raises(CandleLabelError):
        resample_daily_to_1w(moved, utc("2024-06-14T20:02"), calendar=NYSE)


# --- AC7: anti look-ahead ----------------------------------------------------------------------


@pytest.mark.parametrize("cut", ["2024-06-12T15:00", "2024-06-14T20:00", "2024-06-17T04:00"])
def test_weeks_at_t_ignore_daily_rows_after_t(cut: str) -> None:
    daily = session_candles(NYSE, D1, utc("2024-05-01T00:00"), utc("2024-07-01T00:00"), seed=7)
    now = utc(cut)

    full = resample_daily_to_1w(daily, now, calendar=NYSE)
    truncated = resample_daily_to_1w(daily.loc[daily.index < pd.Timestamp(now)], now, calendar=NYSE)

    pd.testing.assert_frame_equal(full.candles, truncated.candles, check_exact=True)
    assert full.slots == truncated.slots
    assert full.slots[-1].slot.close_time <= now


# --- AC4: the Yahoo provider -------------------------------------------------------------------


def make_provider(client: RecordedYahooClient) -> YFinanceProvider:
    async def no_sleep(_: float) -> None:
        return None

    transport = ProviderTransport(
        policy=RetryPolicy(),
        limiter=TokenBucket(RateLimit(), clock=lambda: 0.0, sleep=no_sleep),
        sleep=no_sleep,
        jitter=lambda: 0.5,
    )
    return YFinanceProvider(client=client, transport=transport, calendar=NYSE)


def recorded(name: str) -> tuple[RecordedYahooClient, pd.DataFrame]:
    recording = load_recording(name)
    client = RecordedYahooClient(
        histories={(recording.symbol, recording.interval): recording.frame}
    )
    return client, recording.frame


def test_a_weekly_plan_asks_for_uncapped_daily_bars() -> None:
    now = utc("2026-09-17T15:00")
    plan = plan_history(
        CandleRequest(ticker="SPY", timeframe=W1, lookback=200, now=now), calendar=NYSE
    )

    assert plan.interval == "1d"
    assert not plan.capped
    assert plan.start == NYSE.closed_candles(W1, now, 200)[0].label
    assert plan.start < now - timedelta(days=720)


def test_a_weekly_fetch_returns_closed_weeks_built_from_daily_bars() -> None:
    client, raw = recorded("nvda_1d_2024-06-03")
    now = utc("2024-06-14T20:00:30")

    frame = asyncio.run(make_provider(client).fetch_candles("NVDA", W1, 2, now=now))

    assert client.queries == (
        HistoryQuery(symbol="NVDA", interval="1d", start=utc("2024-06-03T04:00")),
    )
    daily = normalize_candles(raw, D1, calendar=NYSE).candles
    expected = expected_weeks(daily)
    assert [minutes(label) for label in frame.index] == ["2024-06-03T04:00", "2024-06-10T04:00"]
    np.testing.assert_array_equal(frame.to_numpy(), expected.to_numpy())


def test_a_holiday_week_closes_with_the_half_day_and_the_next_week_waits() -> None:
    client, _ = recorded("spy_1d_2025-11-24")
    provider = make_provider(client)

    closed = asyncio.run(provider.fetch_candles("SPY", W1, 1, now=utc("2025-11-28T18:00:30")))
    later = asyncio.run(provider.fetch_candles("SPY", W1, 1, now=utc("2025-12-02T21:00:30")))

    assert [minutes(label) for label in closed.index] == ["2025-11-24T05:00"]
    pd.testing.assert_frame_equal(closed, later, check_exact=True)


def test_a_week_without_its_last_daily_bar_is_not_published(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recording = load_recording("nvda_1d_2024-06-03")
    client = RecordedYahooClient(histories={("NVDA", "1d"): recording.frame.iloc[:-1]})
    caplog.set_level("DEBUG", logger="trading_bot.data.yahoo.provider")

    with pytest.raises(CandleNotPublishedError) as raised:
        asyncio.run(
            make_provider(client).fetch_candles("NVDA", W1, 2, now=utc("2024-06-14T20:00:30"))
        )

    assert raised.value.reason is UnpublishedReason.MISSING
    assert raised.value.timeframe is W1
    assert any("daily bar" in record.getMessage() for record in caplog.records)


def test_a_week_with_a_missing_day_is_built_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    recording = load_recording("nvda_1d_2024-06-03")
    client = RecordedYahooClient(
        histories={("NVDA", "1d"): recording.frame.drop(recording.frame.index[[2, 7]])}
    )
    caplog.set_level("DEBUG", logger="trading_bot.data.yahoo.provider")

    frame = asyncio.run(
        make_provider(client).fetch_candles("NVDA", W1, 2, now=utc("2024-06-14T20:00:30"))
    )

    assert len(frame) == 2
    messages = [(record.levelname, record.getMessage()) for record in caplog.records]
    assert any(level == "DEBUG" and "incomplete daily bars" in text for level, text in messages)
    assert any(level == "WARNING" and "of 5 daily bars" in text for level, text in messages)
