"""The numbers the schedule is made of, validated once (spec 016, Design 6).

``SchedulerPolicy`` is what ``trigger.py``, ``runner.py`` and ``service.py`` read instead of the
settings: the package never imports ``trading_bot.config`` (decision D153), so it stays
importable and testable without an environment, and #16 builds the policy from
``TB_CANDLE_CLOSE_DELAY_SECONDS`` and ``TB_SCHEDULER_MISFIRE_GRACE_SECONDS`` at the edge.

Two of the bounds are not taste:

- ``close_delay`` is capped at fifteen minutes because two consecutive ``1h`` closes can be
  **thirty** minutes apart (the final slot of a session is truncated, 15:30-16:00 ET), so a
  larger delay could push a fire past the next close;
- ``misfire_grace`` has a floor of one minute because a fire always arrives some milliseconds
  after its scheduled instant, and a zero grace would reject every one of them (decision D146).

``retry_backoff`` and ``retry_window`` are literals rather than settings (user decision U3): the
operator's surface stays at two variables, and making them configurable later changes no
signature.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

__all__ = [
    "DEFAULT_POLICY",
    "DEFAULT_RETRY_BACKOFF",
    "MAX_CLOSE_DELAY",
    "MAX_MISFIRE_GRACE",
    "MIN_MISFIRE_GRACE",
    "SchedulerPolicy",
]

MAX_CLOSE_DELAY: Final = timedelta(minutes=15)
MIN_MISFIRE_GRACE: Final = timedelta(minutes=1)
MAX_MISFIRE_GRACE: Final = timedelta(hours=1)
DEFAULT_RETRY_BACKOFF: Final = (
    timedelta(seconds=30),
    timedelta(seconds=60),
    timedelta(seconds=120),
    timedelta(seconds=240),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SchedulerPolicy:
    """When a run fires, how late it may still run and how long unpublished candles are retried."""

    close_delay: timedelta = timedelta(seconds=120)
    misfire_grace: timedelta = timedelta(minutes=15)
    retry_backoff: tuple[timedelta, ...] = DEFAULT_RETRY_BACKOFF
    retry_window: timedelta = timedelta(minutes=10)
    shutdown_timeout: timedelta = timedelta(seconds=10)

    def __post_init__(self) -> None:
        for name in ("close_delay", "misfire_grace", "retry_window", "shutdown_timeout"):
            _whole_seconds(getattr(self, name), name)
        if not timedelta(0) <= self.close_delay <= MAX_CLOSE_DELAY:
            raise ValueError(
                f"close_delay must be between 0 and {int(MAX_CLOSE_DELAY.total_seconds())} "
                f"seconds, got {self.close_delay}"
            )
        if not MIN_MISFIRE_GRACE <= self.misfire_grace <= MAX_MISFIRE_GRACE:
            raise ValueError(
                f"misfire_grace must be between {int(MIN_MISFIRE_GRACE.total_seconds())} and "
                f"{int(MAX_MISFIRE_GRACE.total_seconds())} seconds, got {self.misfire_grace}"
            )
        _check_backoff(self.retry_backoff)
        if self.retry_window < timedelta(0):
            raise ValueError(f"retry_window must not be negative, got {self.retry_window}")
        if self.shutdown_timeout <= timedelta(0):
            raise ValueError(f"shutdown_timeout must be positive, got {self.shutdown_timeout}")


def _check_backoff(value: object) -> None:
    """A tuple of strictly positive, non-decreasing whole-second waits; empty disables retries."""
    if not isinstance(value, tuple):
        raise TypeError(f"retry_backoff must be a tuple, got {type(value).__name__}")
    previous = timedelta(0)
    for position, wait in enumerate(value):
        _whole_seconds(wait, f"retry_backoff[{position}]", type_name="retry_backoff")
        if wait <= timedelta(0):
            raise ValueError(f"retry_backoff waits must be positive, got {wait} at {position}")
        if wait < previous:
            raise ValueError(
                f"retry_backoff must not decrease, got {wait} after {previous} at {position}"
            )
        previous = wait


def _whole_seconds(value: object, name: str, *, type_name: str | None = None) -> None:
    """``value`` must be a ``timedelta`` of whole seconds, so fire times and logs stay exact."""
    if isinstance(value, bool) or not isinstance(value, timedelta):
        raise TypeError(f"{type_name or name} must be a timedelta, got {type(value).__name__}")
    if value.microseconds:
        raise ValueError(f"{name} must be a whole number of seconds, got {value}")


DEFAULT_POLICY: Final = SchedulerPolicy()
