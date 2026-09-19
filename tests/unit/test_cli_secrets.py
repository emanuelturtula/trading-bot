"""Secrets, paths and rule documents across a full CLI session (spec 013, T14, AC25, AC26;
spec 014, T16, AC27).

Every secret is a value built at runtime (CLAUDE.md rule 2), never a token-shaped literal. The
session exercises every one of the fourteen subcommands, including several rejection paths, so
a message built for an error case is checked exactly as hard as one built for a success. Markers
(a distinctive data directory name and a distinctive numeric field of the rule document) are
asserted present in the *input* first, so their absence from every captured line has real teeth
instead of trivially holding because nothing carried them in the first place.

The tests below this point extend the session to spec 014's sixteen subcommands, with a recorded
signal and a paused, beating bot state in the database: neither an indicator value, a corrupt
stored payload, a secret, the data directory nor an absolute path may reach any captured line.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import CliResult, repositories, run_cli, sample_rule, sample_signal
from trading_bot.cli import main as cli_main
from trading_bot.domain.timeframe import Timeframe
from trading_bot.logging_setup import RedactingFilter
from trading_bot.persistence.engine import DATABASE_FILENAME
from trading_bot.persistence.repositories.bot_state import SqlBotStateRepository
from trading_bot.persistence.repositories.signals import SqlSignalRepository

MARKER_COOLDOWN = 137  # only the rule document carries this; no report line ever shows it
DATA_DIR_MARKER = "cli-secrets-check-3fae21"  # distinctive enough that a stray match is real
RULE_NAME = "Secrets check rule"


def runtime_secrets() -> dict[str, str]:
    """Three secret-shaped values built at runtime (never a token-shaped literal in source)."""
    return {
        "TB_TELEGRAM_BOT_TOKEN": "123456789" + ":" + "x" * 35,
        "TB_DASHBOARD_PASSWORD_HASH": "hash-" + "y" * 40,
        "TB_SESSION_SECRET": "session-" + "z" * 40,
    }


def apply_secrets(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    secrets = runtime_secrets()
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    return secrets


def configuration_file(tmp_path: Path) -> str:
    document = {
        "version": 1,
        "tickers": [{"symbol": "AAPL", "timeframe": "1d", "enabled": True}],
        "rules": [
            {
                "enabled": True,
                "rule": {
                    "name": RULE_NAME,
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
                    "cooldown_bars": MARKER_COOLDOWN,
                },
            }
        ],
        "assignments": [{"symbol": "AAPL", "timeframe": "1d", "rule": RULE_NAME}],
    }
    path = tmp_path / "configuration.json"
    path.write_text(json.dumps(document) + "\n", encoding="utf-8", newline="\n")
    return str(path)


def run_full_session(source: str, data_dir: Path, tmp_path: Path) -> list[CliResult]:
    """Exercise all fourteen subcommands, including a handful of rejection paths."""
    destination = tmp_path / "exported.json"
    return [
        run_cli(["config", "import", source], data_dir=data_dir),
        run_cli(["tickers", "list"], data_dir=data_dir),
        run_cli(["tickers", "add", "MSFT"], data_dir=data_dir),
        run_cli(["tickers", "add", "MSFT"], data_dir=data_dir),  # rejected: exit 1
        run_cli(["tickers", "disable", "MSFT"], data_dir=data_dir),
        run_cli(["tickers", "enable", "MSFT"], data_dir=data_dir),
        run_cli(["rules", "list"], data_dir=data_dir),
        run_cli(["rules", "disable", RULE_NAME], data_dir=data_dir),
        run_cli(["rules", "enable", RULE_NAME], data_dir=data_dir),
        run_cli(["rules", "remove", RULE_NAME], data_dir=data_dir),  # rejected: still assigned
        run_cli(["assignments", "list"], data_dir=data_dir),
        run_cli(["assignments", "add", "MSFT", RULE_NAME], data_dir=data_dir),
        run_cli(["assignments", "remove", "MSFT", RULE_NAME], data_dir=data_dir),
        run_cli(["tickers", "remove", "AAPL"], data_dir=data_dir),  # rejected: still assigned
        run_cli(["tickers", "remove", "AAPL", "--force"], data_dir=data_dir),
        run_cli(["config", "export", str(destination)], data_dir=data_dir),
        run_cli(["config", "export", str(destination)], data_dir=data_dir),  # rejected: exists
        run_cli(["no-such-group"], data_dir=data_dir),  # argparse usage error: exit 2
    ]


# --- no secret, rule document or absolute path in any line of the session -------------------


def test_a_full_session_leaks_no_secret_no_rule_document_and_no_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = apply_secrets(monkeypatch)
    data_dir = tmp_path / DATA_DIR_MARKER
    with temporary_database(data_dir):
        pass
    source = configuration_file(tmp_path)
    # Control: the markers are real input, so their absence from the output below is meaningful.
    source_text = Path(source).read_text(encoding="utf-8")
    assert str(MARKER_COOLDOWN) in source_text
    assert '"conditions"' in source_text

    results = run_full_session(source, data_dir, tmp_path)

    assert {result.code for result in results} == {0, 1, 2}  # a real mix of outcomes, not vacuous
    combined = "\n".join(result.out + result.err for result in results)
    for value in secrets.values():
        assert value not in combined
    assert DATA_DIR_MARKER not in combined
    assert str(MARKER_COOLDOWN) not in combined
    assert '"conditions"' not in combined


def test_configure_logging_receives_every_configured_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC26's wiring: ``main`` hands every secret to the redaction filter before any work.

    This is what gives the "no log record contains a secret" half of AC26 real teeth: the CLI
    itself never logs anything secret-shaped (there is no code path that would), so a session
    with zero matching log records would pass just as well against a build that forgot to wire
    the redaction filter at all. Pinning the exact arguments the filter is built from, and then
    proving that filter would in fact catch every secret, closes that gap.
    """
    secrets = apply_secrets(monkeypatch)
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    captured: list[str] = []
    real_configure_logging = cli_main.configure_logging

    def spy(level: str, configured_secrets: object = ()) -> None:
        captured.extend(configured_secrets)  # type: ignore[arg-type]
        real_configure_logging(level, configured_secrets)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_main, "configure_logging", spy)

    result = run_cli(["tickers", "list"], data_dir=data_dir)

    assert result.code == 0
    assert set(captured) == set(secrets.values())
    redaction = RedactingFilter(captured)
    for value in secrets.values():
        assert redaction.redact(f"a log line carrying {value} by mistake") == (
            "a log line carrying [REDACTED] by mistake"
        )


