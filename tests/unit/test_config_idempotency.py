"""Configuration idempotency across the CLI (spec 013, T12, AC23, AC24, AC35).

Rule 5 ("a signal is identified by its natural key; reprocessing does not resend it") has no
signal to reprocess yet (#13 is out of scope, see the spec's hand-off list); this is its
analogue for configuration: reapplying the same mutation twice must never create a second row,
move a timestamp or burn an identifier. Every comparison here is the exact bytes
``database_snapshot`` reads back (rows, ids, ``created_at``/``updated_at`` and
``sqlite_sequence``), never only the CLI's own report line, so a bug that prints "unchanged"
while still writing something would still be caught.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import database_snapshot, run_cli

MARKER_RULE = "Idempotent breakout"


def migrated(tmp_path: Path, name: str = "volume") -> Path:
    data_dir = tmp_path / name
    with temporary_database(data_dir):
        pass
    return data_dir


def whole_configuration() -> dict[str, object]:
    return {
        "version": 1,
        "tickers": [
            {"symbol": "AAPL", "timeframe": "1d", "enabled": True},
            {"symbol": "MSFT", "timeframe": "1d", "enabled": False},
        ],
        "rules": [
            {
                "enabled": True,
                "rule": {
                    "name": MARKER_RULE,
                    "signal": "BUY",
                    "timeframe": "1d",
                    "conditions": {
                        "all": [
                            {
                                "left": {"price": "close"},
                                "op": ">",
                                "right": {"indicator": "sma", "params": {"length": 20}},
                            }
                        ]
                    },
                    "cooldown_bars": 3,
                },
            }
        ],
        "assignments": [{"symbol": "AAPL", "timeframe": "1d", "rule": MARKER_RULE}],
    }


def write(tmp_path: Path, document: object, name: str = "configuration.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return str(path)


# --- re-importing the same file through the CLI writes nothing new (AC23) -------------------


def test_reimporting_the_same_file_with_skip_creates_nothing_new(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)
    source = write(tmp_path, whole_configuration())
    first = run_cli(["config", "import", source], data_dir=data_dir)
    assert first.code == 0
    after_first = database_snapshot(data_dir)

    second = run_cli(["config", "import", source], data_dir=data_dir)

    assert second.code == 0
    assert second.lines == [
        "skipped ticker AAPL 1d (enabled): it is already tracked",
        "skipped ticker MSFT 1d (disabled): it is already tracked",
        f"skipped rule '{MARKER_RULE}': a rule with that name already exists (id 1)",
        f"unchanged assignment AAPL 1d -> '{MARKER_RULE}'",
        f"imported {source}: 2 tickers, 1 rule, 1 assignment"
        " (0 created, 0 replaced, 1 unchanged, 3 skipped)",
    ]
    assert database_snapshot(data_dir) == after_first


def test_reimporting_the_same_file_with_replace_moves_nothing(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)
    source = write(tmp_path, whole_configuration())
    run_cli(["config", "import", source], data_dir=data_dir)
    after_first = database_snapshot(data_dir)

    second = run_cli(["config", "import", "--on-conflict", "replace", source], data_dir=data_dir)

    assert second.code == 0
    assert second.lines == [
        "unchanged ticker AAPL 1d (enabled)",
        "unchanged ticker MSFT 1d (disabled)",
        f"unchanged rule '{MARKER_RULE}' (id 1, 1d, BUY, enabled)",
        f"unchanged assignment AAPL 1d -> '{MARKER_RULE}'",
        f"imported {source}: 2 tickers, 1 rule, 1 assignment"
        " (0 created, 0 replaced, 4 unchanged, 0 skipped)",
    ]
    assert database_snapshot(data_dir) == after_first


def test_reimporting_three_times_in_a_row_is_stable(tmp_path: Path) -> None:
    """A third pass changes nothing either: idempotency is not a one-shot accident."""
    data_dir = migrated(tmp_path)
    source = write(tmp_path, whole_configuration())
    run_cli(["config", "import", source], data_dir=data_dir)
    run_cli(["config", "import", "--on-conflict", "replace", source], data_dir=data_dir)
    stable = database_snapshot(data_dir)

    third = run_cli(["config", "import", "--on-conflict", "replace", source], data_dir=data_dir)

    assert third.code == 0
    assert database_snapshot(data_dir) == stable


# --- a real (non-dry-run) no-op through the single-row commands writes nothing (AC17) --------


def test_enabling_an_already_enabled_ticker_writes_nothing_at_the_byte_level(
    tmp_path: Path,
) -> None:
    data_dir = migrated(tmp_path)
    run_cli(["tickers", "add", "AAPL"], data_dir=data_dir)
    before = database_snapshot(data_dir)

    result = run_cli(["tickers", "enable", "AAPL"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["unchanged ticker AAPL 1d (enabled)"]
    assert database_snapshot(data_dir) == before


def test_disabling_an_already_disabled_rule_writes_nothing_at_the_byte_level(
    tmp_path: Path,
) -> None:
    data_dir = migrated(tmp_path)
    source = write(tmp_path, whole_configuration())
    run_cli(["config", "import", source], data_dir=data_dir)
    run_cli(["rules", "disable", MARKER_RULE], data_dir=data_dir)
    before = database_snapshot(data_dir)

    result = run_cli(["rules", "disable", MARKER_RULE], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [f"unchanged rule '{MARKER_RULE}' (id 1, 1d, BUY, disabled)"]
    assert database_snapshot(data_dir) == before


def test_assigning_twice_via_the_cli_preserves_the_original_row(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)
    run_cli(["tickers", "add", "MSFT"], data_dir=data_dir)
    source = write(
        tmp_path,
        {
            "version": 1,
            "rules": [
                {
                    "enabled": True,
                    "rule": {
                        "name": "Standalone rule",
                        "signal": "BUY",
                        "timeframe": "1d",
                        "conditions": {
                            "all": [{"left": {"price": "close"}, "op": ">", "right": {"value": 1}}]
                        },
                    },
                }
            ],
        },
    )
    run_cli(["config", "import", source], data_dir=data_dir)
    run_cli(["assignments", "add", "MSFT", "Standalone rule"], data_dir=data_dir)
    before = database_snapshot(data_dir)

    result = run_cli(["assignments", "add", "MSFT", "Standalone rule"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["unchanged assignment MSFT 1d -> 'Standalone rule'"]
    assert database_snapshot(data_dir) == before


def test_database_snapshot_control_a_real_change_does_move_the_snapshot(tmp_path: Path) -> None:
    """Control for every equality check above: the comparison is not vacuously always true."""
    data_dir = migrated(tmp_path)
    run_cli(["tickers", "add", "AAPL"], data_dir=data_dir)
    before = database_snapshot(data_dir)

    changed = run_cli(["tickers", "disable", "AAPL"], data_dir=data_dir)

    assert changed.code == 0
    assert database_snapshot(data_dir) != before


# --- export -> import into an empty database -> export is byte-identical (AC24) --------------


def test_export_import_into_an_empty_database_then_export_is_byte_identical(
    tmp_path: Path,
) -> None:
    """AC24's round trip, assignments included: this is the only place it is exercised."""
    origin = migrated(tmp_path, "origin")
    source = write(tmp_path, whole_configuration())
    run_cli(["config", "import", source], data_dir=origin)
    # A second, unassigned ticker on another timeframe exercises the sort order too.
    run_cli(["tickers", "add", "NVDA", "--timeframe", "1h"], data_dir=origin)

    first_export = run_cli(["config", "export", "-"], data_dir=origin)
    assert first_export.code == 0
    assert '"assignments"' in first_export.out  # the round trip is not vacuously empty

    roundtrip_file = tmp_path / "roundtrip.json"
    roundtrip_file.write_text(first_export.out, encoding="utf-8", newline="\n")
    empty = migrated(tmp_path, "empty")

    imported = run_cli(["config", "import", str(roundtrip_file)], data_dir=empty)
    assert imported.code == 0

    second_export = run_cli(["config", "export", "-"], data_dir=empty)

    assert second_export.code == 0
    assert second_export.out == first_export.out
