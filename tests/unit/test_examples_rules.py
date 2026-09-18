"""The shipped example rules (spec 013, T11, AC27).

Documentation that is executed cannot drift: an indicator catalog change that invalidated an
example fails here instead of failing on the Raspberry Pi.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import trading_bot
from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import repositories, run_cli
from trading_bot.cli.files import read_configuration
from trading_bot.domain.rules.schema import dump_rule, parse_rule

EXAMPLES_DIR = Path(trading_bot.__file__).resolve().parents[2] / "docs" / "examples" / "rules"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("*.json"))
EXPECTED_NAMES = {
    "bollinger-breakout-on-volume.json": "Bollinger breakout on volume",
    "macd-bearish-crossover.json": "MACD bearish crossover",
    "rsi-oversold-in-uptrend.json": "RSI oversold in uptrend",
}
DISCLAIMER = "Not financial advice."


def slug(name: str) -> str:
    return "".join(character if character.isalnum() else "-" for character in name.lower())


def test_the_expected_example_files_are_present() -> None:
    """Guards against an empty glob making every parametrized test vacuous."""
    assert [path.name for path in EXAMPLE_FILES] == sorted(EXPECTED_NAMES)


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.name)
def test_every_example_is_a_valid_rules_only_import_file(path: Path) -> None:
    configuration = read_configuration(path.read_bytes())

    assert len(configuration.rules) == 1
    assert configuration.tickers == ()
    assert configuration.assignments == ()


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.name)
def test_every_example_ships_disabled_and_in_canonical_form(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    item = document["rules"][0]
    rule = parse_rule(item["rule"])

    assert document["version"] == 1
    assert item["enabled"] is False
    assert item["rule"] == dump_rule(rule)
    assert rule.name == EXPECTED_NAMES[path.name]


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.name)
def test_every_file_name_matches_the_slug_of_its_rule(path: Path) -> None:
    rule = parse_rule(json.loads(path.read_text(encoding="utf-8"))["rules"][0]["rule"])

    assert path.stem == slug(rule.name)


def test_every_example_name_is_unique_across_the_directory() -> None:
    names = [
        parse_rule(json.loads(path.read_text(encoding="utf-8"))["rules"][0]["rule"]).name
        for path in EXAMPLE_FILES
    ]

    assert sorted(names) == sorted(set(names))


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.name)
def test_importing_an_example_creates_one_disabled_rule_and_nothing_else(
    tmp_path: Path, path: Path
) -> None:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass

    result = run_cli(["config", "import", str(path)], data_dir=data_dir)

    assert result.code == 0
    with temporary_database(data_dir) as database, database.session() as session:
        handles = repositories(session)
        stored = handles.rules.list_all()

        assert [rule.name for rule in stored] == [EXPECTED_NAMES[path.name]]
        assert [rule.enabled for rule in stored] == [False]
        assert handles.tickers.list_all() == ()
        assert handles.assignments.list_all() == ()


def test_the_readme_documents_the_examples_and_carries_the_disclaimer() -> None:
    readme = (EXAMPLES_DIR / "README.md").read_text(encoding="utf-8")

    assert DISCLAIMER in readme
    assert "python -m trading_bot.cli config import" in readme
    for name in EXPECTED_NAMES:
        assert name in readme
