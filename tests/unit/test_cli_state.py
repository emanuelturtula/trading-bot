"""``state show`` (spec 014, T12, AC25).

The five lines of Design 10.4 are literals, and the command writes nothing: a missing row is
printed as ``never``, never seeded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import (
    database_snapshot,
    frozen_clock,
    repositories,
    run_cli,
)
from trading_bot.domain.timeframe import Timeframe

PAUSED_AT = datetime(2024, 7, 5, 21, 0, tzinfo=UTC)
HEARTBEAT_AT = datetime(2024, 7, 5, 20, 30, tzinfo=UTC)
SCHEDULED_AT = datetime(2024, 7, 5, 20, 0, 30, tzinfo=UTC)
COMPLETED_AT = datetime(2024, 7, 5, 20, 1, 2, tzinfo=UTC)

EMPTY_LINES = [
    "paused: no",
    "last heartbeat: never",
    "last run 1h: never",
    "last run 4h: never",
    "last run 1d: never",
]


def migrated(tmp_path: Path) -> Path:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    return data_dir


def populated(tmp_path: Path) -> Path:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir) as database:
        with database.session() as session:
            repositories(session, clock=frozen_clock(PAUSED_AT)).state.pause()
        with database.session() as session:
            repositories(session, clock=frozen_clock(HEARTBEAT_AT)).state.record_heartbeat()
        with database.session() as session:
            state = repositories(session, clock=frozen_clock(COMPLETED_AT)).state
            for timeframe in Timeframe:
                state.record_run(timeframe, SCHEDULED_AT)
    return data_dir


def test_an_empty_table_reads_as_never(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)

    result = run_cli(["state", "show"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == EMPTY_LINES


def test_a_populated_table_shows_every_fact(tmp_path: Path) -> None:
    data_dir = populated(tmp_path)

    result = run_cli(["state", "show"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [
        "paused: since 2024-07-05T21:00:00+00:00",
        "last heartbeat: 2024-07-05T20:30:00+00:00",
        "last run 1h: 2024-07-05T20:00:30+00:00 (completed 2024-07-05T20:01:02+00:00)",
        "last run 4h: 2024-07-05T20:00:30+00:00 (completed 2024-07-05T20:01:02+00:00)",
        "last run 1d: 2024-07-05T20:00:30+00:00 (completed 2024-07-05T20:01:02+00:00)",
    ]


def test_a_partly_populated_table_mixes_facts_and_never(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)
    with temporary_database(data_dir) as database, database.session() as session:
        repositories(session, clock=frozen_clock(COMPLETED_AT)).state.record_run(
            Timeframe.D1, SCHEDULED_AT
        )

    result = run_cli(["state", "show"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [
        "paused: no",
        "last heartbeat: never",
        "last run 1h: never",
        "last run 4h: never",
        "last run 1d: 2024-07-05T20:00:30+00:00 (completed 2024-07-05T20:01:02+00:00)",
    ]


def test_showing_the_state_writes_nothing(tmp_path: Path) -> None:
    data_dir = populated(tmp_path)
    before = database_snapshot(data_dir)

    run_cli(["state", "show"], data_dir=data_dir)

    assert database_snapshot(data_dir) == before


def test_the_command_takes_no_argument(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)

    result = run_cli(["state", "show", "--force"], data_dir=data_dir)

    assert result.code == 2
