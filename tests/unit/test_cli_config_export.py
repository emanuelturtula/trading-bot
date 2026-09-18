"""``config export`` (spec 013, T8, AC21, AC24).

The file is read back as bytes, so the line endings compared are the ones on disk: the export
must be identical on Windows and on Linux.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import repositories, run_cli, sample_rule
from tests.fixtures.rules import NAME_WITH_ACCENTS
from trading_bot.domain.rules.schema import dump_rule
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database

EMPTY_EXPORT = '{\n  "version": 1\n}\n'


def migrated(tmp_path: Path) -> Path:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    return data_dir


def seeded(tmp_path: Path) -> Path:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir) as database:
        _seed(database)
    return data_dir


def _seed(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        msft = handles.tickers.add("MSFT", Timeframe.D1, enabled=False)
        aapl = handles.tickers.add("AAPL", Timeframe.D1)
        handles.tickers.add("AAPL", Timeframe.H1)
        zulu = handles.rules.add(sample_rule("Zulu rule"), enabled=True)
        alpha = handles.rules.add(sample_rule("Alpha rule"))
        handles.rules.add(sample_rule("Hourly rule", timeframe="1h"))
        handles.assignments.assign(msft.id, zulu.id)
        handles.assignments.assign(aapl.id, zulu.id)
        handles.assignments.assign(aapl.id, alpha.id)


def exported(tmp_path: Path, data_dir: Path) -> str:
    destination = tmp_path / "configuration.json"
    result = run_cli(["config", "export", str(destination)], data_dir=data_dir)
    assert result.code == 0
    # Read as bytes: ``read_text`` would translate the line endings it is meant to check.
    return destination.read_bytes().decode("utf-8")


# --- the envelope --------------------------------------------------------------------------


def test_an_empty_database_exports_the_version_alone(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)

    result = run_cli(["config", "export", "-"], data_dir=data_dir)

    assert result.code == 0
    assert result.out == EMPTY_EXPORT


def test_the_sections_are_written_in_the_documented_order(tmp_path: Path) -> None:
    text = exported(tmp_path, seeded(tmp_path))

    assert list(json.loads(text)) == ["version", "tickers", "rules", "assignments"]


def test_the_tickers_are_sorted_by_symbol_and_timeframe(tmp_path: Path) -> None:
    document = json.loads(exported(tmp_path, seeded(tmp_path)))

    assert document["tickers"] == [
        {"symbol": "AAPL", "timeframe": "1d", "enabled": True},
        {"symbol": "AAPL", "timeframe": "1h", "enabled": True},
        {"symbol": "MSFT", "timeframe": "1d", "enabled": False},
    ]


def test_the_rules_are_sorted_by_name_with_their_flag_and_canonical_document(
    tmp_path: Path,
) -> None:
    document = json.loads(exported(tmp_path, seeded(tmp_path)))

    assert [item["rule"]["name"] for item in document["rules"]] == [
        "Alpha rule",
        "Hourly rule",
        "Zulu rule",
    ]
    assert [item["enabled"] for item in document["rules"]] == [False, False, True]
    assert document["rules"][0]["rule"] == dump_rule(sample_rule("Alpha rule"))


def test_the_assignments_are_sorted_by_natural_key(tmp_path: Path) -> None:
    document = json.loads(exported(tmp_path, seeded(tmp_path)))

    assert document["assignments"] == [
        {"symbol": "AAPL", "timeframe": "1d", "rule": "Alpha rule"},
        {"symbol": "AAPL", "timeframe": "1d", "rule": "Zulu rule"},
        {"symbol": "MSFT", "timeframe": "1d", "rule": "Zulu rule"},
    ]


def test_no_database_identifier_appears_anywhere(tmp_path: Path) -> None:
    text = exported(tmp_path, seeded(tmp_path))

    assert '"id"' not in text
    assert '"ticker_id"' not in text
    assert '"rule_id"' not in text


def test_a_section_without_items_is_absent(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)
    run_cli(["tickers", "add", "AAPL"], data_dir=data_dir)

    document = json.loads(exported(tmp_path, data_dir))

    assert list(document) == ["version", "tickers"]


# --- encoding and destinations -------------------------------------------------------------


def test_the_file_is_utf_8_with_newline_endings_and_a_trailing_newline(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)
    with temporary_database(data_dir) as database, database.session() as session:
        repositories(session).rules.add(sample_rule(NAME_WITH_ACCENTS))

    destination = tmp_path / "configuration.json"
    result = run_cli(["config", "export", str(destination)], data_dir=data_dir)
    raw = destination.read_bytes()

    assert result.code == 0
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    assert NAME_WITH_ACCENTS.encode("utf-8") in raw  # ensure_ascii=False keeps it readable


def test_the_dash_destination_writes_to_standard_output(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["config", "export", "-"], data_dir=data_dir)

    assert result.code == 0
    assert result.out == exported(tmp_path, data_dir)
    assert not (tmp_path / "-").exists()


def test_an_existing_file_is_refused_without_force(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    destination = tmp_path / "configuration.json"
    destination.write_text("keep me", encoding="utf-8")

    result = run_cli(["config", "export", str(destination)], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [f"error: {destination} already exists; pass --force to overwrite it"]
    assert destination.read_text(encoding="utf-8") == "keep me"


def test_force_overwrites_an_existing_file_and_reports_it(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    destination = tmp_path / "configuration.json"
    destination.write_text("replace me", encoding="utf-8")

    result = run_cli(["config", "export", "--force", str(destination)], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [f"exported {destination}"]
    assert destination.read_text(encoding="utf-8").startswith('{\n  "version": 1')


def test_a_destination_that_cannot_be_written_exits_three(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    destination = tmp_path / "missing-directory" / "configuration.json"

    result = run_cli(["config", "export", str(destination)], data_dir=data_dir)

    assert result.code == 3
    assert result.errors == [f"error: {destination} cannot be written"]
    assert not destination.parent.exists()
