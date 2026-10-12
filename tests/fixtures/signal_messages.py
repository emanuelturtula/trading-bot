"""A ``SignalNotification`` builder for the message and notifier tests (spec 017).

Always import this module as ``tests.fixtures.signal_messages``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from zoneinfo import ZoneInfo

import pandas as pd

from tests.fixtures.calendars import utc
from tests.fixtures.engine import triggering_rule
from trading_bot.domain.signals import Side, Signal
from trading_bot.domain.timeframe import Timeframe
from trading_bot.notifications.notifier import SignalNotification
from trading_bot.persistence.signal_records import StoredSignal

__all__ = ["DAILY_LABEL", "MADRID", "notification"]

DAILY_LABEL: Final = utc("2024-07-01T04:00")  # the 1d label of the Monday 2024-07-01 session
MADRID: Final = ZoneInfo("Europe/Madrid")


def notification(
    *,
    ticker: str = "AAPL",
    timeframe: Timeframe = Timeframe.D1,
    label: datetime = DAILY_LABEL,
    side: Side = Side.BUY,
    rule_name: str = "RSI bounce",
    close_price: float = 182.41,
    values: dict[str, float] | None = None,
) -> SignalNotification:
    signal = Signal(
        ticker=ticker,
        timeframe=timeframe,
        rule_id="7",
        side=side,
        candle_close_ts=timeframe.nominal_close(label),
        close_price=close_price,
        indicator_values={"rsi_14": 28.734, "sma_50": 190.1234} if values is None else values,
    )
    stored = StoredSignal(
        id=1,
        ticker_id=1,
        rule_id=7,
        rule_name=rule_name,
        signal=signal,
        created_at=datetime(2024, 7, 1, 20, 2, tzinfo=UTC),
        notified_at=None,
    )
    return SignalNotification(
        signal=stored, rule=triggering_rule("x"), candles=pd.DataFrame(), owner_id=1
    )
