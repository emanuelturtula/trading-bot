"""The repositories one Telegram command uses, in one session (spec 018, D4).

Like the engine's unit of work (spec 015, D124), a command sees ports only and runs its whole
block in a worker thread through ``in_command_unit_of_work``, so a session is never held across
an ``await`` (spec 014, D110).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

from trading_bot.persistence.clock import Clock, system_clock
from trading_bot.persistence.database import Database
from trading_bot.persistence.owner import Owner
from trading_bot.persistence.repositories.assignments import SqlAssignmentRepository
from trading_bot.persistence.repositories.bot_state import SqlBotStateRepository
from trading_bot.persistence.repositories.protocols import (
    AssignmentRepository,
    BotStateRepository,
    RuleRepository,
    TickerRepository,
    UserRepository,
)
from trading_bot.persistence.repositories.rules import SqlRuleRepository
from trading_bot.persistence.repositories.tickers import SqlTickerRepository
from trading_bot.persistence.repositories.users import SqlUserRepository

__all__ = [
    "CommandRepositories",
    "CommandUnitOfWork",
    "in_command_unit_of_work",
    "sql_command_unit_of_work",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandRepositories:
    tickers: TickerRepository
    rules: RuleRepository
    assignments: AssignmentRepository
    state: BotStateRepository
    users: UserRepository


type CommandUnitOfWork = Callable[[], AbstractContextManager[CommandRepositories]]


async def in_command_unit_of_work[T](
    unit_of_work: CommandUnitOfWork, work: Callable[[CommandRepositories], T]
) -> T:
    """Run ``work`` in one unit of work, in a worker thread of its own."""

    def run() -> T:
        with unit_of_work() as repositories:
            return work(repositories)

    return await asyncio.to_thread(run)


def sql_command_unit_of_work(
    database: Database, *, owner: Owner = None, clock: Clock = system_clock
) -> CommandUnitOfWork:
    """One ``Database.session()`` per block; commits on a clean exit, rolls back otherwise.

    ``owner`` is the user whose bot received the command: the repositories see only their
    tickers and rules (spec 025, D5).
    """
    if not isinstance(database, Database):
        raise TypeError(f"database must be a Database, got {type(database).__name__}")

    @contextmanager
    def unit_of_work() -> Iterator[CommandRepositories]:
        with database.session() as session:
            yield CommandRepositories(
                tickers=SqlTickerRepository(session, clock=clock, owner=owner),
                rules=SqlRuleRepository(session, clock=clock, owner=owner),
                assignments=SqlAssignmentRepository(session, clock=clock, owner=owner),
                state=SqlBotStateRepository(session, clock=clock),
                users=SqlUserRepository(session, clock=clock),
            )

    return unit_of_work
