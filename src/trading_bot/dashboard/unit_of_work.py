"""The repositories one dashboard request uses, in one session (spec 020, D7).

Same pattern as the Telegram commands (spec 018, D4): the whole block runs in a worker thread
through ``in_dashboard_unit_of_work``, so a session is never held across an ``await``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from trading_bot.persistence.clock import Clock, system_clock
from trading_bot.persistence.database import Database
from trading_bot.persistence.owner import Owner
from trading_bot.persistence.repositories.assignments import SqlAssignmentRepository
from trading_bot.persistence.repositories.bot_state import SqlBotStateRepository
from trading_bot.persistence.repositories.protocols import (
    AssignmentRepository,
    BotStateRepository,
    RuleRepository,
    SignalRepository,
    TickerRepository,
    UserRepository,
)
from trading_bot.persistence.repositories.rules import SqlRuleRepository
from trading_bot.persistence.repositories.signals import SqlSignalRepository
from trading_bot.persistence.repositories.tickers import SqlTickerRepository
from trading_bot.persistence.repositories.users import SqlUserRepository

__all__ = [
    "DashboardRepositories",
    "DashboardUnitOfWork",
    "in_dashboard_unit_of_work",
    "sql_dashboard_unit_of_work",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardRepositories:
    tickers: TickerRepository
    rules: RuleRepository
    assignments: AssignmentRepository
    state: BotStateRepository
    signals: SignalRepository
    users: UserRepository


class DashboardUnitOfWork(Protocol):
    """Opens one unit of work over ``owner``'s configuration (spec 024): the signed-in user."""

    def __call__(self, owner: Owner = None) -> AbstractContextManager[DashboardRepositories]: ...


async def in_dashboard_unit_of_work[T](
    unit_of_work: DashboardUnitOfWork,
    work: Callable[[DashboardRepositories], T],
    *,
    owner: Owner = None,
) -> T:
    """Run ``work`` in one unit of work over ``owner``'s rows, in a worker thread of its own."""

    def run() -> T:
        with unit_of_work(owner) as repositories:
            return work(repositories)

    return await asyncio.to_thread(run)


def sql_dashboard_unit_of_work(
    database: Database, *, clock: Clock = system_clock
) -> DashboardUnitOfWork:
    """One ``Database.session()`` per block; commits on a clean exit, rolls back otherwise."""
    if not isinstance(database, Database):
        raise TypeError(f"database must be a Database, got {type(database).__name__}")

    @contextmanager
    def unit_of_work(owner: Owner = None) -> Iterator[DashboardRepositories]:
        with database.session() as session:
            yield DashboardRepositories(
                tickers=SqlTickerRepository(session, clock=clock, owner=owner),
                rules=SqlRuleRepository(session, clock=clock, owner=owner),
                assignments=SqlAssignmentRepository(session, clock=clock, owner=owner),
                state=SqlBotStateRepository(session, clock=clock),
                signals=SqlSignalRepository(session, clock=clock),
                users=SqlUserRepository(session, clock=clock),
            )

    return unit_of_work
