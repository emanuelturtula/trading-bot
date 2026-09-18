"""The assignment subcommands (spec 013, T19, AC21, AC35, AC37).

The timeframe mismatch is checked against the literal line of §9.6: the operator must never see
an ``IntegrityError`` traceback, only that sentence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import database_snapshot, repositories, run_cli, sample_rule
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database

MISMATCH = "error: rule 'Hourly breakout' is evaluated on 1h, but ticker AAPL is tracked on 1d"
DRY_RUNS = [
    ["assignments", "add", "MSFT", "Daily breakout"],
    ["assignments", "add", "AAPL", "Daily breakout"],
    ["assignments", "remove", "AAPL", "Daily breakout"],
]


def seeded(tmp_path: Path) -> Path:
    """AAPL 1d and MSFT 1d, a daily rule already assigned to AAPL and an hourly rule."""
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir) as database:
        _seed(database)
    return data_dir


def _seed(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        aapl = handles.tickers.add("AAPL", Timeframe.D1)
        handles.tickers.add("MSFT", Timeframe.D1)
        daily = handles.rules.add(sample_rule("Daily breakout"))
        handles.rules.add(sample_rule("Hourly breakout", timeframe="1h"))
        handles.assignments.assign(aapl.id, daily.id)


# --- add -----------------------------------------------------------------------------------


def test_add_assigns_a_rule_of_the_same_timeframe(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["assignments", "add", "MSFT", "Daily breakout"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["created assignment MSFT 1d -> 'Daily breakout'"]


def test_add_twice_is_reported_as_unchanged(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["assignments", "add", "aapl", "Daily breakout"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["unchanged assignment AAPL 1d -> 'Daily breakout'"]
    assert len(run_cli(["assignments", "list"], data_dir=data_dir).lines) == 1


def test_a_timeframe_mismatch_is_a_readable_line(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["assignments", "add", "AAPL", "Hourly breakout"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [MISMATCH]
    assert "IntegrityError" not in result.err
    assert "FOREIGN KEY" not in result.err


def test_a_missing_ticker_names_only_the_ticker(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["assignments", "add", "TSLA", "No such rule"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: no ticker TSLA 1d is stored"]


def test_a_missing_rule_names_only_the_rule(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["assignments", "add", "AAPL", "No such rule"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: no rule named 'No such rule' is stored"]


# --- remove --------------------------------------------------------------------------------


def test_remove_deletes_an_existing_assignment(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["assignments", "remove", "AAPL", "Daily breakout"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["removed assignment AAPL 1d -> 'Daily breakout'"]
    assert run_cli(["assignments", "list"], data_dir=data_dir).out == ""


def test_remove_refuses_a_missing_assignment(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["assignments", "remove", "MSFT", "Daily breakout"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: no assignment MSFT 1d -> 'Daily breakout' is stored"]


# --- list ----------------------------------------------------------------------------------


def test_list_prints_nothing_when_there_is_none(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    run_cli(["assignments", "remove", "AAPL", "Daily breakout"], data_dir=data_dir)

    result = run_cli(["assignments", "list"], data_dir=data_dir)

    assert result.code == 0
    assert result.out == ""


def test_list_is_ordered_by_symbol_timeframe_and_rule(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    run_cli(["assignments", "add", "MSFT", "Daily breakout"], data_dir=data_dir)
    run_cli(["tickers", "add", "AAPL", "--timeframe", "1h"], data_dir=data_dir)
    run_cli(
        ["assignments", "add", "AAPL", "Hourly breakout", "--timeframe", "1h"], data_dir=data_dir
    )

    result = run_cli(["assignments", "list"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [
        "AAPL 1d -> 'Daily breakout'",
        "AAPL 1h -> 'Hourly breakout'",
        "MSFT 1d -> 'Daily breakout'",
    ]


# --- dry runs (AC37) -----------------------------------------------------------------------


@pytest.mark.parametrize("argv", DRY_RUNS, ids=[" ".join(argv) for argv in DRY_RUNS])
def test_a_dry_run_leaves_the_database_byte_identical(tmp_path: Path, argv: list[str]) -> None:
    data_dir = seeded(tmp_path)
    before = database_snapshot(data_dir)

    result = run_cli([*argv, "--dry-run"], data_dir=data_dir)

    assert result.code == 0
    assert database_snapshot(data_dir) == before


def test_a_dry_run_uses_the_conditional_form(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(
        ["assignments", "add", "MSFT", "Daily breakout", "--dry-run"], data_dir=data_dir
    )

    assert result.lines == [
        "dry run: would create assignment MSFT 1d -> 'Daily breakout'",
        "dry run: nothing was written",
    ]


def test_a_dry_run_of_a_rejected_request_still_exits_one(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    before = database_snapshot(data_dir)

    result = run_cli(
        ["assignments", "add", "AAPL", "Hourly breakout", "--dry-run"], data_dir=data_dir
    )

    assert result.code == 1
    assert result.errors == [MISMATCH]
    assert database_snapshot(data_dir) == before
