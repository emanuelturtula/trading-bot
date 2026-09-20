"""The bot state: vocabulary, pause, heartbeat and runs (spec 014, T10, AC19-AC22).

Every instant is a literal and comes from an injected clock. A missing row means "never", so no
test seeds the table to express "nothing happened yet".
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.fixtures.repositories import frozen_clock, repositories
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.clock import Clock
from trading_bot.persistence.database import Database
from trading_bot.persistence.repositories.bot_state import SqlBotStateRepository
from trading_bot.persistence.state import BotState, LastRun, StateKey

PAUSED_AT = datetime(2024, 7, 5, 21, 0, tzinfo=UTC)
HEARTBEAT_AT = datetime(2024, 7, 5, 20, 30, tzinfo=UTC)
SCHEDULED_AT = datetime(2024, 7, 5, 20, 0, 30, tzinfo=UTC)
COMPLETED_AT = datetime(2024, 7, 5, 20, 1, 2, tzinfo=UTC)


def state(session: Session, clock: Clock) -> SqlBotStateRepository:
    return SqlBotStateRepository(session, clock=clock)


def stored_rows(database: Database) -> list[tuple[str, str, str]]:
    with database.engine.connect() as connection:
        rows = connection.exec_driver_sql(
            'SELECT "key", value, updated_at FROM bot_state ORDER BY "key"'
        ).all()
    return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]


# --- AC19: the closed vocabulary -----------------------------------------------------------


def test_the_vocabulary_is_exactly_the_five_keys() -> None:
    assert [member.value for member in StateKey] == [
        "paused_since",
        "last_heartbeat",
        "last_run.1h",
        "last_run.4h",
        "last_run.1d",
    ]


@pytest.mark.parametrize(
    ("timeframe", "key"),
    [
        (Timeframe.H1, "last_run.1h"),
        (Timeframe.H4, "last_run.4h"),
        (Timeframe.D1, "last_run.1d"),
    ],
)
def test_every_timeframe_has_a_last_run_key(timeframe: Timeframe, key: str) -> None:
    assert StateKey.last_run(timeframe).value == key


def test_the_last_run_keys_cover_every_timeframe() -> None:
    """Adding a timeframe without its key fails here, and the golden DDL fails too."""
    keys = {StateKey.last_run(timeframe) for timeframe in Timeframe}

    assert len(keys) == len(Timeframe)
    assert keys <= set(StateKey)


def test_a_timeframe_key_needs_a_timeframe() -> None:
    with pytest.raises(TypeError, match="Timeframe"):
        StateKey.last_run("1d")  # type: ignore[arg-type]


def test_a_key_outside_the_vocabulary_is_refused_by_the_database(database: Database) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_bot_state_key"),
        database.engine.begin() as connection,
    ):
        connection.exec_driver_sql(
            'INSERT INTO bot_state ("key", value, updated_at)'
            " VALUES ('last_run.1w', '2024-07-05 20:00:00.000000', '2024-07-05 20:00:00.000000')"
        )


# --- AC20: a missing row means "never" -----------------------------------------------------


def test_an_empty_table_reads_as_never(database: Database) -> None:
    with database.session() as session:
        loaded = state(session, frozen_clock(PAUSED_AT)).load()

    assert loaded == BotState(paused_since=None, last_heartbeat=None, last_runs=())
    assert loaded.paused is False
    for timeframe in Timeframe:
        assert loaded.last_run(timeframe) is None
    assert stored_rows(database) == []


# --- AC21: the pause -----------------------------------------------------------------------


def test_pause_stores_and_returns_the_clock_instant(database: Database) -> None:
    with database.session() as session:
        paused_at = state(session, frozen_clock(PAUSED_AT)).pause()

    assert paused_at == PAUSED_AT
    assert stored_rows(database) == [
        ("paused_since", "2024-07-05 21:00:00.000000", "2024-07-05 21:00:00.000000")
    ]
    with database.session() as session:
        loaded = state(session, frozen_clock(PAUSED_AT)).load()
    assert loaded.paused is True
    assert loaded.paused_since == PAUSED_AT


def test_pausing_twice_keeps_the_first_instant_and_writes_nothing(database: Database) -> None:
    with database.session() as session:
        state(session, frozen_clock(PAUSED_AT)).pause()
    before = stored_rows(database)

    with database.session() as session:
        again = state(session, frozen_clock(PAUSED_AT + timedelta(hours=3))).pause()

    assert again == PAUSED_AT
    assert stored_rows(database) == before


def test_resume_removes_the_pause_and_reports_whether_there_was_one(database: Database) -> None:
    with database.session() as session:
        state(session, frozen_clock(PAUSED_AT)).pause()

    with database.session() as session:
        removed = state(session, frozen_clock(PAUSED_AT)).resume()
    with database.session() as session:
        again = state(session, frozen_clock(PAUSED_AT)).resume()

    assert (removed, again) == (True, False)
    assert stored_rows(database) == []
    with database.session() as session:
        assert state(session, frozen_clock(PAUSED_AT)).load().paused is False


def test_pause_after_a_resume_takes_the_new_instant(database: Database) -> None:
    later = PAUSED_AT + timedelta(days=1)
    with database.session() as session:
        repository = state(session, frozen_clock(PAUSED_AT))
        repository.pause()
        repository.resume()

    with database.session() as session:
        assert state(session, frozen_clock(later)).pause() == later


# --- AC22: heartbeat and runs --------------------------------------------------------------


def test_the_heartbeat_is_the_latest_sign_of_life(database: Database) -> None:
    backwards = HEARTBEAT_AT - timedelta(minutes=10)

    with database.session() as session:
        first = state(session, frozen_clock(HEARTBEAT_AT)).record_heartbeat()
    with database.session() as session:
        second = state(session, frozen_clock(backwards)).record_heartbeat()
    with database.session() as session:
        loaded = state(session, frozen_clock(HEARTBEAT_AT)).load()

    assert first == HEARTBEAT_AT
    assert second == backwards  # last write wins, even when the clock moved backwards
    assert loaded.last_heartbeat == backwards
    assert stored_rows(database) == [
        ("last_heartbeat", "2024-07-05 20:20:00.000000", "2024-07-05 20:20:00.000000")
    ]


def test_a_run_stores_the_scheduled_close_and_the_completion_instant(database: Database) -> None:
    with database.session() as session:
        run = state(session, frozen_clock(COMPLETED_AT)).record_run(Timeframe.D1, SCHEDULED_AT)

    assert run == LastRun(
        timeframe=Timeframe.D1, scheduled_at=SCHEDULED_AT, completed_at=COMPLETED_AT
    )
    assert stored_rows(database) == [
        ("last_run.1d", "2024-07-05 20:00:30.000000", "2024-07-05 20:01:02.000000")
    ]


@pytest.mark.parametrize(
    ("offset", "replaces"),
    [
        pytest.param(timedelta(hours=-1), False, id="earlier"),
        pytest.param(timedelta(0), False, id="equal"),
        pytest.param(timedelta(hours=1), True, id="later"),
    ],
)
def test_a_run_is_recorded_monotonically(
    database: Database, offset: timedelta, replaces: bool
) -> None:
    with database.session() as session:
        state(session, frozen_clock(COMPLETED_AT)).record_run(Timeframe.D1, SCHEDULED_AT)
    before = stored_rows(database)
    later_completion = COMPLETED_AT + timedelta(days=1)

    with database.session() as session:
        reported = state(session, frozen_clock(later_completion)).record_run(
            Timeframe.D1, SCHEDULED_AT + offset
        )

    if replaces:
        assert reported == LastRun(
            timeframe=Timeframe.D1,
            scheduled_at=SCHEDULED_AT + offset,
            completed_at=later_completion,
        )
        assert stored_rows(database) != before
    else:
        assert reported == LastRun(
            timeframe=Timeframe.D1, scheduled_at=SCHEDULED_AT, completed_at=COMPLETED_AT
        )
        assert stored_rows(database) == before


def test_a_naive_scheduled_instant_is_refused_and_writes_nothing(database: Database) -> None:
    naive = datetime(2024, 7, 5, 20, 0, 30)  # a naive instant is the point of the case

    with database.session() as session, pytest.raises(ValueError, match="naive"):
        state(session, frozen_clock(COMPLETED_AT)).record_run(Timeframe.D1, naive)

    assert stored_rows(database) == []


def test_a_scheduled_instant_in_another_zone_is_the_same_instant(database: Database) -> None:
    eastern = timezone(timedelta(hours=-4))
    same_instant = SCHEDULED_AT.astimezone(eastern)

    with database.session() as session:
        state(session, frozen_clock(COMPLETED_AT)).record_run(Timeframe.D1, same_instant)
    with database.session() as session:
        repeated = state(session, frozen_clock(COMPLETED_AT)).record_run(Timeframe.D1, SCHEDULED_AT)
        loaded = state(session, frozen_clock(COMPLETED_AT)).load()

    assert repeated.scheduled_at == SCHEDULED_AT
    assert repeated.scheduled_at.tzinfo is UTC
    assert loaded.last_run(Timeframe.D1) == repeated
    assert stored_rows(database) == [
        ("last_run.1d", "2024-07-05 20:00:30.000000", "2024-07-05 20:01:02.000000")
    ]


def test_the_runs_are_ordered_by_timeframe(database: Database) -> None:
    with database.session() as session:
        repository = state(session, frozen_clock(COMPLETED_AT))
        for timeframe in (Timeframe.D1, Timeframe.H1, Timeframe.H4):
            repository.record_run(timeframe, SCHEDULED_AT)

    with database.session() as session:
        loaded = state(session, frozen_clock(COMPLETED_AT)).load()

    assert [run.timeframe for run in loaded.last_runs] == [
        Timeframe.H1,
        Timeframe.H4,
        Timeframe.D1,
    ]
    assert loaded.last_run(Timeframe.H4) is not None


def test_every_fact_is_read_back_at_once(database: Database) -> None:
    with database.session() as session:
        repository = state(session, frozen_clock(COMPLETED_AT))
        repository.record_run(Timeframe.H1, SCHEDULED_AT)
    with database.session() as session:
        state(session, frozen_clock(PAUSED_AT)).pause()
    with database.session() as session:
        state(session, frozen_clock(HEARTBEAT_AT)).record_heartbeat()

    with database.session() as session:
        loaded = state(session, frozen_clock(COMPLETED_AT)).load()

    assert loaded == BotState(
        paused_since=PAUSED_AT,
        last_heartbeat=HEARTBEAT_AT,
        last_runs=(
            LastRun(timeframe=Timeframe.H1, scheduled_at=SCHEDULED_AT, completed_at=COMPLETED_AT),
        ),
    )


def test_a_failed_unit_of_work_leaves_no_state(database: Database) -> None:
    with pytest.raises(RuntimeError, match="boom"), database.session() as session:  # noqa: PT012
        repository = state(session, frozen_clock(PAUSED_AT))
        repository.pause()
        repository.record_heartbeat()
        raise RuntimeError("boom")

    assert stored_rows(database) == []
    with database.session() as session:
        assert state(session, frozen_clock(PAUSED_AT)).load().paused is False


def test_the_repository_is_the_port_the_fixtures_build(database: Database) -> None:
    """The typed factory of the fixtures is what proves the ``Protocol`` conformance."""
    with database.session() as session:
        tools = repositories(session)
        paused_at = tools.state.pause()

        assert tools.state.load().paused_since == paused_at


# --- the records and the unit of work (AC10) -----------------------------------------------


@pytest.mark.parametrize("record", [LastRun, BotState], ids=lambda kind: kind.__name__)
def test_every_state_record_is_a_frozen_slotted_keyword_only_dataclass(
    record: type[object],
) -> None:
    assert dataclasses.is_dataclass(record)
    assert record.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    assert record.__dataclass_params__.kw_only is True  # type: ignore[attr-defined]
    assert "__slots__" in vars(record)


def test_a_state_record_rejects_attribute_assignment() -> None:
    run = LastRun(timeframe=Timeframe.D1, scheduled_at=SCHEDULED_AT, completed_at=COMPLETED_AT)

    with pytest.raises(dataclasses.FrozenInstanceError):
        run.completed_at = SCHEDULED_AT  # type: ignore[misc]


def test_the_repository_never_commits(database: Database) -> None:
    """Leaving the session without the context manager's commit must store nothing (AC10)."""
    session = database.session_factory()
    try:
        repository = state(session, frozen_clock(PAUSED_AT))
        repository.pause()
        repository.record_run(Timeframe.D1, SCHEDULED_AT)
    finally:
        session.rollback()
        session.close()

    assert stored_rows(database) == []