# --- no new TB_* environment variable and no stray file (AC25) ------------------------------


def test_the_session_does_not_mutate_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_secrets(monkeypatch)
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    source = configuration_file(tmp_path)
    before = {key for key in os.environ if key.startswith("TB_")}

    run_full_session(source, data_dir, tmp_path)

    after = {key for key in os.environ if key.startswith("TB_")}
    assert after == before


def test_the_session_creates_exactly_the_database_file_and_the_given_export_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_secrets(monkeypatch)
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    source = configuration_file(tmp_path)
    before = {path.name for path in tmp_path.iterdir()}

    run_full_session(source, data_dir, tmp_path)

    after = {path.name for path in tmp_path.iterdir()}
    assert after - before == {"exported.json"}
    created = {path.name for path in data_dir.iterdir()}
    assert created <= {DATABASE_FILENAME, f"{DATABASE_FILENAME}-wal", f"{DATABASE_FILENAME}-shm"}
    assert DATABASE_FILENAME in created


# --- T16 (tester): sixteen subcommands, with signals and bot state present (spec 014, AC27) -

# Only the indicator-values text carries this: it must never reach a captured line, because
# spec 014 Design 10.3 deliberately keeps ``signals list`` to a bounded line with no values.
INDICATOR_MARKER = "leak-marker-in-indicator-values-7f3ac1"
# Stands in for whatever a manual ``sqlite3`` session could have written into a stored row;
# decision D93/D119 says it must never reach a message or a traceback.
CORRUPT_MARKER = "corrupt-payload-marker-9d2b6e"


