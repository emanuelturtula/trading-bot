"""The transactional port the engine writes through (spec 015, Design 3; decision D124).

The engine depends on the repository ``Protocol``s only, never on an implementation: a
``UnitOfWork`` is a factory that yields the four repositories of one ``Database.session()``,
commits on a clean exit and rolls back on any ``BaseException``. ``sql_unit_of_work`` builds the
SQLAlchemy one; tests pass doubles for the paths a real database cannot produce on demand, such
as a ticker deleted between two units of work.

Four ports, not the five of the persistence layer: the engine reads rules through
``AssignmentRepository.rules_for_ticker`` and never addresses one by name, so ``RuleRepository``
would be a dependency nothing uses. A later consumer that needs it adds it here.

One block is one unit of work. The engine never nests two of them and never holds one across an
``await`` (spec 014, D110): ``SignalEngine._in_unit_of_work`` makes both structural.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from trading_bot.persistence.repositories.protocols import (
    AssignmentRepository,
    BotStateRepository,
    SignalRepository,
    TickerRepository,
)

__all__ = ["EngineRepositories", "UnitOfWork"]


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineRepositories:
    """The ports of one unit of work, as the engine sees them."""

    tickers: TickerRepository
    assignments: AssignmentRepository
    signals: SignalRepository
    state: BotStateRepository


type UnitOfWork = Callable[[], AbstractContextManager[EngineRepositories]]
