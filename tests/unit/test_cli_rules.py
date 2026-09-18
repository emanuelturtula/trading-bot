"""The rule subcommands (spec 013, T18, AC21, AC34, AC36, AC37).

A rule is addressed by its unique name, echoed ``repr()``-escaped, and the rule **document** is
never printed: ``config export`` is the way to read it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import database_snapshot, repositories, run_cli, sample_rule
from tests.fixtures.rules import NAME_WITH_ACCENTS
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database

QUOTED_NAME = "a rule name with 'quotes' and spaces"
DRY_RUNS = [
    ["rules", "enable", "Daily breakout"],
    ["rules", "disable", "Enabled rule"],
    ["rules", "remove", "Daily breakout", "--force"],
    ["rules", "remove", "Unassigned rule"],
]


def migrated(tmp_path: Path) -> Path:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    return data_dir


def seeded(tmp_path: Path) -> Path:
    """Four rules: one assigned and disabled, one unassigned, one enabled, one oddly named."""
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir) as database:
        _seed(database)
    return data_dir


def _seed(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        assigned = handles.rules.add(sample_rule("Daily breakout"))
        handles.rules.add(sample_rule("Unassigned rule", signal="SELL"))
        handles.rules.add(sample_rule("Enabled rule", timeframe="1h"), enabled=True)
        handles.rules.add(sample_rule(QUOTED_NAME))
        handles.assignments.assign(ticker.id, assigned.id)


# --- list ----------------------------------------------------------------------------------


def test_list_prints_nothing_on_an_empty_database(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)

    result = run_cli(["rules", "list"], data_dir=data_dir)

    assert result.code == 0
    assert result.out == ""


def test_list_is_ordered_by_name_and_shows_the_assignment_count(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["rules", "list"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [
        "'Daily breakout' 1d BUY disabled 1 ticker",
        "'Enabled rule' 1h BUY enabled 0 tickers",
        "'Unassigned rule' 1d SELL disabled 0 tickers",
        "\"a rule name with 'quotes' and spaces\" 1d BUY disabled 0 tickers",
    ]


def test_no_line_ever_carries_the_rule_document(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    listed = run_cli(["rules", "list"], data_dir=data_dir)
    enabled = run_cli(["rules", "enable", "Daily breakout"], data_dir=data_dir)

    for output in (listed.out, enabled.out):
        assert "conditions" not in output
        assert "indicator" not in output
        assert "crosses" not in output


# --- enable and disable --------------------------------------------------------------------


def test_enable_switches_a_disabled_rule(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["rules", "enable", "Daily breakout"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["enabled rule 'Daily breakout' (id 1, 1d, BUY)"]


def test_enable_on_an_enabled_rule_is_reported_as_unchanged(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["rules", "enable", "Enabled rule"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["unchanged rule 'Enabled rule' (id 3, 1h, BUY, enabled)"]


def test_disable_switches_an_enabled_rule(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["rules", "disable", "Enabled rule"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["disabled rule 'Enabled rule' (id 3, 1h, BUY)"]


def test_a_name_that_needs_quoting_is_addressed_as_written(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["rules", "enable", QUOTED_NAME], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["enabled rule \"a rule name with 'quotes' and spaces\" (id 4, 1d, BUY)"]


def test_a_missing_name_is_refused(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["rules", "enable", "daily breakout"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: no rule named 'daily breakout' is stored"]


def test_an_accented_name_is_reported_without_raising(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)
    with temporary_database(data_dir) as database, database.session() as session:
        repositories(session).rules.add(sample_rule(NAME_WITH_ACCENTS))

    result = run_cli(["rules", "enable", NAME_WITH_ACCENTS], data_dir=data_dir)

    assert result.code == 0
    assert NAME_WITH_ACCENTS in result.out


# --- remove --------------------------------------------------------------------------------


def test_remove_deletes_a_rule_without_assignments(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["rules", "remove", "Unassigned rule"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["removed rule 'Unassigned rule'"]


def test_remove_refuses_a_rule_with_assignments(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["rules", "remove", "Daily breakout"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [
        "error: rule 'Daily breakout' has 1 assignment; pass --force to remove them with it"
    ]


def test_remove_with_force_reports_the_assignments_it_took(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["rules", "remove", "Daily breakout", "--force"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["removed rule 'Daily breakout' with 1 assignment"]
    assert run_cli(["assignments", "list"], data_dir=data_dir).out == ""
    assert run_cli(["tickers", "list"], data_dir=data_dir).lines == ["AAPL 1d enabled 0 rules"]


def test_remove_refuses_a_missing_rule(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["rules", "remove", "No such rule"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: no rule named 'No such rule' is stored"]


def test_a_stored_document_that_no_longer_parses_stops_the_command(tmp_path: Path) -> None:
    """Decision D93: the failure is loud and readable, and the document is never echoed."""
    data_dir = seeded(tmp_path)
    with temporary_database(data_dir) as database, database.session() as session:
        session.execute(
            text("UPDATE rules SET definition_json = '{}' WHERE name = :name"),
            {"name": "Daily breakout"},
        )

    result = run_cli(["rules", "list"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [
        "error: the document of rule 1 is not valid: missing_field at name,"
        " missing_field at signal, missing_field at timeframe, missing_field at conditions"
    ]


def test_a_name_far_beyond_the_bound_is_truncated_in_the_message(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["rules", "enable", "n" * 300], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [f"error: no rule named '{'n' * 80}...' is stored"]


# --- dry runs (AC37) -----------------------------------------------------------------------


@pytest.mark.parametrize("argv", DRY_RUNS, ids=[" ".join(argv) for argv in DRY_RUNS])
def test_a_dry_run_leaves_the_database_byte_identical(tmp_path: Path, argv: list[str]) -> None:
    data_dir = seeded(tmp_path)
    before = database_snapshot(data_dir)

    result = run_cli([*argv, "--dry-run"], data_dir=data_dir)

    assert result.code == 0
    assert database_snapshot(data_dir) == before


def test_a_dry_run_carries_no_identifier(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["rules", "enable", "Daily breakout", "--dry-run"], data_dir=data_dir)

    assert result.lines == [
        "dry run: would enable rule 'Daily breakout' (1d, BUY)",
        "dry run: nothing was written",
    ]


def test_a_dry_run_of_a_removal_reports_the_assignments(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(
        ["rules", "remove", "Daily breakout", "--force", "--dry-run"], data_dir=data_dir
    )

    assert result.lines == [
        "dry run: would remove rule 'Daily breakout' with 1 assignment",
        "dry run: nothing was written",
    ]


def test_a_dry_run_of_a_rejected_removal_exits_one(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    before = database_snapshot(data_dir)

    result = run_cli(["rules", "remove", "Daily breakout", "--dry-run"], data_dir=data_dir)

    assert result.code == 1
    assert database_snapshot(data_dir) == before
