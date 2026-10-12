"""``LogNotifier``: the notifier used while Telegram is not configured (spec 017, U2).

The app starts without a bot token or without chat ids, so beta stays healthy before the bot
exists; every signal is still recorded by the engine and is logged here as one ``INFO`` line.
A signal "delivered" this way is marked notified, exactly like a Telegram delivery: delivery is
at-most-once (spec 015, U4), so configuring the bot later never re-sends it.
"""

from __future__ import annotations

import logging
from typing import Final

from trading_bot.notifications.notifier import SignalNotification

__all__ = ["LogNotifier"]

_LOGGER_NAME: Final = "trading_bot.notifications.log"


class LogNotifier:
    """Logs each signal instead of sending it. No network, no clock."""

    def __init__(self) -> None:
        self._logger = logging.getLogger(_LOGGER_NAME)

    async def notify(self, notification: SignalNotification) -> None:
        signal = notification.signal.signal
        self._logger.info(
            "signal (telegram not configured): %s %s %s rule %r close %s at %s",
            signal.side.value,
            signal.ticker,
            signal.timeframe.value,
            notification.signal.rule_name,
            signal.close_price,
            signal.candle_close_ts.isoformat(),
        )

    async def aclose(self) -> None:
        return None
