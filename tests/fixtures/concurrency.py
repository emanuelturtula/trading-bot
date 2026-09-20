"""The ordered two-thread race that proves ``record`` is safe under concurrency (spec 014, D123).

A barrier would pass vacuously whenever the threads happened to run one after the other, so the
overlap is **forced**: A records and keeps its transaction open; B starts its own unit of work;
a ``before_cursor_execute`` listener sees B's ``BEGIN IMMEDIATE`` being dispatched, and only
then does A commit. B therefore either waits for A's commit or, when the commit lands between
the dispatch and the lock request, finds the row already there; both interleavings must give
``is_new=False``, which is the property under test.

Nothing here sleeps and nothing measures elapsed time: every wait is an event with a 60 s guard
that turns a deadlock into a failure. An ``assert`` inside a thread would not fail the test, so
each thread stores its outcome or its exception and the caller re-raises; both events are set on
the failure path too, so a crash in one thread never leaves the other waiting out the guard.

It opens no database of its own: the caller passes the handles it got from
``tests/fixtures/database.py`` (or from ``connect_database`` on the same directory).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from sqlalchemy import event

from trading_bot.domain.signals import Signal
from trading_bot.persistence.clock import Clock
from trading_bot.persistence.database import Database
from trading_bot.persistence.repositories.signals import SqlSignalRepository
from trading_bot.persistence.signal_records import RecordOutcome

__all__ = ["GUARD_SECONDS", "Race", "ordered_race"]

# Turns a deadlock into a failure. It is never compared with an elapsed time: the production
# busy timeout is 5 s, three orders of magnitude above a commit, so a slow runner cannot turn
# the expected wait into a failure.
GUARD_SECONDS = 60

_BEGIN_IMMEDIATE = "BEGIN IMMEDIATE"


@dataclass(frozen=True, slots=True)
class Race:
    """What the two units of work produced.

    ``second`` is ``None`` exactly when the second unit of work raised, which is what the
    control variant (a zero busy timeout) expects and the main variants must never see.
    """

    first: RecordOutcome
    second: RecordOutcome | None
    second_error: BaseException | None


def ordered_race(
    first: Database,
    second: Database,
    signal: Signal,
    *,
    clock_first: Clock,
    clock_second: Clock,
    ticker_id: int,
    rule_id: int,
    read_first: bool = False,
    hold_until_second_finishes: bool = False,
) -> Race:
    """Record ``signal`` twice at once and return both outcomes.

    ``read_first`` makes the second unit of work read ``latest`` before recording, which is the
    cooldown read of #14 and the shape a deferred ``BEGIN`` could not survive.
    ``hold_until_second_finishes`` keeps the first unit of work open until the second one is
    done, which the control variant needs: with a zero busy timeout the second unit of work
    must be refused, and that only proves the collision if the lock was still held.
    """
    first_recorded = threading.Event()
    second_began = threading.Event()
    second_finished = threading.Event()
    second_ident: list[int] = []
    outcomes: dict[str, RecordOutcome] = {}
    failures: dict[str, BaseException] = {}

    def spy(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        # Only the second thread's own BEGIN counts: on one handle the first thread sends the
        # very same statement on the very same engine.
        if (
            statement == _BEGIN_IMMEDIATE
            and second_ident
            and threading.get_ident() == second_ident[0]
        ):
            second_began.set()

    def run_first() -> None:
        try:
            with first.session() as session:
                outcomes["first"] = SqlSignalRepository(session, clock=clock_first).record(signal)
                first_recorded.set()
                _wait(
                    second_finished if hold_until_second_finishes else second_began,
                    "the second unit of work never started",
                )
            # Leaving the block committed the first unit of work.
        except BaseException as error:  # re-raised by the caller, in its own thread
            failures["first"] = error
        finally:
            first_recorded.set()  # never leave the second thread waiting out the guard

    def run_second() -> None:
        second_ident.append(threading.get_ident())
        try:
            _wait(first_recorded, "the first unit of work never recorded")
            with second.session() as session:
                repository = SqlSignalRepository(session, clock=clock_second)
                if read_first:
                    repository.latest(ticker_id, rule_id)  # the cooldown read of #14
                outcomes["second"] = repository.record(signal)
        except BaseException as error:  # reported through the result, never swallowed
            failures["second"] = error
        finally:
            second_began.set()
            second_finished.set()

    # Events reach the engine the session is bound to through its parent, so the listener goes
    # on the handle's own engine (measured).
    event.listen(second.engine, "before_cursor_execute", spy)
    threads = (
        threading.Thread(target=run_second, name="race-second"),
        threading.Thread(target=run_first, name="race-first"),
    )
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(GUARD_SECONDS)
            if thread.is_alive():
                raise TimeoutError(f"{thread.name} did not finish within {GUARD_SECONDS} s")
    finally:
        event.remove(second.engine, "before_cursor_execute", spy)

    first_failure = failures.get("first")
    if first_failure is not None:
        raise first_failure
    first_outcome = outcomes.get("first")
    if first_outcome is None:
        raise RuntimeError("the first unit of work produced no outcome")
    return Race(
        first=first_outcome,
        second=outcomes.get("second"),
        second_error=failures.get("second"),
    )


def _wait(flag: threading.Event, message: str) -> None:
    if not flag.wait(GUARD_SECONDS):
        raise TimeoutError(message)
