"""The bot-state repository over a ``Session`` (spec 014, Design 9; D118).

One row per ``StateKey``, each holding a UTC instant, and a missing row means "never". The
writers are independent — Telegram pauses, the scheduler reports runs, operations sends
heartbeats — so each key is read and written on its own, with its own ``updated_at``.

An upsert here is a ``session.get`` followed by an insert or an update, which is race-free
inside a session: the transaction holds the write lock from its first statement (D110), and the
primary key is the backstop for any writer that does not go through a session. Like the signal
repository, it never commits, rolls back or closes, and it loads no rule schema (D120).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc
from trading_bot.persistence.clock import Clock, system_clock
from trading_bot.persistence.models import BotStateRow
from trading_bot.persistence.state import BotState, LastRun, StateKey

__all__ = ["SqlBotStateRepository"]


class SqlBotStateRepository:
    """``BotStateRepository`` over one session; build one per unit of work."""

    def __init__(self, session: Session, *, clock: Clock = system_clock) -> None:
        self._session = session
        self._clock = clock

    def load(self) -> BotState:
        """Every stored fact in one read; ``last_runs`` follows ``Timeframe`` order."""
        rows = {row.key: row for row in self._session.execute(select(BotStateRow)).scalars().all()}
        paused = rows.get(StateKey.PAUSED_SINCE)
        heartbeat = rows.get(StateKey.LAST_HEARTBEAT)
        runs = []
        for timeframe in Timeframe:
            row = rows.get(StateKey.last_run(timeframe))
            if row is not None:
                runs.append(_to_run(timeframe, row))
        return BotState(
            paused_since=None if paused is None else to_utc(paused.value),
            last_heartbeat=None if heartbeat is None else to_utc(heartbeat.value),
            last_runs=tuple(runs),
        )

    def pause(self) -> datetime:
        """Pause the bot, keeping the instant of the first pause if one is already stored."""
        row = self._row(StateKey.PAUSED_SINCE)
        if row is not None:
            return to_utc(row.value)
        paused_at = to_utc(self._clock())
        self._store(StateKey.PAUSED_SINCE, paused_at, paused_at)
        return paused_at

    def resume(self) -> bool:
        row = self._row(StateKey.PAUSED_SINCE)
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def record_heartbeat(self) -> datetime:
        """A heartbeat is the latest sign of life, not a maximum: the last write wins."""
        beat_at = to_utc(self._clock())
        self._store(StateKey.LAST_HEARTBEAT, beat_at, beat_at)
        return beat_at

    def record_run(self, timeframe: Timeframe, scheduled_at: datetime) -> LastRun:
        """Record a completed run; an earlier or repeated report writes nothing (D118).

        Monotonicity makes a late or duplicated report from the scheduler harmless, which is
        what lets #15 call this after a retry or a catch-up without checking anything first.
        """
        key = StateKey.last_run(timeframe)
        scheduled = to_utc(scheduled_at)
        row = self._row(key)
        if row is not None and to_utc(row.value) >= scheduled:
            return _to_run(timeframe, row)
        completed = to_utc(self._clock())
        self._store(key, scheduled, completed)
        return LastRun(timeframe=timeframe, scheduled_at=scheduled, completed_at=completed)

    def _row(self, key: StateKey) -> BotStateRow | None:
        return self._session.get(BotStateRow, key)

    def _store(self, key: StateKey, value: datetime, updated_at: datetime) -> None:
        row = self._row(key)
        if row is None:
            self._session.add(BotStateRow(key=key, value=value, updated_at=updated_at))
        else:
            row.value = value
            row.updated_at = updated_at
        self._session.flush()


def _to_run(timeframe: Timeframe, row: BotStateRow) -> LastRun:
    return LastRun(
        timeframe=timeframe,
        scheduled_at=to_utc(row.value),
        completed_at=to_utc(row.updated_at),
    )