def seed_signal_and_state(
    data_dir: Path, *, symbol: str = "AAPL", rule_name: str = RULE_NAME
) -> int:
    """One recorded signal, its indicator values carrying a marker, and a paused, beating state.

    ``symbol`` and ``rule_name`` default to what the corrupt-payload test below uses in
    isolation; the sixteen-subcommand test picks a ticker and a rule name of its own so the two
    never collide with ``configuration_file``'s AAPL ticker and ``RULE_NAME`` rule. Returns the
    signal's id, so a later raw-SQL corruption can target it precisely.
    """
    with temporary_database(data_dir) as database:
        with database.session() as session:
            handles = repositories(session)
            ticker = handles.tickers.add(symbol, Timeframe.D1)
            rule = handles.rules.add(sample_rule(rule_name))
            handles.assignments.assign(ticker.id, rule.id)
        with database.session() as session:
            signal = sample_signal(ticker, rule, indicator_values={INDICATOR_MARKER: 1.5})
            stored_id = SqlSignalRepository(session).record(signal).stored.id
            state = SqlBotStateRepository(session)
            state.pause()
            state.record_heartbeat()
    return stored_id


def test_sixteen_subcommands_with_signals_and_bot_state_leak_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 013's own fourteen subcommands (``run_full_session``, unmodified), plus ``signals
    list`` and ``state show``, over a database that already holds a recorded signal and a
    paused, beating bot state for a ticker and a rule the fourteen-subcommand flow never touches
    (``GOOG`` / a rule name of its own), so the flow's own AAPL/MSFT lifecycle cannot remove them
    partway through and silently make the added commands run over an empty history instead.
    """
    secrets = apply_secrets(monkeypatch)
    data_dir = tmp_path / DATA_DIR_MARKER
    seed_signal_and_state(data_dir, symbol="GOOG", rule_name="History rule for T16")
    source = configuration_file(tmp_path)
    before = {key for key in os.environ if key.startswith("TB_")}

    results = [
        *run_full_session(source, data_dir, tmp_path),
        run_cli(["signals", "list"], data_dir=data_dir),
        run_cli(["signals", "list", "--ticker", "GOOG"], data_dir=data_dir),
        run_cli(["signals", "list", "--rule", "History rule for T16"], data_dir=data_dir),
        run_cli(["signals", "list", "--ticker", "UNKNOWN"], data_dir=data_dir),  # rejected
        run_cli(["signals", "list", "--rule", "no such rule"], data_dir=data_dir),  # rejected
        run_cli(["state", "show"], data_dir=data_dir),
    ]

    after = {key for key in os.environ if key.startswith("TB_")}
    assert after == before  # AC27, extended: still no new TB_* variable
    codes = {result.code for result in results}
    assert {0, 1, 2} <= codes  # a real mix, not a vacuous session (mirrors the fourteen-only one)
    combined = "\n".join(result.out + result.err for result in results)
    for value in secrets.values():
        assert value not in combined
    assert DATA_DIR_MARKER not in combined
    assert INDICATOR_MARKER not in combined  # the values never print, filters included
    # Control: the marker really is stored, so its absence above is a real assertion.
    with temporary_database(data_dir) as database, database.session() as session:
        stored = SqlSignalRepository(session).get(1)
    assert stored is not None
    assert INDICATOR_MARKER in stored.signal.indicator_values


def test_a_corrupt_signal_makes_signals_list_fail_without_leaking_the_stored_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision D93/D119: a stored payload that no longer loads fails loudly, never leaking."""
    apply_secrets(monkeypatch)
    data_dir = tmp_path / "volume"
    signal_id = seed_signal_and_state(data_dir)
    with temporary_database(data_dir) as database, database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE signals SET indicator_values_json = ? WHERE id = ?",
            (f'{{"leaked": "{CORRUPT_MARKER}"}}', signal_id),
        )

    result = run_cli(["signals", "list"], data_dir=data_dir)

    assert result.code == 1
    assert result.out == ""
    assert result.err == f"error: the stored signal {signal_id} is not valid: indicator_values\n"
    assert CORRUPT_MARKER not in result.err
    assert "leaked" not in result.err
