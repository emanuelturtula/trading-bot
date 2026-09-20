"""A recording ``Notifier`` for engine tests (spec 015, Design 12; decision D140).

``FakeNotifier`` records every delivery, raises scripted failures first in first out and, on
each call, checks the two properties AC5 and AC18 are about:

- **no unit of work is open while a notification is sent**: an injected probe asks SQLite
  whether the write lock is free, the technique of spec 014 T3. The answer is recorded rather
  than raised, so a violation is reported by the test that asserts on it instead of being
  swallowed by the engine's per-signal error ring;
- **the frame is the one the engine evaluated, and it is read-only**: the object received is
  kept as is, next to a deep copy taken at the time of the call, so a notifier that mutates it
  is visible afterwards.

There is no network and no clock here. Always import this module as ``tests.fixtures.notifiers``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from trading_bot.domain.signals import SignalKey
from trading_bot.domain.utc import to_utc
from trading_bot.notifications.notifier import Notifier, SignalNotification

__all__ = ["FakeNotifier", "NotifyCall", "as_notifier"]


@dataclass(frozen=True, slots=True, kw_only=True)
class NotifyCall:
    """One recorded delivery, reduced to the values a test asserts on."""

    key: SignalKey
    signal_id: int
    rule_name: str
    last_label: datetime  # ``candles.index[-1]``, to prove which frame was passed


class FakeNotifier:
    """Records deliveries and raises scripted failures, first in first out. No network, no clock."""

    def __init__(self, *, lock_probe: Callable[[], bool] | None = None) -> None:
        self._calls: list[NotifyCall] = []
        self._frames: list[pd.DataFrame] = []
        self._snapshots: list[pd.DataFrame] = []
        self._notifications: list[SignalNotification] = []
        self._lock_free: list[bool] = []
        self._failures: deque[Exception] = deque()
        self._lock_probe = lock_probe

    @property
    def calls(self) -> tuple[NotifyCall, ...]:
        """Every delivery attempt, in order, including the ones that raised."""
        return tuple(self._calls)

    @property
    def notifications(self) -> tuple[SignalNotification, ...]:
        """The notifications as received, for assertions on the whole value."""
        return tuple(self._notifications)

    @property
    def frames(self) -> tuple[pd.DataFrame, ...]:
        """The candle frames as received: the very objects, never copies."""
        return tuple(self._frames)

    @property
    def lock_free(self) -> tuple[bool, ...]:
        """Whether the database write lock was free at each call (``()`` without a probe)."""
        return tuple(self._lock_free)

    @property
    def mutated_frames(self) -> tuple[int, ...]:
        """The indexes of the calls whose frame differs from the copy taken when it arrived."""
        return tuple(
            index
            for index, (frame, snapshot) in enumerate(
                zip(self._frames, self._snapshots, strict=True)
            )
            if not frame.equals(snapshot)
        )

    def fail_next(self, error: Exception, *, times: int = 1) -> None:
        """Raise ``error`` on the next ``times`` calls (first in first out)."""
        if not isinstance(error, Exception):
            raise TypeError(f"error must be an Exception, got {type(error).__name__}")
        if isinstance(times, bool) or not isinstance(times, int):
            raise TypeError(f"times must be an int, got {type(times).__name__}")
        if times < 1:
            raise ValueError(f"times must be at least 1, got {times}")
        self._failures.extend([error] * times)

    async def notify(self, notification: SignalNotification) -> None:
        if not isinstance(notification, SignalNotification):
            raise TypeError(
                f"notification must be a SignalNotification, got {type(notification).__name__}"
            )
        candles = notification.candles
        self._notifications.append(notification)
        self._frames.append(candles)
        self._snapshots.append(candles.copy(deep=True))
        if self._lock_probe is not None:
            self._lock_free.append(self._lock_probe())
        stored = notification.signal
        self._calls.append(
            NotifyCall(
                key=stored.key,
                signal_id=stored.id,
                rule_name=stored.rule_name,
                last_label=to_utc(pd.DatetimeIndex(candles.index)[-1]),
            )
        )
        if self._failures:
            # A fresh traceback per raise: an instance scripted several times does not
            # accumulate the frames of earlier raises.
            raise self._failures.popleft().with_traceback(None)


def as_notifier(notifier: FakeNotifier) -> Notifier:
    """Returns its argument; strict mypy checks that the fake conforms to the port (AC18)."""
    return notifier
