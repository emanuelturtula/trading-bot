"""``config import`` (spec 013, T9, AC12, AC21, AC23, AC25).

Every file is built from ``tests.fixtures.rules`` payloads and written under ``tmp_path`` with
``newline="\\n"``, so the same bytes are parsed on Windows and on Linux.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Final

import pytest

from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import (
    database_snapshot,
    repositories,
    run_cli,
    sample_rule,
)
from tests.fixtures.rules import rule_payload, simple_condition
from trading_bot.cli.files import MAX_IMPORT_BYTES, MAX_IMPORT_ITEMS
from trading_bot.domain.indicators.registry import JsonValue
from trading_bot.domain.rules.schema import dump_rule
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database

DAILY: Final = "Daily breakout"
HOURLY: Final = "Hourly breakout"


def migrated(tmp_path: Path) -> Path:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    return data_dir


def seeded(tmp_path: Path) -> Path:
    """AAPL 1d enabled and the ``Daily breakout`` rule, disabled and unassigned."""
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir) as database:
        _seed(database)
    return data_dir


def _seed(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        handles.tickers.add("AAPL", Timeframe.D1)
        handles.rules.add(sample_rule(DAILY))


def rule_item(name: str, *, enabled: bool = False, **overrides: JsonValue) -> dict[str, object]:
    payload = rule_payload({"all": [simple_condition()]}, name=name, **overrides)
    return {"enabled": enabled, "rule": payload}


def written(tmp_path: Path, document: object, name: str = "configuration.json") -> str:
    path = tmp_path / name
    text = json.dumps(document, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return str(path)


def whole_configuration() -> dict[str, object]:
    return {
        "version": 1,
        "tickers": [
            {"symbol": "MSFT", "timeframe": "1d", "enabled": True},
            {"symbol": "NVDA", "timeframe": "1h", "enabled": False},
        ],
        "rules": [rule_item(HOURLY, timeframe="1h"), rule_item("Fresh rule")],
        "assignments": [
            {"symbol": "MSFT", "timeframe": "1d", "rule": "Fresh rule"},
            {"symbol": "NVDA", "timeframe": "1h", "rule": HOURLY},
        ],
    }


# --- a whole configuration in one transaction (AC23) ---------------------------------------


def test_a_file_with_all_three_sections_is_applied_in_order(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)
    source = written(tmp_path, whole_configuration())

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [
        "created ticker MSFT 1d (enabled)",
        "created ticker NVDA 1h (disabled)",
        "created rule 'Hourly breakout' (id 1, 1h, BUY, disabled)",
        "created rule 'Fresh rule' (id 2, 1d, BUY, disabled)",
        "created assignment MSFT 1d -> 'Fresh rule'",
        "created assignment NVDA 1h -> 'Hourly breakout'",
        f"imported {source}: 2 tickers, 2 rules, 2 assignments"
        " (6 created, 0 replaced, 0 unchanged, 0 skipped)",
    ]


def test_an_assignment_is_resolved_against_the_database_too(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(
        tmp_path,
        {
            "version": 1,
            "assignments": [{"symbol": "AAPL", "timeframe": "1d", "rule": DAILY}],
        },
    )

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 0
    assert result.lines[0] == "created assignment AAPL 1d -> 'Daily breakout'"
    assert run_cli(["assignments", "list"], data_dir=data_dir).lines == [
        "AAPL 1d -> 'Daily breakout'"
    ]


def test_a_rules_only_file_touches_nothing_else(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, {"version": 1, "rules": [rule_item("Fresh rule")]})

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 0
    assert run_cli(["tickers", "list"], data_dir=data_dir).lines == ["AAPL 1d enabled 0 rules"]
    assert run_cli(["assignments", "list"], data_dir=data_dir).out == ""


def test_a_tickers_only_file_touches_nothing_else(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, {"version": 1, "tickers": [{"symbol": "MSFT"}]})

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 0
    assert result.lines[0] == "created ticker MSFT 1d (enabled)"
    assert run_cli(["rules", "list"], data_dir=data_dir).lines == [
        "'Daily breakout' 1d BUY disabled 0 tickers"
    ]


def test_import_reads_standard_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = migrated(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(whole_configuration())))

    result = run_cli(["config", "import", "-"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines[-1].startswith("imported -: 2 tickers, 2 rules, 2 assignments")


def test_import_never_deletes_what_the_file_does_not_mention(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, {"version": 1, "tickers": [{"symbol": "MSFT"}]})

    run_cli(["config", "import", source], data_dir=data_dir)

    assert run_cli(["tickers", "list"], data_dir=data_dir).lines == [
        "AAPL 1d enabled 0 rules",
        "MSFT 1d enabled 0 rules",
    ]
    assert run_cli(["rules", "list"], data_dir=data_dir).lines == [
        "'Daily breakout' 1d BUY disabled 0 tickers"
    ]


# --- the conflict policies (AC23) ----------------------------------------------------------


def existing_configuration(*, enabled: bool = True) -> dict[str, object]:
    return {
        "version": 1,
        "tickers": [{"symbol": "AAPL", "timeframe": "1d", "enabled": enabled}],
        "rules": [rule_item(DAILY, enabled=enabled, cooldown_bars=7)],
    }


def test_skip_leaves_every_existing_item_alone(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, existing_configuration(enabled=False))
    before = database_snapshot(data_dir)

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [
        "skipped ticker AAPL 1d (enabled): it is already tracked",
        "skipped rule 'Daily breakout': a rule with that name already exists (id 1)",
        f"imported {source}: 1 ticker, 1 rule, 0 assignments"
        " (0 created, 0 replaced, 0 unchanged, 2 skipped)",
    ]
    assert database_snapshot(data_dir) == before


def test_replace_updates_the_document_and_the_flags(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, existing_configuration())

    result = run_cli(["config", "import", "--on-conflict", "replace", source], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [
        "unchanged ticker AAPL 1d (enabled)",
        "replaced rule 'Daily breakout' (id 1, 1d, BUY, enabled)",
        f"imported {source}: 1 ticker, 1 rule, 0 assignments"
        " (0 created, 1 replaced, 1 unchanged, 0 skipped)",
    ]
    document = json.loads(run_cli(["config", "export", "-"], data_dir=data_dir).out)
    assert document["rules"][0]["rule"]["cooldown_bars"] == 7


def test_fail_rejects_the_first_existing_item_and_writes_nothing(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, whole_configuration() | existing_configuration())
    before = database_snapshot(data_dir)

    result = run_cli(["config", "import", "--on-conflict", "fail", source], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: tickers[0]: ticker AAPL 1d already exists"]
    assert result.out == ""
    assert database_snapshot(data_dir) == before


def test_fail_reports_an_existing_rule_with_its_path(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, {"version": 1, "rules": [rule_item(DAILY)]})

    result = run_cli(["config", "import", "--on-conflict", "fail", source], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: rules[0]: a rule named 'Daily breakout' already exists (id 1)"]


def test_an_existing_assignment_is_unchanged_under_every_policy(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    run_cli(["assignments", "add", "AAPL", DAILY], data_dir=data_dir)
    source = written(tmp_path, {"version": 1, "assignments": [{"symbol": "AAPL", "rule": DAILY}]})

    for policy in ("skip", "replace", "fail"):
        result = run_cli(["config", "import", "--on-conflict", policy, source], data_dir=data_dir)

        assert result.code == 0, policy
        assert result.lines[0] == "unchanged assignment AAPL 1d -> 'Daily breakout'", policy


# --- rejections leave the database untouched (AC23, AC12) ----------------------------------


@pytest.mark.parametrize(
    ("document", "message"),
    [
        pytest.param({"version": 2}, "error: version: version must be 1", id="version"),
        pytest.param(
            {"version": 1, "extra": []},
            "error: unknown key 'extra'; expected one of version, tickers, rules, assignments",
            id="unknown-envelope-key",
        ),
        pytest.param(
            {"version": 1, "tickers": {"symbol": "AAPL"}},
            "error: tickers: tickers must be a list",
            id="section-not-a-list",
        ),
        pytest.param(
            {"version": 1, "tickers": ["AAPL"]},
            "error: tickers[0]: an item must be a JSON object",
            id="item-not-an-object",
        ),
        pytest.param(
            {"version": 1, "tickers": [{"symbol": "AAPL", "ticker": "AAPL"}]},
            "error: tickers[0]: unknown key 'ticker'; expected one of symbol, timeframe, enabled",
            id="unknown-item-key",
        ),
        pytest.param(
            {"version": 1, "tickers": [{"symbol": "AA PL"}]},
            "error: tickers[0].symbol: invalid ticker",
            id="malformed-symbol",
        ),
        pytest.param(
            {"version": 1, "tickers": [{"symbol": "AAPL", "timeframe": "1D"}]},
            "error: tickers[0].timeframe: unknown timeframe",
            id="unknown-timeframe",
        ),
        pytest.param(
            {"version": 1, "tickers": [{"symbol": "AAPL", "enabled": "yes"}]},
            "error: tickers[0].enabled: enabled must be true or false",
            id="flag-not-a-boolean",
        ),
        pytest.param(
            {"version": 1, "tickers": [{"symbol": 5}]},
            "error: tickers[0].symbol: symbol must be a string",
            id="symbol-not-a-string",
        ),
        pytest.param(
            {"version": 1, "tickers": [{"symbol": "AAPL", "timeframe": 1}]},
            "error: tickers[0].timeframe: timeframe must be a string",
            id="timeframe-not-a-string",
        ),
        pytest.param(
            {"version": 1, "rules": [{"enabled": False, "rule": "text"}]},
            "error: rules[0].rule: rule must be a JSON object",
            id="rule-not-an-object",
        ),
        pytest.param(
            {"version": 1, "rules": [{"enabled": False, "rule": {"name": "x"}}]},
            "error: rules[0].rule: signal: Field required",
            id="invalid-rule-document",
        ),
        pytest.param(
            {"version": 1, "assignments": [{"symbol": "AAPL", "rule": 5}]},
            "error: assignments[0].rule: rule must be a rule name",
            id="assignment-rule-not-a-name",
        ),
    ],
)
def test_an_invalid_item_is_rejected_with_its_path(
    tmp_path: Path, document: dict[str, object], message: str
) -> None:
    data_dir = seeded(tmp_path)
    before = database_snapshot(data_dir)
    source = written(tmp_path, document)

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 1
    assert result.err.startswith(message)
    assert result.out == ""
    assert database_snapshot(data_dir) == before


def test_a_valid_item_after_an_invalid_one_is_not_written(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    before = database_snapshot(data_dir)
    source = written(
        tmp_path,
        {
            "version": 1,
            "tickers": [{"symbol": "MSFT"}, {"symbol": "AA PL"}],
            "rules": [rule_item("Fresh rule")],
        },
    )

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 1
    assert database_snapshot(data_dir) == before


@pytest.mark.parametrize(
    ("document", "message"),
    [
        pytest.param(
            {
                "version": 1,
                "tickers": [{"symbol": "MSFT"}, {"symbol": "msft", "timeframe": "1d"}],
            },
            "error: tickers[1]: MSFT 1d appears twice",
            id="two-tickers",
        ),
        pytest.param(
            {"version": 1, "rules": [rule_item("Twice"), rule_item("Twice", enabled=True)]},
            "error: rules[1]: a rule with that name appears twice",
            id="two-rules",
        ),
        pytest.param(
            {
                "version": 1,
                "assignments": [
                    {"symbol": "AAPL", "rule": DAILY},
                    {"symbol": "AAPL", "timeframe": "1d", "rule": DAILY},
                ],
            },
            "error: assignments[1]: that assignment appears twice",
            id="two-assignments",
        ),
    ],
)
def test_duplicates_inside_the_file_are_rejected(
    tmp_path: Path, document: dict[str, object], message: str
) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, document)

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [message]


def test_an_assignment_naming_a_missing_ticker_is_rejected(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, {"version": 1, "assignments": [{"symbol": "TSLA", "rule": DAILY}]})

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: assignments[0]: no ticker TSLA 1d in the file or the database"]


def test_an_assignment_naming_a_missing_rule_is_rejected(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(
        tmp_path, {"version": 1, "assignments": [{"symbol": "AAPL", "rule": "No such rule"}]}
    )

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [
        "error: assignments[0]: no rule named 'No such rule' in the file or the database"
    ]


def test_an_assignment_whose_timeframes_differ_is_rejected(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(
        tmp_path,
        {
            "version": 1,
            "rules": [rule_item(HOURLY, timeframe="1h")],
            "assignments": [{"symbol": "AAPL", "timeframe": "1d", "rule": HOURLY}],
        },
    )
    before = database_snapshot(data_dir)

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [
        "error: assignments[0]: rule 'Hourly breakout' is evaluated on 1h,"
        " but ticker AAPL is tracked on 1d"
    ]
    assert database_snapshot(data_dir) == before


def test_replacing_the_timeframe_of_an_assigned_rule_is_rejected(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    run_cli(["assignments", "add", "AAPL", DAILY], data_dir=data_dir)
    source = written(tmp_path, {"version": 1, "rules": [rule_item(DAILY, timeframe="1h")]})
    before = database_snapshot(data_dir)

    result = run_cli(["config", "import", "--on-conflict", "replace", source], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [
        "error: rules[0]: rule 'Daily breakout' is assigned to 1 ticker on 1d,"
        " so its timeframe cannot change to 1h"
    ]
    assert database_snapshot(data_dir) == before


def test_replace_that_would_change_nothing_leaves_the_row_alone(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, {"version": 1, "rules": [rule_item(DAILY)]})
    before = database_snapshot(data_dir)

    result = run_cli(["config", "import", "--on-conflict", "replace", source], data_dir=data_dir)

    assert result.code == 0
    assert result.lines[0] == "unchanged rule 'Daily breakout' (id 1, 1d, BUY, disabled)"
    assert database_snapshot(data_dir) == before


def test_replace_switches_the_flag_of_an_existing_ticker(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, {"version": 1, "tickers": [{"symbol": "AAPL", "enabled": False}]})

    result = run_cli(["config", "import", "--on-conflict", "replace", source], data_dir=data_dir)

    assert result.code == 0
    assert result.lines[0] == "replaced ticker AAPL 1d (disabled)"
    assert run_cli(["tickers", "list"], data_dir=data_dir).lines == ["AAPL 1d disabled 0 rules"]


# --- unreadable files and the caps (AC25) --------------------------------------------------


def test_a_missing_file_is_rejected(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = str(tmp_path / "not-there.json")

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [f"error: {source} cannot be read"]


def test_a_directory_given_as_the_file_is_rejected(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["config", "import", str(tmp_path)], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [f"error: {tmp_path} cannot be read"]


def test_standard_input_that_cannot_be_read_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unreadable(io.StringIO):
        def read(self, size: int | None = -1) -> str:
            raise OSError("no input")

    data_dir = seeded(tmp_path)
    monkeypatch.setattr(sys, "stdin", Unreadable())

    result = run_cli(["config", "import", "-"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: standard input cannot be read"]


def test_a_file_that_is_not_valid_utf_8_is_rejected(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = tmp_path / "configuration.json"
    source.write_bytes(b'{"version": 1, "tickers": [{"symbol": "\xff\xfe"}]}')

    result = run_cli(["config", "import", str(source)], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: the file is not valid UTF-8"]


def test_a_file_that_is_not_json_is_rejected(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = tmp_path / "configuration.json"
    source.write_text("not json at all", encoding="utf-8", newline="\n")

    result = run_cli(["config", "import", str(source)], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: the file is not valid JSON"]


def test_a_json_array_is_rejected(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, [{"version": 1}])

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: the file must hold a JSON object"]


def test_a_file_above_the_byte_cap_is_rejected_before_parsing(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = tmp_path / "configuration.json"
    source.write_bytes(b" " * (MAX_IMPORT_BYTES + 1))

    result = run_cli(["config", "import", str(source)], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [f"error: the file is larger than {MAX_IMPORT_BYTES} bytes"]


def test_a_file_above_the_item_cap_is_rejected(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    tickers = [{"symbol": f"T{index}"} for index in range(MAX_IMPORT_ITEMS + 1)]
    source = written(tmp_path, {"version": 1, "tickers": tickers})

    result = run_cli(["config", "import", source], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [f"error: the file holds more than {MAX_IMPORT_ITEMS} items"]


# --- dry runs (AC23, AC37) -----------------------------------------------------------------


def test_a_dry_run_writes_nothing_and_exits_zero(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)
    source = written(tmp_path, whole_configuration())
    before = database_snapshot(data_dir)

    result = run_cli(["config", "import", "--dry-run", source], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [
        "dry run: would create ticker MSFT 1d (enabled)",
        "dry run: would create ticker NVDA 1h (disabled)",
        "dry run: would create rule 'Hourly breakout' (1h, BUY, disabled)",
        "dry run: would create rule 'Fresh rule' (1d, BUY, disabled)",
        "dry run: would create assignment MSFT 1d -> 'Fresh rule'",
        "dry run: would create assignment NVDA 1h -> 'Hourly breakout'",
        "dry run: nothing was written",
    ]
    assert database_snapshot(data_dir) == before


def test_a_dry_run_of_a_rejected_file_still_exits_one(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    source = written(tmp_path, {"version": 1, "tickers": [{"symbol": "AA PL"}]})

    result = run_cli(["config", "import", "--dry-run", source], data_dir=data_dir)

    assert result.code == 1


def test_the_stored_document_is_the_canonical_one(tmp_path: Path) -> None:
    """The file may be written in any equivalent shape; the row holds the canonical text."""
    data_dir = migrated(tmp_path)
    source = written(tmp_path, {"version": 1, "rules": [rule_item("Fresh rule")]})

    run_cli(["config", "import", source], data_dir=data_dir)

    with temporary_database(data_dir) as database, database.session() as session:
        stored = repositories(session).rules.get_by_name("Fresh rule")

    assert stored is not None
    assert dump_rule(stored.rule) == dump_rule(sample_rule("Fresh rule"))
