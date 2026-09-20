"""The transactional port the engine writes through (spec 015, Design 3; decision D124).

The engine depends on the repository ``Protocol``s only, never on an implementation: a
``UnitOfWork`` is a factory that yields the four repositories of one ``Database.session()``,
commits on a clean exit and rolls back on any ``BaseException``. ``sql_unit_of_work`` builds the
SQLAlchemy one; tests pass doubles for the paths a real database cannot produce on demand, such
as a ticker deleted between two units of work.

Four ports, not the five of the persistence layer: the engine reads rules through
``AssignmentRepository.rules_for_ticker`` and never addresses one by name, so ``RuleRepository``
would be a dependency nothing uses. A later consumer that needs it adds it here.

One block is one unit of work. No consumer nests two of them and none holds one across an
``await`` (spec 014, D110): ``in_unit_of_work`` makes both structural, and it lives here rather
than in one consumer because that discipline must have exactly one implementation (spec 016,
decision D158). ``SignalEngine`` and ``scheduler/state.py`` are the two callers today.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from trading_bot.persistence.repositories.protocols import (
    AssignmentRepository,
    BotStateRepository,
    SignalRepository,
    TickerRepository,
)

__all__ = ["EngineRepositories", "UnitOfWork", "in_unit_of_work"]


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineRepositories:
    """The ports of one unit of work, as the engine sees them."""

    tickers: TickerRepository
    assignments: AssignmentRepository
    signals: SignalRepository
    state: BotStateRepository


type UnitOfWork = Callable[[], AbstractContextManager[EngineRepositories]]


async def in_unit_of_work[T](
    unit_of_work: UnitOfWork, work: Callable[[EngineRepositories], T]
) -> T:
    """Run ``work`` in one unit of work, in a worker thread of its own (decision D138).

    ``work`` is a plain function of the repositories: it cannot await, cannot reach the provider
    or the notifier and cannot open a second unit of work, so "never two sessions at once in one
    thread" and "never a session across an ``await``" hold by construction (spec 014, D110).

    The synchronous session is confined to that worker thread, which is what lets a caller
    running on the event loop use SQLAlchemy at all (decision D69).
    """

    def run() -> T:
        with unit_of_work() as repositories:
            return work(repositories)

    return await asyncio.to_thread(run)
