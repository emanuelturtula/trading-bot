"""The SQLAlchemy ``UnitOfWork`` the application is wired with (spec 015, Design 3.1).

This is the **only** module of ``engine/`` allowed to name a ``Sql*`` repository, the
``Database`` handle or SQLAlchemy itself (AC21): everything else depends on the ports of
``unit_of_work.py``, so the orchestration stays testable with doubles and reusable by a later
dry run (#24). It builds no engine of its own — #16 opens the one ``Database`` and passes it
here — and gives the four repositories one clock (decision D92).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

from trading_bot.engine.unit_of_work import EngineRepositories, UnitOfWork
from trading_bot.persistence.clock import Clock, system_clock
from trading_bot.persistence.database import Database
from trading_bot.persistence.repositories.assignments import SqlAssignmentRepository
from trading_bot.persistence.repositories.bot_state import SqlBotStateRepository
from trading_bot.persistence.repositories.signals import SqlSignalRepository
from trading_bot.persistence.repositories.tickers import SqlTickerRepository

__all__ = ["sql_unit_of_work"]


def sql_unit_of_work(database: Database, *, clock: Clock = system_clock) -> UnitOfWork:
    """A ``UnitOfWork`` over ``database``: one ``Database.session()`` per block (#16 wires it).

    The block commits on a clean exit and rolls back on any ``BaseException``, exactly like
    ``Database.session()``. Keep it short, never hold it across an ``await`` and never open a
    second one in the same thread while one is live (spec 014, D110).
    """
    if not isinstance(database, Database):
        raise TypeError(f"database must be a Database, got {type(database).__name__}")

    def unit_of_work() -> AbstractContextManager[EngineRepositories]:
        return _repositories(database, clock)

    return unit_of_work


@contextmanager
def _repositories(database: Database, clock: Clock) -> Iterator[EngineRepositories]:
    with database.session() as session:
        yield EngineRepositories(
            tickers=SqlTickerRepository(session, clock=clock),
            assignments=SqlAssignmentRepository(session, clock=clock),
            signals=SqlSignalRepository(session, clock=clock),
            state=SqlBotStateRepository(session, clock=clock),
        )
