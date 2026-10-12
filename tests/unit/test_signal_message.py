"""The text of a signal message (spec 017, AC7, AC8; decision D9).

Every expected text is a literal. Candle closes are checked against the real NYSE close in the
display zone, never the nominal close (spec 004 §5): the second ``4h`` candle of a session
closes at 16:00 New York, 22:00 Madrid in summer, not 17:30 New York.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tests.fixtures.calendars import nyse_test_calendar, utc
from tests.fixtures.signal_messages import MADRID, notification
from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe
from trading_bot.notifications.message import format_price, format_signal_message
from trading_bot.notifications.notifier import SignalNotification


def render(item: SignalNotification, *, production: bool = False) -> str:
    return format_signal_message(
        item, production=production, display_tz=MADRID, calendar=nyse_test_calendar()
    )


def test_a_daily_beta_message_matches_the_golden_text() -> None:
    assert render(notification()) == (
        "<b>[BETA] 🟢 BUY · AAPL · 1d</b>\n"
        "Rule: RSI bounce\n"
        "Close: 182.41\n"
        "Candle: 2024-07-01 (session)\n"
        "rsi_14: 28.73\n"
        "sma_50: 190.12\n"
        "\n"
        "<i>Not financial advice.</i>"
    )


def test_production_drops_the_beta_prefix_and_keeps_the_disclaimer() -> None:
    text = render(notification(side=Side.SELL), production=True)

    assert text.startswith("<b>🔴 SELL · AAPL · 1d</b>\n")
    assert "[BETA]" not in text
    assert text.endswith("<i>Not financial advice.</i>")


@pytest.mark.parametrize(
    ("timeframe", "label", "expected"),
    [
        (Timeframe.H1, utc("2024-07-01T13:30"), "closed 2024-07-01 16:30 CEST"),
        (Timeframe.H4, utc("2024-07-01T13:30"), "closed 2024-07-01 19:30 CEST"),
        # The session's second 4h candle is truncated by the 16:00 New York close.
        (Timeframe.H4, utc("2024-07-01T17:30"), "closed 2024-07-01 22:00 CEST"),
        # Half day: the only 4h candle ends at the 13:00 New York early close, not 17:30 UTC.
        (Timeframe.H4, utc("2024-07-03T13:30"), "closed 2024-07-03 19:00 CEST"),
        (Timeframe.H1, utc("2024-01-02T14:30"), "closed 2024-01-02 16:30 CET"),
    ],
)
def test_intraday_candles_show_the_real_close_in_the_display_zone(
    timeframe: Timeframe, label: datetime, expected: str
) -> None:
    text = render(notification(timeframe=timeframe, label=label))

    assert f"Candle: {expected}\n" in text


def test_a_weekly_candle_shows_the_day_of_its_first_session() -> None:
    # 2024-05-27 is Memorial Day, so the week starts on Tuesday.
    text = render(notification(timeframe=Timeframe.W1, label=utc("2024-05-28T04:00")))

    assert "<b>[BETA] 🟢 BUY · AAPL · 1w</b>\n" in text
    assert "Candle: week of 2024-05-28\n" in text


def test_an_off_grid_candle_shows_its_open_instead_of_a_guessed_close() -> None:
    text = render(notification(timeframe=Timeframe.H1, label=utc("2024-07-01T13:00")))

    assert "Candle: opened 2024-07-01 15:00 CEST\n" in text


def test_user_strings_are_html_escaped() -> None:
    text = render(
        notification(rule_name="<b>a & b</b>", values={"x<y": 1.5}),
    )

    assert "Rule: &lt;b&gt;a &amp; b&lt;/b&gt;\n" in text
    assert "x&lt;y: 1.50\n" in text
    assert "<b>a" not in text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (182.414, "182.41"),
        (1.0, "1.00"),
        (12345.678, "12,345.68"),
        (0.0, "0.00"),
        (0.123456, "0.1235"),
        (0.000123456, "0.0001235"),
        (-0.5, "-0.5000"),
        (-42.0, "-42.00"),
    ],
)
def test_prices_have_two_decimals_or_four_significant_digits(value: float, expected: str) -> None:
    assert format_price(value) == expected
