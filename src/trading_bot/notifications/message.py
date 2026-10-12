"""The text of a signal message (spec 017, §3; AC8).

Pure: the same notification, environment and zone always give the same text. Every string that
comes from user data (ticker, rule name, indicator names) is HTML-escaped, because the message is
sent with HTML parse mode.

The candle is presented by its session date (``1d``), the date of its first session (``1w``,
spec 028) or its real close in the display zone (``1h``, ``4h``), never by the nominal close
(spec 004 §5): for ``1d`` that one is midnight of the next day. The real close comes from the
calendar slot whose label is the candle's open.
"""

from __future__ import annotations

import html
import math
from datetime import datetime, tzinfo
from typing import Final

from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.domain.signals import Side, Signal
from trading_bot.domain.timeframe import Timeframe
from trading_bot.notifications.notifier import SignalNotification

__all__ = ["BETA_PREFIX", "DISCLAIMER", "format_price", "format_signal_message"]

BETA_PREFIX: Final = "[BETA]"
DISCLAIMER: Final = "Not financial advice."


def format_signal_message(
    notification: SignalNotification,
    *,
    production: bool,
    display_tz: tzinfo,
    calendar: MarketCalendar,
) -> str:
    """The HTML text of one signal. ``production`` drops the ``[BETA]`` prefix."""
    stored = notification.signal
    signal = stored.signal
    side = signal.side
    header = f"{_side_mark(side)} {side.value} · {_escape(signal.ticker)} · {signal.timeframe}"
    if not production:
        header = f"{BETA_PREFIX} {header}"
    lines = [
        f"<b>{header}</b>",
        f"Rule: {_escape(stored.rule_name)}",
        f"Close: {format_price(signal.close_price)}",
        f"Candle: {_candle_text(signal, display_tz, calendar)}",
    ]
    lines.extend(
        f"{_escape(name)}: {format_price(value)}" for name, value in signal.indicator_values.items()
    )
    lines.extend(["", f"<i>{DISCLAIMER}</i>"])
    return "\n".join(lines)


def format_price(value: float) -> str:
    """Two decimals at or above 1, otherwise four significant digits (decision D9)."""
    if not math.isfinite(value):
        return str(value)
    if abs(value) >= 1 or value == 0:
        return f"{value:,.2f}"
    digits = 3 - math.floor(math.log10(abs(value)))
    return f"{value:.{digits}f}"


def _side_mark(side: Side) -> str:
    return "🟢" if side is Side.BUY else "🔴"


def _candle_text(signal: Signal, display_tz: tzinfo, calendar: MarketCalendar) -> str:
    timeframe = signal.timeframe
    label = signal.candle_close_ts - timeframe.duration  # the open, which labels the candle
    try:
        slot = calendar.candle_slot(timeframe, label)
    except Exception:  # an off-grid label: present the open instead of guessing a close
        return f"opened {_local(label, display_tz)}"
    if timeframe is Timeframe.D1:
        return f"{slot.session_day.isoformat()} (session)"
    if timeframe is Timeframe.W1:
        return f"week of {slot.session_day.isoformat()}"
    return f"closed {_local(slot.close_time, display_tz)}"


def _local(instant: datetime, display_tz: tzinfo) -> str:
    return instant.astimezone(display_tz).strftime("%Y-%m-%d %H:%M %Z")


def _escape(text: str) -> str:
    return html.escape(text, quote=False)
