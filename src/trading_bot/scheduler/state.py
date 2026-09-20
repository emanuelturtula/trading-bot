"""What the scheduler reads and writes in ``bot_state`` (spec 016, Design 9; decision D144).

Two short units of work per fire: one before the run, to answer "has this close already been
run?", and one after it, to record that it has. They are what makes "exactly once per close"
survive a restart, a coalesced burst of fires and a clock stepped backwards by NTP —
``max_instances=1`` says nothing about any of those.

Both go through ``in_unit_of_work`` (decision D158), so the session lives in a worker thread of
its own, is never held across an ``await`` and is never open while the engine runs (spec 014,
D110). Only ``BotStateRepository`` is touched, and ``completed_at`` comes from the repositories'
injected clock (spec 013, D92), never from the scheduler's.
"""

from __future__ import annotations

from datetime import datetime

from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine.unit_of_work import EngineRepositories, UnitOfWork, in_unit_of_work
from trading_bot.persistence.state import LastRun

__all__ = ["read_last_run", "record_run"]


async def read_last_run(unit_of_work: UnitOfWork, timeframe: Timeframe) -> LastRun | None:
    """The last completed run of ``timeframe``, in one unit of work (decision D144).

    ``None`` means that timeframe has never completed a run, which is what a fresh database and
    a brand-new timeframe both look like.
    """

    def read(repositories: EngineRepositories) -> LastRun | None:
        return repositories.state.load().last_run(timeframe)

    return await in_unit_of_work(unit_of_work, read)


async def record_run(
    unit_of_work: UnitOfWork, timeframe: Timeframe, scheduled_close: datetime
) -> LastRun:
    """Record a completed run; ``record_run`` is monotonic, so a repeat writes nothing.

    It is called for every completed cycle, a paused one and one with no enabled ticker
    included (user decision U4): a paused bot must not look dead to the missing-run alert F7
    will build on this row.
    """

    def write(repositories: EngineRepositories) -> LastRun:
        return repositories.state.record_run(timeframe, scheduled_close)

    return await in_unit_of_work(unit_of_work, write)
