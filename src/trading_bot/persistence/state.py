"""The bot state: its closed vocabulary of keys and the records it is read into (spec 014, D118).

``bot_state`` holds one row per ``StateKey``, and every value is a UTC instant: when the pause
began, when the last heartbeat was seen and which scheduled close each timeframe last ran for.
A missing row means "never": not paused, no heartbeat, no run of that timeframe. The records
are frozen, hashable and free of ORM state, like every other record that leaves a session.

This module is light on purpose (decision D120): ``models.py`` and ``types.py`` need
``StateKey``, and they must load without the rule schema and the analysis stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from trading_bot.domain.timeframe import Timeframe

__all__ = ["BotState", "LastRun", "StateKey"]


class StateKey(StrEnum):
    """The keys ``bot_state`` may hold; the table's ``CHECK`` is built from this enumeration."""

    PAUSED_SINCE = "paused_since"
    LAST_HEARTBEAT = "last_heartbeat"
    LAST_RUN_1H = "last_run.1h"
    LAST_RUN_4H = "last_run.4h"
    LAST_RUN_1D = "last_run.1d"

    @classmethod
    def last_run(cls, timeframe: Timeframe) -> StateKey:
        """The key of the last run of ``timeframe``; every ``Timeframe`` member has one."""
        if not isinstance(timeframe, Timeframe):
            raise TypeError(f"timeframe must be a Timeframe, got {type(timeframe).__name__}")
        return cls(f"last_run.{timeframe.value}")


@dataclass(frozen=True, slots=True, kw_only=True)
class LastRun:
    """The last completed run of one timeframe."""

    timeframe: Timeframe
    scheduled_at: datetime  # the scheduled ``now`` the run evaluated (spec 010)
    completed_at: datetime  # when the run was recorded as complete, from the clock


@dataclass(frozen=True, slots=True, kw_only=True)
class BotState:
    """Every fact of ``bot_state`` at once; ``None`` and a missing run mean "never"."""

    paused_since: datetime | None
    last_heartbeat: datetime | None
    last_runs: tuple[LastRun, ...]  # only the timeframes that ran, in ``Timeframe`` order

    @property
    def paused(self) -> bool:
        """The global pause is the presence of ``paused_since``."""
        return self.paused_since is not None

    def last_run(self, timeframe: Timeframe) -> LastRun | None:
        """The last run of ``timeframe``, or ``None`` when it never ran."""
        for run in self.last_runs:
            if run.timeframe is timeframe:
                return run
        return None
