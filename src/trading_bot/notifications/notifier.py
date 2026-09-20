"""The notifier port and what one notification carries (spec 015, Design 7; decision D139).

Everything a message and a chart need comes from the run that decided the signal: the committed
row, the parsed rule and the closed candles the decision was made on. No second provider request
(which could return revised candles and draw a chart that contradicts the message) and no second
database read.

The frame is **shared, not copied**: copying it per signal would duplicate up to 2 255 rows for
every rule that fires, so a notifier must treat ``notification.candles`` as read-only and copy
before drawing. A notifier that mutates it is a notifier bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from trading_bot.domain.rules.schema import Rule
from trading_bot.persistence.signal_records import StoredSignal

__all__ = ["NotificationError", "Notifier", "SignalNotification"]


class NotificationError(Exception):
    """A notification could not be delivered. Implementations raise it after their own retries."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalNotification:
    """Everything a message needs, gathered by the run that decided the signal (D139)."""

    signal: StoredSignal  # the committed row: key, side, close price, values, rule name and id
    rule: Rule  # the parsed document, for the chart's indicators (#18)
    candles: pd.DataFrame  # the closed candles the decision was made on; read-only, never copied


class Notifier(Protocol):
    """Delivers signals to the user. It never places orders and never decides what to send."""

    async def notify(self, notification: SignalNotification) -> None:
        """Deliver one signal, retrying internally as the implementation sees fit.

        Raises ``NotificationError`` when delivery ultimately failed. The engine logs it, leaves
        the signal with ``notified_at`` ``NULL`` and never re-sends it (decision D132, spec 014
        D115). Implementations must treat ``notification.candles`` as read-only and must never
        put a token, a chat id or any provider text into the exceptions they raise: the engine
        logs the exception class only (decision D135).
        """
